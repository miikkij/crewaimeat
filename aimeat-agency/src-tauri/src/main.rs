// aimeat-agency — thin Tauri shell. The crewaimeat SOURCE and the `uv` binary are BUNDLED in the
// installer (no git, no pre-installed tools). On first run it copies the bundled source to a writable
// dir and `uv sync`s it, then spawns the Python cockpit and shows it.
//
// Lifecycle (what the user asked for):
//   • The window's X HIDES to the system tray (does NOT quit). Reopen from the tray icon.
//   • Tray menu: Open Window · Shut down & Quit. "Shut down" POSTs /api/shutdown so the cockpit stops
//     the WHOLE fleet (serve daemon + crew CMD windows, repo/home-scoped) BEFORE the app exits.
//   • If the cockpit process exits (e.g. the in-app "Shut down" button asked it to), the shell quits too.
#![cfg_attr(all(not(debug_assertions), target_os = "windows"), windows_subsystem = "windows")]

use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Manager, WindowEvent,
};

const PORT: u16 = 8753;
const TRAY_ID: &str = "aimeat_agency_tray";
const ID_OPEN: &str = "open_window";
const ID_QUIT: &str = "shutdown_quit";

/// Shared state: the per-launch cockpit token (to authenticate /api/shutdown) + the cockpit PID.
struct AppState {
    token: String,
    pid: Mutex<Option<u32>>,
}

/// Writable home for the runtime (the copied crewaimeat checkout + its venv).
fn runtime_dir() -> PathBuf {
    if let Ok(local) = std::env::var("LOCALAPPDATA") {
        return PathBuf::from(local).join("aimeat-agency");
    }
    PathBuf::from(std::env::var("USERPROFILE").unwrap_or_else(|_| ".".into())).join(".aimeat-agency")
}

fn run(cmd: &str, args: &[&str], cwd: Option<&Path>) -> std::io::Result<std::process::Output> {
    let mut c = Command::new(cmd);
    c.args(args);
    if let Some(d) = cwd {
        c.current_dir(d);
    }
    c.output()
}

fn ok(out: &std::io::Result<std::process::Output>) -> bool {
    matches!(out, Ok(o) if o.status.success())
}

fn copy_dir(src: &Path, dst: &Path) -> std::io::Result<()> {
    std::fs::create_dir_all(dst)?;
    for entry in std::fs::read_dir(src)? {
        let entry = entry?;
        let from = entry.path();
        let to = dst.join(entry.file_name());
        if from.is_dir() {
            copy_dir(&from, &to)?;
        } else {
            std::fs::copy(&from, &to)?;
        }
    }
    Ok(())
}

/// The bundled `uv.exe` (Tauri places the externalBin next to the app exe, triple stripped). Falls back
/// to a `uv` on PATH for `tauri dev`.
fn uv_path() -> Option<String> {
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            let p = dir.join("uv.exe");
            if p.exists() {
                return Some(p.to_string_lossy().into_owned());
            }
        }
    }
    if ok(&run("uv", &["--version"], None)) {
        return Some("uv".into());
    }
    None
}

/// Provision idempotently from BUNDLED assets: copy the bundled crewaimeat source to the writable runtime
/// on first run, then `uv sync --extra agency` (uv fetches Python + deps). Returns (uv, repo_dir).
fn provision(handle: &AppHandle, say: &dyn Fn(&str)) -> Result<(String, PathBuf), String> {
    let repo = runtime_dir().join("crewaimeat");
    std::fs::create_dir_all(repo.parent().unwrap()).ok();

    let uv = uv_path().ok_or("uv not found (the bundled uv.exe is missing).")?;

    if !repo.join("pyproject.toml").exists() {
        say("Setting up the agency (first run)…");
        let res = handle.path().resource_dir().map_err(|e| e.to_string())?;
        let a = res.join("resources").join("runtime-src");
        let bundled = if a.exists() { a } else { res.join("runtime-src") };
        copy_dir(&bundled, &repo).map_err(|e| format!("could not lay out the runtime: {e}"))?;
    }

    say("Installing dependencies (first run, a few minutes)…");
    let s = run(&uv, &["sync", "--extra", "agency"], Some(&repo));
    if !ok(&s) {
        return Err("Dependency install failed (uv sync). Check your connection and reopen.".into());
    }
    Ok((uv, repo))
}

fn spawn_cockpit(uv: &str, repo: &Path, token: &str) -> std::io::Result<Child> {
    Command::new(uv)
        .args(["run", "--extra", "agency", "python", "-m", "crewaimeat.agency.cockpit"])
        .current_dir(repo)
        .env("AIMEAT_HOME", repo.join(".aimeat"))
        .env("AIMEAT_AGENCY_TOKEN", token)
        .env("AIMEAT_AGENCY_PORT", PORT.to_string())
        .spawn()
}

fn wait_up(timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    while Instant::now() < deadline {
        if std::net::TcpStream::connect(("127.0.0.1", PORT)).is_ok() {
            return true;
        }
        std::thread::sleep(Duration::from_millis(300));
    }
    false
}

fn splash_say(handle: &AppHandle, msg: &str) {
    if let Some(win) = handle.get_webview_window("splash") {
        let safe = msg.replace('\'', "\\'");
        let _ = win.eval(&format!("var m=document.querySelector('.muted'); if(m) m.textContent='{safe}';"));
    }
}

fn show_window(handle: &AppHandle) {
    if let Some(win) = handle.get_webview_window("splash") {
        let _ = win.unminimize();
        let _ = win.show();
        let _ = win.set_focus();
    }
}

/// Ask the cockpit to stop the whole fleet (it self-exits afterwards, which makes the shell quit). Falls
/// back to a forced kill + exit if the cockpit doesn't answer.
fn shutdown_and_quit(handle: &AppHandle) {
    let state = handle.state::<AppState>();
    let token = state.token.clone();
    let pid = *state.pid.lock().unwrap();
    let h = handle.clone();
    std::thread::spawn(move || {
        // best-effort: cockpit stops the fleet (crews + serve) then self-exits
        let _ = ureq::post(&format!("http://127.0.0.1:{PORT}/api/shutdown"))
            .set("Authorization", &format!("Bearer {token}"))
            .timeout(Duration::from_secs(30))
            .call();
        // give the cockpit a moment to exit on its own (the child-watcher would quit us), else force it
        std::thread::sleep(Duration::from_secs(3));
        if let Some(p) = pid {
            let _ = run("taskkill", &["/PID", &p.to_string(), "/T", "/F"], None);
        }
        h.exit(0);
    });
}

fn setup_tray(app: &tauri::App) -> tauri::Result<()> {
    let h = app.handle();
    let open = MenuItem::with_id(h, ID_OPEN, "Open Window", true, None::<&str>)?;
    let sep = PredefinedMenuItem::separator(h)?;
    let quit = MenuItem::with_id(h, ID_QUIT, "Shut down & Quit", true, None::<&str>)?;
    let menu = Menu::with_items(h, &[&open, &sep, &quit])?;

    let mut builder = TrayIconBuilder::with_id(TRAY_ID)
        .tooltip("aimeat-agency")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|h, ev| {
            if ev.id() == ID_OPEN {
                show_window(h);
            } else if ev.id() == ID_QUIT {
                shutdown_and_quit(h);
            }
        })
        .on_tray_icon_event(|tray, ev| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = ev
            {
                show_window(tray.app_handle());
            }
        });
    if let Some(icon) = app.default_window_icon() {
        builder = builder.icon(icon.clone());
    }
    builder.build(app)?;
    Ok(())
}

fn main() {
    let nanos = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0);
    let token = format!("{:x}{:x}", nanos, std::process::id());

    tauri::Builder::default()
        .manage(AppState { token: token.clone(), pid: Mutex::new(None) })
        .setup(move |app| {
            setup_tray(app)?;
            let handle = app.handle().clone();
            let token = token.clone();
            let url = format!("http://127.0.0.1:{}/", PORT);

            std::thread::spawn(move || {
                let say = |m: &str| splash_say(&handle, m);
                match provision(&handle, &say) {
                    Ok((uv, repo)) => {
                        say("Starting your agency…");
                        match spawn_cockpit(&uv, &repo, &token) {
                            Ok(mut child) => {
                                *handle.state::<AppState>().pid.lock().unwrap() = Some(child.id());
                                if wait_up(Duration::from_secs(120)) {
                                    if let Some(win) = handle.get_webview_window("splash") {
                                        let _ = win.eval(&format!("window.location.replace('{}')", url));
                                    }
                                } else {
                                    say("The cockpit did not start — see the logs.");
                                }
                                // Block until the cockpit exits (e.g. the in-app Shut down). Then quit the app.
                                let _ = child.wait();
                                handle.exit(0);
                            }
                            Err(e) => say(&format!("Could not start the cockpit: {e}")),
                        }
                    }
                    Err(e) => say(&e),
                }
            });
            Ok(())
        })
        .on_window_event(|window, event| {
            // The X hides to tray instead of quitting (reopen from the tray icon).
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building aimeat-agency")
        .run(|app_handle, event| {
            // Belt-and-suspenders: if the app exits by any path, make sure the cockpit child is gone.
            if let tauri::RunEvent::ExitRequested { .. } = event {
                if let Some(pid) = *app_handle.state::<AppState>().pid.lock().unwrap() {
                    let _ = run("taskkill", &["/PID", &pid.to_string(), "/T", "/F"], None);
                }
            }
        });
}

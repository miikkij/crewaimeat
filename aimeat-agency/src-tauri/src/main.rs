// aimeat-agency — thin Tauri shell. The crewaimeat SOURCE and the `uv` binary are BUNDLED in the
// installer (no git, no pre-installed tools).
//
// First-run order (what the user asked for): the splash asks the LANGUAGE and shows WHAT WILL HAPPEN —
// INCLUDING that helper terminal windows (the agency engine + agents) will open and stay open — and
// nothing runs until the user clicks Begin. The windows are intentionally visible; the splash explains
// them so they aren't scary, and "Shut down" closes them all safely. The window's X hides to the tray.
#![cfg_attr(all(not(debug_assertions), target_os = "windows"), windows_subsystem = "windows")]

use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use tauri::{
    menu::{Menu, MenuItem, PredefinedMenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Manager, WindowEvent,
};
use tauri_plugin_updater::UpdaterExt;

const PORT: u16 = 8753;
const TRAY_ID: &str = "aimeat_agency_tray";
const ID_OPEN: &str = "open_window";
const ID_QUIT: &str = "shutdown_quit";

// Set once the fleet teardown (/api/shutdown -> terminate_fleet.ps1, home-scoped) has been kicked off,
// so the tray-quit path and the ExitRequested fallback never double-tear-down (or double-block on exit).
static TEARDOWN_DONE: AtomicBool = AtomicBool::new(false);

struct AppState {
    token: String,
    pid: Mutex<Option<u32>>,
    lang: Mutex<String>,
    started: Mutex<bool>, // provisioning kicked off? (guards against a double Begin)
}

fn runtime_dir() -> PathBuf {
    if let Ok(local) = std::env::var("LOCALAPPDATA") {
        return PathBuf::from(local).join("aimeat-agency").join("runtime");
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

/// Localized splash status text (EN/FI) — so the progress the user sees matches the language they chose.
fn status(lang: &str, key: &str) -> String {
    let fi = lang == "fi";
    match key {
        "setup" => if fi { "Otetaan agency käyttöön (ensimmäinen kerta)…" } else { "Setting up the agency (first run)…" },
        "update" => if fi { "Päivitetään uusimpaan versioon…" } else { "Updating to the latest version…" },
        "deps" => if fi { "Asennetaan tarvittavat osat (hetki)…" } else { "Installing dependencies (a moment)…" },
        "starting" => if fi { "Avataan agency…" } else { "Starting your agency…" },
        "err_uv" => if fi { "uv-työkalua ei löytynyt (asennus on vajaa)." } else { "uv not found (the bundled uv.exe is missing)." },
        "err_layout" => if fi { "Ajoympäristön luonti epäonnistui" } else { "could not lay out the runtime" },
        "err_deps" => if fi { "Osien asennus epäonnistui. Tarkista nettiyhteys ja avaa sovellus uudelleen." } else { "Dependency install failed. Check your connection and reopen." },
        "err_cockpit" => if fi { "Agency ei käynnistynyt — katso loki." } else { "The cockpit did not start — see the logs." },
        "err_start" => if fi { "Agencyä ei voitu käynnistää" } else { "Could not start the cockpit" },
        _ => key,
    }
    .to_string()
}

/// First run AND version change → (re)copy the bundled source (preserves .aimeat/.venv), then uv sync.
fn provision(handle: &AppHandle, lang: &str, say: &dyn Fn(&str)) -> Result<(String, PathBuf), String> {
    let repo = runtime_dir().join("crewaimeat");
    std::fs::create_dir_all(repo.parent().unwrap()).ok();

    let uv = uv_path().ok_or_else(|| status(lang, "err_uv"))?;

    let version = env!("CARGO_PKG_VERSION");
    let marker = repo.join(".agency_version");
    let installed = std::fs::read_to_string(&marker).unwrap_or_default();
    let fresh = !repo.join("pyproject.toml").exists();
    if fresh || installed.trim() != version {
        say(&status(lang, if fresh { "setup" } else { "update" }));
        let res = handle.path().resource_dir().map_err(|e| e.to_string())?;
        let a = res.join("resources").join("runtime-src");
        let bundled = if a.exists() { a } else { res.join("runtime-src") };
        copy_dir(&bundled, &repo).map_err(|e| format!("{}: {e}", status(lang, "err_layout")))?;
        std::fs::write(&marker, version).ok();
    }

    say(&status(lang, "deps"));
    let s = run(&uv, &["sync", "--extra", "agency"], Some(&repo));
    if !ok(&s) {
        return Err(status(lang, "err_deps"));
    }
    Ok((uv, repo))
}

fn spawn_cockpit(uv: &str, repo: &Path, token: &str) -> std::io::Result<Child> {
    // The cockpit (and later the fleet) run in their own visible windows ON PURPOSE — the splash tells the
    // user they'll open and what they are. "Shut down" closes them all safely.
    //
    // Spawn the VENV python directly (not `uv run`): `uv run` keeps uv.exe alive as the parent for the whole
    // app session, which LOCKS uv.exe so the next installer can't overwrite it ("Error opening file for
    // writing: uv.exe"). After `uv sync`, the venv python exists, so we use it and uv.exe stays free.
    let venv_py = repo.join(".venv").join("Scripts").join("python.exe");
    let mut c = if venv_py.exists() {
        let mut c = Command::new(&venv_py);
        c.args(["-m", "crewaimeat.agency.cockpit"]);
        c
    } else {
        let mut c = Command::new(uv);
        c.args(["run", "--extra", "agency", "python", "-m", "crewaimeat.agency.cockpit"]);
        c
    };
    c.current_dir(repo)
        .env("AIMEAT_HOME", repo.join(".aimeat"))
        .env("AIMEAT_AGENCY", "1") // lets runtime code branch appliance-vs-dev explicitly
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
        let _ = win.eval(&format!("if(window.agencyStatus)window.agencyStatus('{safe}');"));
    }
}

fn show_window(handle: &AppHandle) {
    if let Some(win) = handle.get_webview_window("splash") {
        let _ = win.unminimize();
        let _ = win.show();
        let _ = win.set_focus();
    }
}

/// Provision + spawn the cockpit, then point the window at it (with the chosen language). Called once,
/// from the `begin` command — never before the user picks a language and clicks Begin.
fn start_provisioning(handle: AppHandle) {
    let token = handle.state::<AppState>().token.clone();
    let lang = handle.state::<AppState>().lang.lock().unwrap().clone();
    std::thread::spawn(move || {
        let say = |m: &str| splash_say(&handle, m);
        match provision(&handle, &lang, &say) {
            Ok((uv, repo)) => {
                say(&status(&lang, "starting"));
                match spawn_cockpit(&uv, &repo, &token) {
                    Ok(mut child) => {
                        *handle.state::<AppState>().pid.lock().unwrap() = Some(child.id());
                        if wait_up(Duration::from_secs(120)) {
                            // `boot` proves the caller knows the token — the cockpit refuses to hand
                            // the UI (with the injected token) to any local process that doesn't.
                            let url = format!("http://127.0.0.1:{}/?lang={}&boot={}", PORT, lang, token);
                            if let Some(win) = handle.get_webview_window("splash") {
                                let _ = win.eval(&format!("window.location.replace('{}')", url));
                            }
                        } else {
                            say(&status(&lang, "err_cockpit"));
                        }
                        let _ = child.wait(); // cockpit exited (e.g. in-app Shut down) -> quit the app
                        handle.exit(0);
                    }
                    Err(e) => say(&format!("{}: {e}", status(&lang, "err_start"))),
                }
            }
            Err(e) => say(&e),
        }
    });
}

/// Called by the splash AFTER the user picks a language and clicks Begin. Stores the language, then
/// provisions + opens the cockpit. Idempotent (a second Begin is ignored).
#[tauri::command]
fn begin(app: AppHandle, lang: String) {
    let st = app.state::<AppState>();
    *st.lang.lock().unwrap() = if lang.trim().is_empty() { "en".into() } else { lang };
    {
        let mut started = st.started.lock().unwrap();
        if *started {
            return;
        }
        *started = true;
    }
    start_provisioning(app.clone());
}

fn shutdown_and_quit(handle: &AppHandle) {
    let state = handle.state::<AppState>();
    let token = state.token.clone();
    let pid = *state.pid.lock().unwrap();
    let h = handle.clone();
    TEARDOWN_DONE.store(true, Ordering::SeqCst); // claim the teardown so ExitRequested doesn't repeat it
    std::thread::spawn(move || {
        let _ = ureq::post(&format!("http://127.0.0.1:{PORT}/api/shutdown"))
            .set("Authorization", &format!("Bearer {token}"))
            .timeout(Duration::from_secs(30))
            .call();
        std::thread::sleep(Duration::from_secs(3));
        if let Some(p) = pid {
            let _ = run("taskkill", &["/PID", &p.to_string(), "/T", "/F"], None);
        }
        h.exit(0);
    });
}

/// Download + install the available signed update, then relaunch. Called from the splash's "Update now"
/// button (BEFORE Begin, so no fleet is running and nothing locks uv.exe → a clean install). Returns
/// false if there was nothing to install.
#[tauri::command]
async fn install_update(app: AppHandle) -> Result<bool, String> {
    let updater = app.updater().map_err(|e| e.to_string())?;
    if let Some(update) = updater.check().await.map_err(|e| e.to_string())? {
        update.download_and_install(|_, _| {}, || {}).await.map_err(|e| e.to_string())?;
        return Ok(true);
    }
    Ok(false)
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
    // The token gates every cockpit /api/* call (start/stop/reset/publish), so it must be
    // unguessable by other local processes — OS entropy, not the old timestamp+pid (predictable).
    let mut buf = [0u8; 32];
    getrandom::fill(&mut buf).expect("OS entropy unavailable");
    let token: String = buf.iter().map(|b| format!("{:02x}", b)).collect();

    tauri::Builder::default()
        .manage(AppState {
            token,
            pid: Mutex::new(None),
            lang: Mutex::new("en".into()),
            started: Mutex::new(false),
        })
        .plugin(tauri_plugin_updater::Builder::new().build())
        .invoke_handler(tauri::generate_handler![begin, install_update])
        .setup(move |app| {
            setup_tray(app)?;
            // Check for a newer signed release at startup (before provisioning, so an install is clean —
            // no fleet locking uv.exe). If one exists, tell the splash to offer "Update now".
            let h = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                let mut offered = false;
                if let Ok(updater) = h.updater() {
                    if let Ok(Some(update)) = updater.check().await {
                        let v = update.version.replace('\'', "");
                        if let Some(win) = h.get_webview_window("splash") {
                            let _ = win.eval(&format!("if(window.agencyUpdate)window.agencyUpdate('{}')", v));
                            offered = true;
                        }
                    }
                }
                if !offered {
                    if let Some(win) = h.get_webview_window("splash") {
                        let _ = win.eval("if(window.agencyNoUpdate)window.agencyNoUpdate()");
                    }
                }
            });
            // Do NOT provision yet — the splash gathers language + consent (Begin) first.
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide(); // X hides to tray; reopen from the tray icon
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building aimeat-agency")
        .run(|app_handle, event| {
            if let tauri::RunEvent::ExitRequested { .. } = event {
                let state = app_handle.state::<AppState>();
                // The serve daemon + crews are DETACHED, so taskkill /T on the cockpit never reaches them.
                // Tear down THIS home's fleet (home-scoped terminate_fleet.ps1 via /api/shutdown) on any
                // exit path that didn't already (tray "Shut down & Quit" claims it first) — otherwise they
                // orphan and the next launch wastes time reaping them. Blocks briefly; we're exiting anyway.
                if !TEARDOWN_DONE.swap(true, Ordering::SeqCst) {
                    let token = state.token.clone();
                    let _ = ureq::post(&format!("http://127.0.0.1:{PORT}/api/shutdown"))
                        .set("Authorization", &format!("Bearer {token}"))
                        .timeout(Duration::from_secs(30))
                        .call();
                    std::thread::sleep(Duration::from_secs(2));
                }
                let pid = *state.pid.lock().unwrap(); // copy out so the MutexGuard drops before the if-let
                if let Some(p) = pid {
                    let _ = run("taskkill", &["/PID", &p.to_string(), "/T", "/F"], None);
                }
            }
        });
}

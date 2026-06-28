// aimeat-agency — thin Tauri shell. The crewaimeat SOURCE and the `uv` binary are BUNDLED in the
// installer (no git, no pre-installed tools). On first run it copies the bundled source to a writable
// dir and `uv sync`s it (uv fetches Python + deps), then spawns the Python cockpit and shows it.
#![cfg_attr(all(not(debug_assertions), target_os = "windows"), windows_subsystem = "windows")]

use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use tauri::{AppHandle, Manager, WebviewWindow};

const PORT: u16 = 8753;

struct Cockpit(Mutex<Option<Child>>);

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

/// Provision idempotently from BUNDLED assets (no git, no network for the source): copy the bundled
/// crewaimeat source to the writable runtime on first run, then `uv sync --extra agency` (uv fetches
/// Python + the deps). Returns (uv, repo_dir). `say` streams a status to the splash.
fn provision(handle: &AppHandle, say: &dyn Fn(&str)) -> Result<(String, PathBuf), String> {
    let repo = runtime_dir().join("crewaimeat");
    std::fs::create_dir_all(repo.parent().unwrap()).ok();

    let uv = uv_path().ok_or("uv not found (the bundled uv.exe is missing).")?;

    if !repo.join("pyproject.toml").exists() {
        say("Setting up the agency (first run)…");
        let res = handle.path().resource_dir().map_err(|e| e.to_string())?;
        // Tauri may place a resource under <res>/resources/runtime-src or <res>/runtime-src.
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

fn splash_say(win: &WebviewWindow, msg: &str) {
    let safe = msg.replace('\'', "\\'");
    let _ = win.eval(&format!("var m=document.querySelector('.muted'); if(m) m.textContent='{safe}';"));
}

fn main() {
    let nanos = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0);
    let token = format!("{:x}{:x}", nanos, std::process::id());

    tauri::Builder::default()
        .manage(Cockpit(Mutex::new(None)))
        .setup(move |app| {
            let win = app.get_webview_window("splash").expect("splash window missing");
            let handle = app.handle().clone();
            let token = token.clone();
            let url = format!("http://127.0.0.1:{}/", PORT);

            std::thread::spawn(move || {
                let say = |m: &str| splash_say(&win, m);
                match provision(&handle, &say) {
                    Ok((uv, repo)) => {
                        say("Starting your agency…");
                        match spawn_cockpit(&uv, &repo, &token) {
                            Ok(child) => {
                                *handle.state::<Cockpit>().0.lock().unwrap() = Some(child);
                                if wait_up(Duration::from_secs(120)) {
                                    let _ = win.eval(&format!("window.location.replace('{}')", url));
                                } else {
                                    say("The cockpit did not start — see the logs.");
                                }
                            }
                            Err(e) => say(&format!("Could not start the cockpit: {e}")),
                        }
                    }
                    Err(e) => say(&e),
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building aimeat-agency")
        .run(|app_handle, event| {
            if let tauri::RunEvent::ExitRequested { .. } = event {
                if let Some(mut child) = app_handle.state::<Cockpit>().0.lock().unwrap().take() {
                    let _ = child.kill();
                }
            }
        });
}

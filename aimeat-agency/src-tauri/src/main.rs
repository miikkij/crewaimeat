// aimeat-agency — thin Tauri shell. On first run it PROVISIONS the cockpit runtime the way
// aimeat-desktop does (clone the crewaimeat repo + `uv sync`, never bundling Python), then spawns the
// Python cockpit and points the window at it. All product logic lives in the cockpit.
#![cfg_attr(all(not(debug_assertions), target_os = "windows"), windows_subsystem = "windows")]

use std::path::{Path, PathBuf};
use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use tauri::{Manager, WebviewWindow};

const PORT: u16 = 8753;
const REPO: &str = "https://github.com/miikkij/crewaimeat";

struct Cockpit(Mutex<Option<Child>>);

/// Where the cockpit runtime lives on the user's machine (the cloned crewaimeat checkout + its venv).
fn runtime_dir() -> PathBuf {
    if let Ok(local) = std::env::var("LOCALAPPDATA") {
        return PathBuf::from(local).join("aimeat-agency");
    }
    PathBuf::from(std::env::var("USERPROFILE").unwrap_or_else(|_| ".".into()))
        .join(".aimeat")
        .join("agency-runtime")
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

/// Find `uv`: on PATH, else the location its official installer drops it.
fn find_uv() -> Option<String> {
    if ok(&run("uv", &["--version"], None)) {
        return Some("uv".into());
    }
    let home = std::env::var("USERPROFILE").unwrap_or_default();
    for p in [
        format!("{home}\\.local\\bin\\uv.exe"),
        format!("{home}\\.cargo\\bin\\uv.exe"),
    ] {
        if Path::new(&p).exists() {
            return Some(p);
        }
    }
    None
}

/// Provision the cockpit runtime (idempotent): ensure uv, clone/update crewaimeat, `uv sync --extra
/// agency`. Returns the uv binary + the repo dir on success. `say` streams a status to the splash.
fn provision(say: &dyn Fn(&str)) -> Result<(String, PathBuf), String> {
    let rt = runtime_dir();
    let repo = rt.join("crewaimeat");
    std::fs::create_dir_all(&rt).ok();

    // 1) git is required to fetch the runtime.
    if !ok(&run("git", &["--version"], None)) {
        return Err("Git is required — install Git for Windows from https://git-scm.com, then reopen.".into());
    }

    // 2) ensure uv (install via the official script if missing).
    let uv = match find_uv() {
        Some(u) => u,
        None => {
            say("Installing uv (one-time)…");
            let _ = run(
                "powershell",
                &["-NoProfile", "-Command", "irm https://astral.sh/uv/install.ps1 | iex"],
                None,
            );
            find_uv().ok_or("Could not install uv (https://astral.sh/uv).")?
        }
    };

    // 3) clone or update the crewaimeat checkout (in place, branch-tracking).
    if repo.join(".git").exists() || repo.join("pyproject.toml").exists() {
        say("Updating the agency runtime…");
        let _ = run("git", &["-C", repo.to_str().unwrap(), "init", "-q"], None);
        if !ok(&run("git", &["-C", repo.to_str().unwrap(), "remote", "add", "origin", REPO], None)) {
            let _ = run("git", &["-C", repo.to_str().unwrap(), "remote", "set-url", "origin", REPO], None);
        }
        let f = run("git", &["-C", repo.to_str().unwrap(), "fetch", "--depth", "1", "origin", "main"], None);
        let _ = run("git", &["-C", repo.to_str().unwrap(), "checkout", "-f", "-B", "main", "origin/main"], None);
        if !ok(&f) {
            say("Could not update (offline?) — using the existing runtime.");
        }
    } else {
        say("Downloading the agency runtime (first run, a few minutes)…");
        let r = run("git", &["clone", "--depth", "1", REPO, repo.to_str().unwrap()], None);
        if !ok(&r) {
            return Err("Could not download the runtime (git clone failed — check your connection).".into());
        }
    }

    // 4) install the Python env with the agency extra (cockpit deps).
    say("Installing dependencies (uv sync — this can take a few minutes the first time)…");
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

/// Update the splash window's status line (best-effort).
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
                match provision(&say) {
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

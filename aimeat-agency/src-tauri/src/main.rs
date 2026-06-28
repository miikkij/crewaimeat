// aimeat-agency — thin Tauri shell. It spawns the Python cockpit (crewaimeat.agency.cockpit) as a
// managed child, waits for it, then points the window at it. All product logic lives in the cockpit.
#![cfg_attr(all(not(debug_assertions), target_os = "windows"), windows_subsystem = "windows")]

use std::process::{Child, Command};
use std::sync::Mutex;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use tauri::Manager;

/// Holds the spawned cockpit child so we can reap it on exit.
struct Cockpit(Mutex<Option<Child>>);

const PORT: u16 = 8753;

/// The crewfive repo root. DEV: aimeat-agency/ sits inside the repo, so the parent of cwd is the repo.
/// PACKAGED (later): this is replaced by the bundled runtime dir + staged uv/node sidecars.
fn repo_root() -> std::path::PathBuf {
    std::env::current_dir()
        .ok()
        .and_then(|p| p.parent().map(|x| x.to_path_buf()))
        .unwrap_or_else(|| std::path::PathBuf::from("."))
}

/// Start the cockpit. DEV uses `uv run`; a packaged build swaps this for the staged uv sidecar driving
/// the bundled crewaimeat package. The token gates /api/*; AIMEAT_HOME is pinned to the repo's .aimeat.
fn spawn_cockpit(token: &str) -> std::io::Result<Child> {
    let root = repo_root();
    Command::new("uv")
        .args(["run", "--extra", "agency", "python", "-m", "crewaimeat.agency.cockpit"])
        .current_dir(&root)
        .env("AIMEAT_HOME", root.join(".aimeat"))
        .env("AIMEAT_AGENCY_TOKEN", token)
        .env("AIMEAT_AGENCY_PORT", PORT.to_string())
        .spawn()
}

/// Poll until the cockpit accepts TCP connections (it's up) or we give up.
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

fn main() {
    // A per-launch token the cockpit serves into the page; the page carries it on every /api call.
    let nanos = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0);
    let token = format!("{:x}{:x}", nanos, std::process::id());

    tauri::Builder::default()
        .manage(Cockpit(Mutex::new(None)))
        .setup(move |app| {
            let win = app.get_webview_window("splash").expect("splash window missing");
            let child = spawn_cockpit(&token).expect("failed to start the cockpit");
            *app.state::<Cockpit>().0.lock().unwrap() = Some(child);

            // Off the main thread: wait for the cockpit, then redirect the splash window to it.
            let url = format!("http://127.0.0.1:{}/", PORT);
            std::thread::spawn(move || {
                if wait_up(Duration::from_secs(90)) {
                    let _ = win.eval(&format!("window.location.replace('{}')", url));
                } else {
                    let _ = win.eval(
                        "var m=document.querySelector('.muted'); if(m) m.textContent='the cockpit did not start — check the terminal/logs';",
                    );
                }
            });
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building aimeat-agency")
        .run(|app_handle, event| {
            // Reap the cockpit on quit so it never lingers holding the port / the venv.
            if let tauri::RunEvent::ExitRequested { .. } = event {
                if let Some(mut child) = app_handle.state::<Cockpit>().0.lock().unwrap().take() {
                    let _ = child.kill();
                }
            }
        });
}

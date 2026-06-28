# aimeat-agency — the desktop appliance

A downloadable desktop app for non-developers to run an **agency of agents** on the AIMEAT substrate:
install → connect your account → pick a brain → tune it → run → watch it work → publish refined output.

It is a **thin Tauri shell over the local Python cockpit** (`crewaimeat.agency.cockpit`). The shell does
almost nothing itself — it spawns the cockpit and shows it in a window. All the product logic lives in
the cockpit (reusing the crewfive read models: brains, fleet, memory, sync, offerings), so we write it
once, in Python. (Contrast aimeat-desktop, which runs a full local *node* — we do not.)

```
┌────────────────────────── Tauri shell (Rust) ──────────────────────────┐
│  • spawns the cockpit (Python) as a managed child, with a per-launch     │
│    token + a free port + AIMEAT_HOME pinned                              │
│  • waits for http://127.0.0.1:<port>/healthz                            │
│  • opens a webview window at the cockpit URL                            │
│  • kills the cockpit on exit; system tray; auto-update                  │
└────────────────────────────────────────────────────────────────────────┘
         │ http (127.0.0.1, token-gated)
┌────────▼──────────── cockpit  (crewaimeat.agency.cockpit, FastAPI) ─────┐
│  Gallery · Manage (brain editor, dry-run, test-run, tasks, history,      │
│  logs, offering) · Fleet · Memory · Sync — the whole UI                  │
└────────────────────────────────────────────────────────────────────────┘
         │ runs / reads
   the crewaimeat fleet (serve daemon + crew daemons) + aimeat.io
```

## What's here (Slice 1 foundation)

- `src-tauri/` — the Rust shell: spawns the cockpit, opens the window, tray + updater, reaps on exit.
- `tauri.conf.json`, `package.json` — Tauri 2.x config + build scripts.

## Run / build

**Dev (fastest loop):** just run the cockpit directly and open it in a browser — no Tauri needed:

```
# from the crewfive repo root
$env:AIMEAT_HOME = 'E:\dev\GitHub\crewfive\.aimeat'
uv run --extra agency python -m crewaimeat.agency.cockpit
# open the printed http://127.0.0.1:8753/
```

**The app (Tauri shell):**

```
cd aimeat-agency
pnpm install
pnpm tauri dev       # spawns the cockpit + opens the window
pnpm tauri build     # NSIS/MSI installer under src-tauri/target/release/bundle/
```

## First-run provisioning (implemented, the aimeat-desktop way)

On first launch the Rust shell PROVISIONS the runtime the same way aimeat-desktop does — it does NOT
bundle Python. In `src-tauri/src/main.rs`:

1. ensure `uv` (install via the official script if missing),
2. `git clone --depth 1` the crewaimeat repo into `%LOCALAPPDATA%\aimeat-agency\crewaimeat` (or update
   it in place on later runs),
3. `uv sync --extra agency` (installs the cockpit + crewaimeat),
4. spawn the cockpit (`uv run … crewaimeat.agency.cockpit`) from that checkout and point the window at it.

The splash shows progress. Requires `git` on the machine (reported if missing). This mirrors
`aimeat-desktop/src-tauri/resources/provision.mjs`, reimplemented directly in Rust (no `node` sidecar).

## Still to do

1. **Non-dev git/offline** — bundle a tarball download fallback so `git` isn't required, and an offline
   wheelhouse so first-run needs no network.
2. **Tray + auto-update** — copy from aimeat-desktop (`tray.rs`, the updater plugin + signing key).
3. **Code-signing** (open decision) before public release — unsigned → SmartScreen.

The cockpit is feature-complete for the v1 operator experience; this shell is the packaging.

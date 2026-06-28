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

## First-run provisioning (self-contained — no git, no dev setup)

The installer **bundles** the crewaimeat source snapshot + the `uv` binary (Tauri `resources` +
`externalBin`, staged in CI). On first launch `src-tauri/src/main.rs`:

1. copies the bundled source to `%LOCALAPPDATA%\aimeat-agency\crewaimeat` (first run only),
2. `uv sync --extra agency` with the bundled `uv` (uv fetches Python + the deps — internet that first
   run only; no git, no pre-installed tools),
3. spawns the cockpit (`uv run … crewaimeat.agency.cockpit`) from that checkout and points the window at it.

The splash shows progress.

## Guided onboarding (in the cockpit)

The cockpit opens on a **step-by-step wizard** (`renderSetup`) that shows the whole checklist up front and
gates each step on the previous: **account → AI brain → first agent → approve → start**. The default model
is **local Ollama + gemma4** (free/private, with in-app install + download guidance and plain-language
"what it means"); **OpenRouter** is an optional "advanced" path (explained, with a key field). Device-auth
is smoothed: it opens aimeat.io for you, copies the code, and auto-detects approval. Backed by
`/api/setup/status`, `/api/ollama/pull`, `/api/setup/openrouter-key`.

## Still to do

1. **Offline wheelhouse** — so the first run needs no network at all.
2. **Tray + auto-update** — copy from aimeat-desktop (`tray.rs`, the updater plugin + signing key).
3. **Code-signing** (open decision) before public release — unsigned → SmartScreen.
4. **External-link opening in the packaged app** — the wizard uses `window.open` for ollama.com /
   openrouter.ai / the device-auth URL; confirm Tauri routes these to the default browser (opener plugin
   if not).

The cockpit is feature-complete for the v1 operator experience; this shell is the packaging.

#!/usr/bin/env bash
# start_fleet.sh — bring the whole crewaimeat fleet up (the counterpart to terminate_fleet.sh).
#
# Usage:   ./scripts/start_fleet.sh
#
# 1) uv sync                              — make the venv match pyproject/uv.lock
# 2) ensure the shared serve daemon + supervisor (the forward tunnel + auto-restart)
# 3) start the fleet HOST in THIS terminal — every agent as a thread in ONE process
#
# MEMORY-LIGHT BY DEFAULT (since 0.5.0): no longer one OS process per crew (which imported crewai
# ~N times and cost several GB). Runs the **fleet host** — every approved agent as a thread in ONE
# Python process, crewai imported once — ~20x less RAM for I/O-bound work. Ctrl+C stops the WHOLE
# fleet. Legacy per-process model: start crew-forge directly (bash scripts/watchdog.sh crews/crew_forge_crew.py).
# (Only APPROVED agents come online; an unapproved one waits for its device-flow approval.)
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

# Pin the AIMEAT connector home to THIS repo (isolated from other projects' fleets on the machine, so
# two `aimeat connect serve` daemons can never collide on one global ~/.aimeat/serve.json). Every child
# inherits it — ensure_serve's daemon, the serve-watchdog, and crew-forge -> reconcile_fleet -> each
# detached crew — so all fleet processes resolve the SAME serve.json/tokens regardless of cwd. An
# explicitly preset AIMEAT_HOME wins (same precedence as the connector). aimeat-crewai>=0.6.0 resolves
# the home per-directory, so WITHOUT this pin a fleet started from /opt/... would look for serve.json
# there instead of where the tokens live, and every crew would crash "No live serve daemon found".
: "${AIMEAT_HOME:="$root/.aimeat"}"; export AIMEAT_HOME
echo "[start_fleet] AIMEAT_HOME = $AIMEAT_HOME"

# Put the venv bin first on PATH so `uv` (and the watchdog's `uv run`) resolve even if the
# shell's PATH lacks uv.
[ -d "$root/.venv/bin" ] && export PATH="$root/.venv/bin:$PATH"

echo "[start_fleet] uv sync ..."
uv sync

# Start the SHARED loopback serve daemon once, before any crew. Every crew attaches to this one
# daemon (serve.json discovery): all MCP + deterministic calls multiplex over one persistent
# WebSocket per agent to the node — no per-call subprocess/TLS. ensure_serve is idempotent
# (pid-guarded), so this simply adopts an already-running daemon. Crews can also auto-start it,
# but doing it here once avoids a 30-crew thundering-herd on a cold boot — and crews launched with
# auto_start=False crash without it.
echo "[start_fleet] ensuring the shared loopback serve daemon (aimeat connect serve --http) ..."
uv run python "$root/scripts/ensure_serve.py"

# Supervise that daemon. It is the fleet's single point of failure — if it ever dies nothing else
# restarts it and the WHOLE fleet's tunnel goes down silently. The supervisor calls the idempotent
# ensure_serve on a timer, so a crashed daemon comes back in seconds and never double-spawns.
# Detached + single-instance.
mkdir -p "$root/logs"
echo "[start_fleet] starting the serve-daemon supervisor (auto-restarts the shared tunnel) ..."
nohup bash "$root/scripts/serve_watchdog.sh" >"$root/logs/serve_watchdog.log" 2>&1 &

# Run the fleet HOST: every agent as a thread in ONE process (crewai imported once) — ~20x less RAM
# than one process per crew. crew-forge is excluded and reconcile_fleet no-ops under AIMEAT_FLEET_HOST,
# so nothing spawns a shadow per-process fleet. The host stays in THIS window; Ctrl+C stops the fleet.
echo "[start_fleet] starting the fleet HOST (all agents as threads in ONE process — memory-light) ..."
echo "[start_fleet] the host stays in THIS window; Ctrl+C stops the WHOLE fleet."
exec uv run python -m crewaimeat.fleet_host

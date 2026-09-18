#!/usr/bin/env bash
# start_fleet.sh — bring the whole crewaimeat fleet up (the counterpart to terminate_fleet.sh).
#
# Usage:   ./scripts/start_fleet.sh
#
# 1) uv sync                              — make the venv match pyproject/uv.lock
# 2) the aimeat connector at npm latest   — upgraded before the serve daemon starts
# 3) ensure the shared serve daemon + supervisor (the forward tunnel + auto-restart)
# 4) start the SPAWNER under its supervisor — agents the node marks run_mode=spawn
# 5) start the fleet HOST in THIS terminal — every other agent as a thread in ONE process
# 6) when the host returns (an all-spawn roster returns at once), follow the spawner's log
#
# The same sequence as start_fleet.ps1. Which runtime serves an agent is the NODE's run_mode, read by
# both halves from the same source. Ctrl+C in the host stops resident threads only; the serve daemon,
# its supervisor and the spawner are detached and keep running — stop everything with
# ./scripts/terminate_fleet.sh. Legacy per-process model: start crew-forge directly
# (bash scripts/watchdog.sh crews/crew_forge_crew.py).
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
# The fleet runs the NEWEST aimeat connector (owner's rule, 2026-09-18) — upgraded here, before the serve
# daemon starts; a running daemon is never upgraded underneath. Exit 4 = below the floor: stop.
echo "[start_fleet] aimeat connector: npm latest / installed / repo pin ..."
rc=0; uv run crewaimeat connector --install || rc=$?
if [ "$rc" -eq 4 ]; then echo "[start_fleet] aimeat connector is below the floor - see above" >&2; exit 4; fi
echo "[start_fleet] ensuring the shared loopback serve daemon (aimeat connect serve --http) ..."
uv run python "$root/scripts/ensure_serve.py"

# Supervise that daemon. It is the fleet's single point of failure — if it ever dies nothing else
# restarts it and the WHOLE fleet's tunnel goes down silently. The supervisor calls the idempotent
# ensure_serve on a timer, so a crashed daemon comes back in seconds and never double-spawns.
# Detached + single-instance.
mkdir -p "$root/logs"
echo "[start_fleet] starting the serve-daemon supervisor (auto-restarts the shared tunnel) ..."
nohup bash "$root/scripts/serve_watchdog.sh" >"$root/logs/serve_watchdog.log" 2>&1 &

# The SPAWNER: the runtime for agents the node marks run_mode=spawn. The host skips exactly those, so
# without this they run nowhere and nothing says so. Detached + single-instance (it holds a lock).
echo "[start_fleet] starting the spawner (agents the node marks run_mode=spawn) ..."
nohup bash "$root/scripts/spawner_watchdog.sh" >"$root/logs/spawner_watchdog.log" 2>&1 &

# Run the fleet HOST for every agent the node has NOT marked spawn: threads in ONE process (crewai
# imported once). crew-forge's reconcile_fleet no-ops under AIMEAT_FLEET_HOST, so nothing spawns a
# shadow per-process fleet. With an all-spawn roster the host has nothing to run and returns at once.
echo "[start_fleet] starting the fleet HOST (crews the node has NOT marked run_mode=spawn) ..."
echo "[start_fleet] with an all-spawn fleet the host has no roster and exits at once - the fleet is still up."
echo "[start_fleet] while any agent is resident the host stays in THIS window; Ctrl+C stops resident threads only."
rc=0; uv run python -m crewaimeat.fleet_host || rc=$?
echo "[start_fleet] host returned ($rc). Serve daemon, its supervisor and the spawner keep running (detached)."
echo "[start_fleet] stop everything with: ./scripts/terminate_fleet.sh"

# Keep watching the fleet in this window: with every agent in spawn mode the host returns in seconds,
# and the spawner is where the work shows — what wakes, what runs, what exits.
spawner_log="$root/logs/spawner_watchdog.log"
if [ -f "$spawner_log" ]; then
    echo
    echo "[start_fleet] following the spawner - what wakes, what runs, what exits."
    echo "[start_fleet] Ctrl+C stops WATCHING only; the fleet keeps running."
    echo
    exec tail -n 20 -f "$spawner_log"
else
    echo "[start_fleet] no spawner log yet - the fleet is up; watch logs/spawner_watchdog.log"
fi

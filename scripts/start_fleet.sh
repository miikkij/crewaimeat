#!/usr/bin/env bash
# start_fleet.sh — bring the whole crewaimeat fleet up (the counterpart to terminate_fleet.sh).
#
# Usage:   ./scripts/start_fleet.sh
#
# 1) uv sync                              — make the venv match pyproject/uv.lock
# 2) start crew-forge under the watchdog  — in THIS terminal (foreground)
#
# Starting the fleet = crew-forge's idempotent reconcile (in code), not a dumb start-all.
# crew-forge calls reconcile_fleet() ON STARTUP (crews/crew_forge_crew.py): it launches every
# APPROVED crew under its own watchdog (detached) and SKIPS the ones already running — so the
# whole fleet comes up. crew-forge stays in the foreground here; Ctrl+C stops ONLY crew-forge
# (the crews it launched keep running). Re-trigger any time with crew-forge's /startall, or:
#   uv run python -c "from dotenv import load_dotenv; load_dotenv(); from crewaimeat.forge import reconcile_fleet; print(reconcile_fleet())"
# (Unapproved/forged crews are reported, not auto-started — they need owner device-flow approval.)
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

# Put the venv bin first on PATH so `uv` (and the watchdog's `uv run`) resolve even if the
# shell's PATH lacks uv.
[ -d "$root/.venv/bin" ] && export PATH="$root/.venv/bin:$PATH"

echo "[start_fleet] uv sync ..."
uv sync

echo "[start_fleet] starting crew-forge under the watchdog (it reconciles the fleet on startup) ..."
echo "[start_fleet] crew-forge stays in THIS window; other crews launch detached. Ctrl+C stops only crew-forge."
exec bash "$root/scripts/watchdog.sh" crews/crew_forge_crew.py

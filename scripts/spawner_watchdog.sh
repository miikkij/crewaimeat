#!/usr/bin/env bash
# spawner_watchdog.sh — supervise the SPAWNER on Linux/macOS. The counterpart to spawner_watchdog.ps1.
#
# The spawner is the runtime for agents the node lists as run_mode=spawn. Without it those agents run
# NOWHERE: the fleet host deliberately skips whatever the node marks as spawn (or both runtimes would
# start the same agent and the OS lock would pick a winner arbitrarily), so if the spawner is not up, a
# spawn-mode agent is simply absent and nothing says so.
#
# Mirrors serve_watchdog.sh: same AIMEAT_HOME pinning; the spawner holds its own singleton lock, so a
# second one exits rather than fighting for the same agents' wake channels. Launch detached; start_fleet
# logs it to logs/spawner_watchdog.log.
#
# An EMPTY roster is a normal, quiet state — nobody has said `spawn` about any agent yet. The spawner
# parks on nothing and costs nothing.
set -u
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
: "${AIMEAT_HOME:="$root/.aimeat"}"; export AIMEAT_HOME
[ -d "$root/.venv/bin" ] && export PATH="$root/.venv/bin:$PATH"

while true; do
    uv run python -m crewaimeat.scaffold spawner
    code=$?
    # Exit 2 is the daemon's "token rejected" — re-running would hot-loop against a dead credential.
    if [ "$code" -eq 2 ]; then
        echo "[spawner_watchdog] spawner exited 2 (credential rejected) - NOT restarting. Re-approve, then start the fleet again."
        break
    fi
    echo "[spawner_watchdog] spawner exited $code - restarting in 20s"
    sleep 20
done

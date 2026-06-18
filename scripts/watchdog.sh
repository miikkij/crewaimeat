#!/usr/bin/env bash
# watchdog.sh — keep an AIMEAT crew running (macOS / Linux).
#
# Usage:  ./scripts/watchdog.sh crews/<your>_crew.py
#
# The crew is a daemon: it runs forever, checking AIMEAT for queued tasks every ~30s.
# This watchdog restarts it if it ever stops. If it keeps stopping quickly (e.g. the
# agent can no longer authenticate), the watchdog pauses and points you to AIMEAT to
# re-approve the agent's token.

set -u
CREW="${1:?usage: ./scripts/watchdog.sh crews/<your>_crew.py}"

# Resolve the connector home the same way every entrypoint does, so a crew launched standalone (not
# via start_fleet) still shares the fleet's serve.json/tokens. A preset/inherited AIMEAT_HOME wins.
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${AIMEAT_HOME:="$root/.aimeat"}"; export AIMEAT_HOME
MAX_RAPID_FAILURES="${MAX_RAPID_FAILURES:-5}"   # stop after this many quick exits in a row
RAPID_WINDOW_SECONDS="${RAPID_WINDOW_SECONDS:-60}"  # an exit faster than this counts as "quick"

fails=0
while true; do
    start=$(date +%s)
    echo "[watchdog] starting $CREW ..."
    uv run python "$CREW"
    code=$?
    elapsed=$(( $(date +%s) - start ))

    if [ "$code" -eq 78 ]; then
        echo ""
        echo "[watchdog] The agent's token is no longer valid (exit 78). Stopping."
        echo "[watchdog] Re-approve it on AIMEAT (Profile -> Agents), then run this watchdog again."
        break
    fi

    if [ "$elapsed" -lt "$RAPID_WINDOW_SECONDS" ]; then fails=$((fails + 1)); else fails=0; fi
    echo "[watchdog] crew exited (code $code) after ${elapsed}s. quick-exits=${fails}/${MAX_RAPID_FAILURES}"

    if [ "$fails" -ge "$MAX_RAPID_FAILURES" ]; then
        echo ""
        echo "[watchdog] The crew keeps stopping quickly. The agent likely needs attention on AIMEAT."
        echo "[watchdog] Open the dashboard (Profile -> Agents) and approve / re-authenticate it, then run this watchdog again."
        break
    fi

    delay=$(( 5 * fails + 5 )); [ "$delay" -gt 30 ] && delay=30
    echo "[watchdog] restarting in ${delay}s ... (Ctrl+C to stop)"
    sleep "$delay"
done

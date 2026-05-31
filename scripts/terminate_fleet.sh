#!/usr/bin/env bash
# terminate_fleet.sh — stop ALL crewaimeat fleet processes on this machine (Linux/macOS).
#
# Usage:
#   ./scripts/terminate_fleet.sh             # stop everything
#   ./scripts/terminate_fleet.sh --dry-run   # just list what would be stopped
#
# Stops, in this order so nothing respawns mid-cleanup:
#   1. watchdogs            (scripts/watchdog.sh)
#   2. crew daemons         (uv run python crews/<x>_crew.py + the python it launches)
#   3. connector MCP procs  (aimeat connect serve --agent <x>)
#
# The single-instance locks under logs/.locks/ release automatically when the daemons
# die (no stale locks), so they are left alone. Re-launch the fleet afterwards with
# crew-forge's /startall, or:
#   uv run python -c "from dotenv import load_dotenv; load_dotenv(); from crewaimeat.forge import reconcile_fleet; print(reconcile_fleet())"

set -u

DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

# This script is "terminate_fleet.sh"; the watchdog is "watchdog.sh" — distinct, so the
# patterns never match this script. We also drop our own PID and the matcher itself.
list_group() {
    # $1 = ERE pattern matched against the full command line
    pgrep -af "$1" 2>/dev/null | grep -vE "terminate_fleet|pgrep|^$$ " || true
}

stop_group() {
    local label="$1" pat="$2" lines
    lines=$(list_group "$pat")
    local n=0
    [ -n "$lines" ] && n=$(printf '%s\n' "$lines" | grep -c .)
    echo "=== $label ($n) ==="
    [ -z "$lines" ] && return 0
    printf '%s\n' "$lines" | while read -r pid rest; do
        echo "  $pid  $rest"
        [ "$DRY" -eq 0 ] && kill "$pid" 2>/dev/null || true
    done
}

PAT_WATCHDOG='watchdog\.sh'
PAT_DAEMON='crews/[A-Za-z0-9_]+_crew\.py'
PAT_CONNECTOR='connect serve'

stop_group "watchdog"  "$PAT_WATCHDOG"
[ "$DRY" -eq 0 ] && sleep 1
stop_group "daemon"    "$PAT_DAEMON"
stop_group "connector" "$PAT_CONNECTOR"

if [ "$DRY" -eq 1 ]; then
    echo
    echo "[dry run] nothing was killed."
    exit 0
fi

# Force-kill any stragglers that ignored SIGTERM.
sleep 2
for pat in "$PAT_WATCHDOG" "$PAT_DAEMON" "$PAT_CONNECTOR"; do
    for p in $(list_group "$pat" | awk '{print $1}'); do
        kill -9 "$p" 2>/dev/null || true
    done
done
echo
echo "[terminate_fleet] done."

#!/usr/bin/env bash
# view_fleet.sh — show each crewaimeat crew and whether it is running (Linux/macOS).
#
# Usage:  ./scripts/view_fleet.sh
#
# For every crews/<x>_crew.py it reports: watchdog count, related process count, whether a
# single-instance lock file is present, and a derived status (running / down / orphan /
# DUPLICATE). The lock-file count at the bottom is the authoritative number of live daemons.
# Read-only — kills nothing.

set -u
root="$(cd "$(dirname "$0")/.." && pwd)"
crews="$root/crews"
locks="$root/logs/.locks"

printf '%-26s %-9s %-6s %-5s %s\n' CREW WATCHDOG PROCS LOCK STATUS
printf '%-26s %-9s %-6s %-5s %s\n' '----' '--------' '-----' '----' '------'

shopt -s nullglob
for f in "$crews"/*_crew.py; do
    fname="$(basename "$f")"
    case "$fname" in _*) continue;; esac
    agent="$(grep -oE 'AGENT_NAME[[:space:]]*=[[:space:]]*"[^"]+"' "$f" | head -1 | sed -E 's/.*"([^"]+)"/\1/')"
    [ -z "$agent" ] && agent="${fname%_crew.py}"

    wd=$(pgrep -af 'watchdog\.sh' 2>/dev/null | grep -F "$fname" | grep -c . || true)
    dae=$(pgrep -af "$fname" 2>/dev/null | grep -vE 'watchdog\.sh|view_fleet|pgrep' | grep -c . || true)
    if [ -f "$locks/$agent.lock" ]; then lock=yes; else lock=no; fi

    if   [ "$wd" -gt 1 ];                       then st="DUPLICATE ($wd watchdogs)"
    elif [ "$wd" -ge 1 ] && [ "$dae" -ge 1 ];   then st="running"
    elif [ "$wd" -eq 0 ] && [ "$dae" -ge 1 ];   then st="orphan (no watchdog)"
    elif [ "$lock" = yes ];                     then st="down (stale lock file)"
    else                                              st="down"
    fi
    printf '%-26s %-9s %-6s %-5s %s\n' "$agent" "$wd" "$dae" "$lock" "$st"
done

echo
conn=$(pgrep -af 'connect serve' 2>/dev/null | grep -vc 'pgrep' || true)
lockn=$(ls "$locks"/*.lock 2>/dev/null | grep -c . || true)
echo "connect serve processes: $conn"
echo "lock files (= live daemons): $lockn"

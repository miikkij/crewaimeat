#!/usr/bin/env bash
# terminate_fleet.sh — stop THIS repo's crewaimeat fleet (home/repo-scoped, Linux/macOS).
# The counterpart to terminate_fleet.ps1, and the same rules.
#
# Usage:
#   ./scripts/terminate_fleet.sh             # stop this repo's fleet
#   ./scripts/terminate_fleet.sh --dry-run   # list what would be stopped (kills nothing)
#
# HOME-AWARE: the shared serve daemon is killed ONLY if it serves THIS repo's AIMEAT_HOME — another
# project's, a sibling checkout's or the desktop app's serve must survive our shutdown. (The previous
# version of this script matched `connect serve` machine-wide and killed every one of them.) Everything
# else is scoped to THIS repo's absolute path in the command line, so another repo's fleet is never
# touched.
#
# Order matters so nothing respawns mid-cleanup:
#   1. spawner watchdog, spawner, spawn workers — the spawner restarts a worker whose run ends, so it
#      goes FIRST; killing workers under a live spawner just makes more of them
#   2. serve watchdog  (would revive the serve daemon)
#   3. fleet host, then crew watchdogs (the legacy per-process model)
#   4. serve daemon    — only THIS home's (crewaimeat.serve_guard.this_home_serve_pids)
#   5. crew-daemon sweep — any orphaned crew python left behind, repo-scoped
#
# The single-instance locks under logs/.locks/ release when their holders die (no stale locks).

set -u

DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
: "${AIMEAT_HOME:="$root/.aimeat"}"; export AIMEAT_HOME
self=$$

# "pid args" of every process whose command line matches the ERE $1 AND names THIS repo followed by a
# path separator — so a sibling clone whose name merely starts with ours ('crewfive-dev') is never
# swept up. `ps -Ao` rather than `pgrep -a`, which macOS does not have.
list_group() {
    # $2 (optional) = an ERE the line must NOT match — the sweep uses it to skip what step 3 owns
    ps -Ao pid=,args= 2>/dev/null \
        | grep -F -- "$root/" \
        | grep -E -- "$1" \
        | grep -vE -- "terminate_fleet|grep ${2:+|$2}" \
        | awk -v me="$self" '$1 != me' || true
}

total=0
stop_group() {
    local label="$1" pat="$2" not="${3:-}" lines n=0
    lines=$(list_group "$pat" "$not")
    [ -n "$lines" ] && n=$(printf '%s\n' "$lines" | grep -c .)
    echo "=== $label ($n) [repo-scoped] ==="
    total=$((total + n))
    [ -z "$lines" ] && return 0
    while read -r pid rest; do
        echo "  $pid  ${rest##*/}"
        [ "$DRY" -eq 0 ] && kill "$pid" 2>/dev/null
    done <<< "$lines"
    return 0
}

# This home's serve-daemon pids (leaves other homes' daemons alone). Empty on any error.
this_home_serve_pids() {
    local py="$root/.venv/bin/python"
    [ -x "$py" ] || py="python3"
    "$py" -c "from crewaimeat.serve_guard import this_home_serve_pids; print(' '.join(map(str, this_home_serve_pids())))" 2>/dev/null || true
}

PAT_SPAWNER_WD='spawner_watchdog'
PAT_SPAWNER='scaffold[[:space:]]+spawner|crewaimeat[. ]spawner'
PAT_WORKER='crewaimeat[. ]run_once'
PAT_SERVE_WD='serve_watchdog'
PAT_HOST='fleet_host'
PAT_WATCHDOG='scripts/watchdog\.sh'
PAT_DAEMON='crews/[A-Za-z0-9_]+_crew\.py'

stop_group "spawner-watchdog" "$PAT_SPAWNER_WD"
stop_group "spawner"          "$PAT_SPAWNER"
stop_group "spawn-worker"     "$PAT_WORKER"
stop_group "serve-watchdog"   "$PAT_SERVE_WD"
stop_group "fleet-host"       "$PAT_HOST"
stop_group "watchdog"         "$PAT_WATCHDOG"
[ "$DRY" -eq 0 ] && sleep 1

serve_pids=$(this_home_serve_pids)
n=0; for _ in $serve_pids; do n=$((n + 1)); done
echo "=== serve-daemon ($n) [this AIMEAT_HOME only: $AIMEAT_HOME] ==="
for sp in $serve_pids; do
    echo "  $sp  serve (this home)"
    [ "$DRY" -eq 0 ] && kill "$sp" 2>/dev/null
done
total=$((total + n))

stop_group "crew-daemon sweep" "$PAT_DAEMON" "$PAT_WATCHDOG"

if [ "$DRY" -eq 1 ]; then
    echo
    echo "[dry run] $total process(es) would be stopped (THIS repo + THIS home only). Nothing was killed."
    exit 0
fi

# Force-kill stragglers that ignored SIGTERM — still repo- and home-scoped.
sleep 2
left=0
for pat in "$PAT_SPAWNER_WD" "$PAT_SPAWNER" "$PAT_WORKER" "$PAT_SERVE_WD" "$PAT_HOST" "$PAT_WATCHDOG" "$PAT_DAEMON"; do
    for p in $(list_group "$pat" | awk '{print $1}'); do
        kill -9 "$p" 2>/dev/null && left=$((left + 1))
    done
done
for sp in $(this_home_serve_pids); do
    kill -9 "$sp" 2>/dev/null && left=$((left + 1))
done
echo
echo "[terminate_fleet] done. Stopped $total; leftover sweep killed $left."

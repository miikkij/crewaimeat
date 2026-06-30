# watchdog.ps1 — keep an AIMEAT crew running (Windows).
#
# Usage:  ./scripts/watchdog.ps1 crews/<your>_crew.py
#
# The crew is a daemon: it runs forever, checking AIMEAT for queued tasks every ~30s.
# This watchdog restarts it if it ever stops. If it keeps stopping quickly (e.g. the
# agent can no longer authenticate), the watchdog pauses and points you to AIMEAT to
# re-approve the agent's token.

param(
    [Parameter(Mandatory = $true)][string]$Crew,
    [int]$MaxRapidFailures = 5,      # stop after this many quick exits in a row
    [int]$RapidWindowSeconds = 60    # an exit faster than this counts as "quick"
)

# Resolve the connector home the same way every entrypoint does, so a crew launched standalone (not via
# start_fleet) still shares the fleet's serve.json/tokens. A preset/inherited AIMEAT_HOME wins.
$root = (Resolve-Path "$PSScriptRoot\..").Path
if (-not $env:AIMEAT_HOME) { $env:AIMEAT_HOME = Join-Path $root '.aimeat' }

# Timestamp every line so the log reads as a timeline (when each crew start/exit/restart happened, and
# when each crew line was emitted) — `Log` for the watchdog's own markers, `Add-Stamp` for piped crew
# output. Local time, sortable format. NB the crew's stderr lines (tracebacks, [agent]/[llm] prints) are
# unbuffered, so their stamps are accurate; buffered stdout may batch.
function Log($msg) { Write-Host ("{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg) }
filter Add-Stamp { "{0} {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $_ }

$fails = 0
while ($true) {
    $start = Get-Date
    Log "[watchdog] starting $Crew ..."
    # 2>&1 | Add-Stamp prepends a timestamp to each crew line; $LASTEXITCODE still reflects `uv` (a native
    # command at the head of the pipe), so the re-auth/quick-exit logic below is unaffected.
    uv run python $Crew 2>&1 | Add-Stamp
    $code = $LASTEXITCODE
    $elapsed = [int]((Get-Date) - $start).TotalSeconds

    # 78 = our startup re-auth exit; 2 = the daemon's own auth_revoked exit (aimeat-crewai 0.7.0, the node
    # pushed auth_revoked). Either way the token needs re-approval — stop, don't crash-loop.
    if ($code -eq 78 -or $code -eq 2) {
        Write-Host ""
        Log "[watchdog] The agent's token is no longer valid (exit $code). Stopping."
        Log "[watchdog] Re-approve it on AIMEAT (Profile -> Agents), then run this watchdog again."
        break
    }

    if ($elapsed -lt $RapidWindowSeconds) { $fails++ } else { $fails = 0 }
    Log "[watchdog] crew exited (code $code) after ${elapsed}s. quick-exits=$fails/$MaxRapidFailures"

    if ($fails -ge $MaxRapidFailures) {
        Write-Host ""
        Log "[watchdog] The crew keeps stopping quickly. The agent likely needs attention on AIMEAT."
        Log "[watchdog] Open the dashboard (Profile -> Agents) and approve / re-authenticate it, then run this watchdog again."
        break
    }

    $delay = [Math]::Min(30, 5 * $fails + 5)
    Log "[watchdog] restarting in ${delay}s ... (Ctrl+C to stop)"
    Start-Sleep -Seconds $delay
}

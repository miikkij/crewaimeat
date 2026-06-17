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

$fails = 0
while ($true) {
    $start = Get-Date
    Write-Host "[watchdog] starting $Crew ..."
    uv run python $Crew
    $code = $LASTEXITCODE
    $elapsed = [int]((Get-Date) - $start).TotalSeconds

    if ($code -eq 78) {
        Write-Host ""
        Write-Host "[watchdog] The agent's token is no longer valid (exit 78). Stopping."
        Write-Host "[watchdog] Re-approve it on AIMEAT (Profile -> Agents), then run this watchdog again."
        break
    }

    if ($elapsed -lt $RapidWindowSeconds) { $fails++ } else { $fails = 0 }
    Write-Host "[watchdog] crew exited (code $code) after ${elapsed}s. quick-exits=$fails/$MaxRapidFailures"

    if ($fails -ge $MaxRapidFailures) {
        Write-Host ""
        Write-Host "[watchdog] The crew keeps stopping quickly. The agent likely needs attention on AIMEAT."
        Write-Host "[watchdog] Open the dashboard (Profile -> Agents) and approve / re-authenticate it, then run this watchdog again."
        break
    }

    $delay = [Math]::Min(30, 5 * $fails + 5)
    Write-Host "[watchdog] restarting in ${delay}s ... (Ctrl+C to stop)"
    Start-Sleep -Seconds $delay
}

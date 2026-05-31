# terminate_fleet.ps1 — stop ALL crewaimeat fleet processes on this machine (Windows).
#
# Usage:
#   ./scripts/terminate_fleet.ps1            # stop everything
#   ./scripts/terminate_fleet.ps1 -DryRun    # just list what would be stopped
#
# It stops, in this order so nothing respawns mid-cleanup:
#   1. watchdogs            (powershell running scripts/watchdog.ps1)
#   2. crew daemons         (uv run python crews/<x>_crew.py + the python it launches)
#   3. connector MCP procs  (aimeat connect serve --agent <x>)
#
# The single-instance locks under logs/.locks/ release automatically when the daemons
# die (no stale locks), so they are left alone. Re-launch the fleet afterwards with
# crew-forge's /startall, or:
#   uv run python -c "from dotenv import load_dotenv; load_dotenv(); from crewaimeat.forge import reconcile_fleet; print(reconcile_fleet())"

[CmdletBinding()]
param([switch]$DryRun)

# Each group matched separately so we can kill watchdogs first. The watchdog filename is
# singular ("watchdog.ps1"), so this script ("terminate_fleet.ps1") never matches itself;
# we also exclude our own PID for safety.
$groups = [ordered]@{
    'watchdog'  = 'scripts[\\/]watchdog\.ps1'
    'daemon'    = 'crews[\\/][A-Za-z0-9_]+_crew\.py'
    'connector' = 'connect\s+serve'
}

function Get-FleetProcs([string]$pattern) {
    Get-CimInstance Win32_Process | Where-Object {
        $_.ProcessId -ne $PID -and $_.CommandLine -and
        $_.CommandLine -match $pattern -and
        $_.CommandLine -notmatch 'terminate_fleet'
    }
}

$total = 0
foreach ($name in $groups.Keys) {
    $procs = @(Get-FleetProcs $groups[$name])
    Write-Host "=== $name ($($procs.Count)) ==="
    foreach ($p in $procs) {
        $short = ($p.CommandLine -split '[\\/ ]')[-1]
        Write-Host ("  {0,-7} {1}" -f $p.ProcessId, $short)
        if (-not $DryRun) {
            try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch {}
        }
    }
    $total += $procs.Count
    if (-not $DryRun -and $name -eq 'watchdog' -and $procs.Count) { Start-Sleep -Seconds 1 }
}

if ($DryRun) {
    Write-Host "`n[dry run] $total process(es) would be stopped. Nothing was killed."
    return
}

Start-Sleep -Seconds 2
$left = 0
foreach ($pattern in $groups.Values) { $left += @(Get-FleetProcs $pattern).Count }
if ($left -gt 0) {
    Write-Host "`n[terminate_fleet] $left straggler(s) remain; retrying once ..."
    foreach ($pattern in $groups.Values) {
        Get-FleetProcs $pattern | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {} }
    }
    Start-Sleep -Seconds 1
    $left = 0
    foreach ($pattern in $groups.Values) { $left += @(Get-FleetProcs $pattern).Count }
}
Write-Host "`n[terminate_fleet] done. Stopped $total, remaining: $left."

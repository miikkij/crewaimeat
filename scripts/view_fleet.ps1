# view_fleet.ps1 — show each crewaimeat crew and whether it is running (Windows).
#
# Usage:  ./scripts/view_fleet.ps1
#
# For every crews/<x>_crew.py it reports: watchdog count, related process count, whether a
# single-instance lock file is present, and a derived status (running / down / orphan /
# DUPLICATE). The lock-file count at the bottom is the authoritative number of live daemons
# (the single-instance lock guarantees one daemon per agent). Read-only — kills nothing.

$root = Split-Path $PSScriptRoot -Parent
$crewsDir = Join-Path $root 'crews'
$locksDir = Join-Path $root 'logs\.locks'

$procs = @(Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and
    ($_.CommandLine -match 'watchdog\.ps1|crews[\\/][A-Za-z0-9_]+_crew\.py|connect serve') -and
    $_.CommandLine -notmatch 'view_fleet|terminate_fleet|Get-CimInstance'
})

$crewFiles = @(Get-ChildItem $crewsDir -Filter '*_crew.py' -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -notlike '_*' })

$rows = foreach ($f in $crewFiles) {
    $fname = $f.Name
    $txt = Get-Content $f.FullName -Raw
    $agent = if ($txt -match 'AGENT_NAME\s*=\s*["'']([^"'']+)["'']') { $Matches[1] } else { $fname -replace '_crew\.py$', '' }
    $esc = [regex]::Escape($fname)
    $wd  = @($procs | Where-Object { $_.CommandLine -match 'watchdog\.ps1' -and $_.CommandLine -match $esc })
    $dae = @($procs | Where-Object { $_.CommandLine -notmatch 'watchdog\.ps1' -and $_.CommandLine -match $esc })
    $lock = Test-Path (Join-Path $locksDir "$agent.lock")
    $status =
        if ($wd.Count -gt 1) { "DUPLICATE ($($wd.Count) watchdogs)" }
        elseif ($wd.Count -eq 1 -and $dae.Count -ge 1) { 'running' }
        elseif ($wd.Count -eq 0 -and $dae.Count -ge 1) { 'orphan (no watchdog)' }
        elseif ($lock) { 'down (stale lock file)' }
        else { 'down' }
    [pscustomobject]@{
        Crew     = $agent
        Watchdog = $wd.Count
        Procs    = $dae.Count
        Lock     = $(if ($lock) { 'yes' } else { 'no' })
        Status   = $status
    }
}
if ($rows) { $rows | Sort-Object Crew | Format-Table -AutoSize } else { Write-Host 'No crew files in crews/.' }

# Crews running with no crew file left on disk (e.g. a deleted crew still alive).
$known = $crewFiles.Name
$running = $procs | ForEach-Object { if ($_.CommandLine -match 'crews[\\/]([A-Za-z0-9_]+_crew\.py)') { $Matches[1] } } | Sort-Object -Unique
$zombies = @($running | Where-Object { $known -notcontains $_ })
if ($zombies.Count) { Write-Host ("ZOMBIE crews (running, no crew file): " + ($zombies -join ', ')) }

$totWd = @($procs | Where-Object { $_.CommandLine -match 'watchdog\.ps1' }).Count
$conn  = @($procs | Where-Object { $_.CommandLine -match 'connect serve' }).Count
$lockN = @(Get-ChildItem (Join-Path $locksDir '*.lock') -ErrorAction SilentlyContinue).Count
Write-Host ""
Write-Host "summary: $($crewFiles.Count) crew files | $totWd watchdogs | $conn connectors | $lockN lock files (= live daemons)"

# terminate_fleet.ps1 — stop THIS repo's crewaimeat fleet (home/repo-scoped, Windows).
#
# Usage:
#   ./scripts/terminate_fleet.ps1            # stop this repo's fleet
#   ./scripts/terminate_fleet.ps1 -DryRun    # list what would be stopped (kills nothing)
#
# HOME-AWARE (aimeat-crewai >= 0.6.0): the shared serve daemon is killed ONLY if it serves THIS repo's
# AIMEAT_HOME — a different project's / the desktop's ISOLATED serve must survive our shutdown (else the
# old machine-wide `connect serve` match started a cross-home reconnect war). Crews, watchdogs and the
# serve-watchdog are scoped to THIS repo's absolute path, so another repo's fleet is never touched.
#
# Order matters so nothing respawns mid-cleanup:
#   1. serve-watchdog  (would revive the serve daemon) — tree-killed FIRST
#   2. crew watchdogs  — tree-killed (/T takes each crew's uv -> venv-python -> c:\python312 child tree)
#   3. serve daemon    — only THIS home's (resolved via crewaimeat.serve_guard.this_home_serve_pids)
#   4. crew-daemon sweep — any orphaned crew python left behind, repo-scoped
#
# The single-instance locks under logs/.locks/ release when their holders die (no stale locks).

[CmdletBinding()]
param([switch]$DryRun)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path "$PSScriptRoot\..").Path
# Match this repo's root FOLLOWED BY a path separator, so a sibling clone whose name merely starts
# with ours (e.g. 'crewfive' vs 'crewfive-dev') is never swept up — its daemons carry '<root>-dev\…'.
$rootEsc = [regex]::Escape($root) + '[\\/]'
if (-not $env:AIMEAT_HOME) { $env:AIMEAT_HOME = Join-Path $root '.aimeat' }

# Procs whose command line matches $pattern AND was launched from THIS repo ($root in the cmdline) —
# never another repo's. Excludes self and this script.
function Get-RepoProcs([string]$pattern) {
    Get-CimInstance Win32_Process | Where-Object {
        $_.ProcessId -ne $PID -and $_.CommandLine -and
        $_.CommandLine -match $pattern -and $_.CommandLine -match $rootEsc -and
        $_.CommandLine -notmatch 'terminate_fleet'
    }
}

# This home's serve-daemon pids (home-scoped — leaves other homes' daemons alone). Empty on any error.
function Get-ThisHomeServePids() {
    try {
        $out = & "$root\.venv\Scripts\python.exe" -c "from crewaimeat.serve_guard import this_home_serve_pids; print(' '.join(map(str, this_home_serve_pids())))" 2>$null
        return @($out -split '\s+' | Where-Object { $_ -match '^\d+$' } | ForEach-Object { [int]$_ })
    } catch { return @() }
}

$total = 0

# 1 + 2: serve-watchdog then crew watchdogs — repo-scoped, tree-killed (/T removes the child trees).
foreach ($grp in @(
    @{ name = 'serve-watchdog'; pat = 'serve_watchdog' },
    @{ name = 'watchdog';       pat = 'scripts[\\/]watchdog\.ps1' }
)) {
    $procs = @(Get-RepoProcs $grp.pat)
    Write-Host "=== $($grp.name) ($($procs.Count)) [repo-scoped] ==="
    foreach ($p in $procs) {
        Write-Host ("  {0,-7} {1}" -f $p.ProcessId, (($p.CommandLine -split '[\\/ ]')[-1]))
        if (-not $DryRun) { & taskkill /PID $p.ProcessId /T /F *> $null }
    }
    $total += $procs.Count
    if (-not $DryRun -and $grp.name -eq 'watchdog' -and $procs.Count) { Start-Sleep -Seconds 1 }
}

# 3: serve daemon — ONLY this home's (never another AIMEAT_HOME's).
$servePids = @(Get-ThisHomeServePids)
Write-Host "=== serve-daemon ($($servePids.Count)) [this AIMEAT_HOME only: $env:AIMEAT_HOME] ==="
foreach ($sp in $servePids) {
    Write-Host ("  {0,-7} serve (this home)" -f $sp)
    if (-not $DryRun) { try { Stop-Process -Id $sp -Force -ErrorAction Stop } catch {} }
}
$total += $servePids.Count

# 4: crew-daemon sweep — orphaned crew python (repo-scoped; child trees already taken by step 2).
$crews = @(Get-RepoProcs 'crews[\\/][A-Za-z0-9_]+_crew\.py')
Write-Host "=== crew-daemon sweep ($($crews.Count)) [repo-scoped orphans] ==="
foreach ($p in $crews) {
    Write-Host ("  {0,-7} {1}" -f $p.ProcessId, (($p.CommandLine -split '[\\/ ]')[-1]))
    if (-not $DryRun) { try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop } catch {} }
}
$total += $crews.Count

if ($DryRun) {
    Write-Host "`n[dry run] $total process(es) would be stopped (THIS repo + THIS home only). Nothing was killed."
    return
}

Start-Sleep -Seconds 2
$left = 0
foreach ($pat in @('serve_watchdog', 'scripts[\\/]watchdog\.ps1', 'crews[\\/][A-Za-z0-9_]+_crew\.py')) {
    Get-RepoProcs $pat | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop } catch {}; $left++ }
}
foreach ($sp in @(Get-ThisHomeServePids)) { try { Stop-Process -Id $sp -Force -ErrorAction Stop } catch {}; $left++ }
Write-Host "`n[terminate_fleet] done. Stopped $total; leftover sweep killed $left."

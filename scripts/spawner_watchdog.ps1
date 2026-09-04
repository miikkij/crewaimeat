# Supervises the SPAWNER — the runtime for agents the node lists as run_mode=spawn. Without it those
# agents run NOWHERE: the fleet host deliberately skips whatever the node marks as spawn (or both
# runtimes would start the same agent and the OS lock would pick a winner arbitrarily), so if the
# spawner is not up, a spawn-mode agent is simply absent and nothing says so.
#
# Mirrors serve_watchdog.ps1 on purpose: same detached launch, same AIMEAT_HOME pinning, same
# single-instance discipline (the spawner holds logs/.locks/spawner.lock, so a second one exits
# rather than fighting for the same agents' wake channels).
#
# An EMPTY roster is a normal, quiet state — it means nobody has said `spawn` about any agent yet,
# which is different from saying `resident`. The spawner parks on nothing and costs nothing.
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
if (-not $env:AIMEAT_HOME) { $env:AIMEAT_HOME = Join-Path $root '.aimeat' }
$env:PATH = "$root\.venv\Scripts;" + $env:PATH

while ($true) {
    uv run python -m crewaimeat.scaffold spawner
    $code = $LASTEXITCODE
    # Exit 2 is the daemon's "token rejected" — re-running would hot-loop against a dead credential.
    if ($code -eq 2) {
        Write-Host "[spawner_watchdog] spawner exited 2 (credential rejected) - NOT restarting. Re-approve, then start the fleet again."
        break
    }
    Write-Host "[spawner_watchdog] spawner exited $code - restarting in 20s"
    Start-Sleep -Seconds 20
}

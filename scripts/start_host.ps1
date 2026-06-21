# start_host.ps1 — run the whole fleet in ONE Python process (threads), not one process per crew.
#
# The memory-light alternative to start_fleet.ps1: imports crewai once and runs every (or a chosen)
# agent as a thread. Ideal for a dev clone where running the full per-process fleet would eat the RAM.
# The agents share one serve daemon. Ctrl+C stops the WHOLE host (all agents).
#
# Usage:
#   ./scripts/start_host.ps1                       # every approved crew, one process
#   ./scripts/start_host.ps1 -Agents joker,image-maker   # just these
#   ./scripts/start_host.ps1 -List                 # show what would run, then exit
#
# NB: do NOT also run start_fleet.ps1 in the same checkout — the per-agent single-instance locks would
# make whichever starts second exit. Pick ONE model per checkout (host here, per-process there).

param(
    [string]$Agents = "",
    [switch]$List
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path "$PSScriptRoot\..").Path
Set-Location $root

# Same connector-home pinning as every other entrypoint (a preset AIMEAT_HOME wins).
if (-not $env:AIMEAT_HOME) { $env:AIMEAT_HOME = Join-Path $root '.aimeat' }
Write-Host "[start_host] AIMEAT_HOME = $env:AIMEAT_HOME"

# venv Scripts first on PATH so `uv` resolves even if this shell's PATH lacks it.
$venvScripts = Join-Path $root '.venv\Scripts'
if (Test-Path $venvScripts) { $env:PATH = "$venvScripts;$env:PATH" }

$hostArgs = @('python', '-m', 'crewaimeat.fleet_host')
if ($Agents) { $hostArgs += @('--agents', $Agents) }
if ($List) { $hostArgs += '--list' }

Write-Host "[start_host] uv run $($hostArgs -join ' ')"
uv run @hostArgs

# start_fleet.ps1 — bring the whole crewaimeat fleet up (the counterpart to terminate_fleet.ps1).
#
# Usage:   ./scripts/start_fleet.ps1
#
# It does two things:
#   1. uv sync                                  — make sure the venv matches pyproject/uv.lock
#   2. start crew-forge under the watchdog      — in THIS terminal (foreground)
#
# There is no "launch every crew" loop here on purpose. Starting the fleet = crew-forge's
# idempotent reconcile, which lives in code, not a dumb start-all that could double-launch.
# crew-forge calls reconcile_fleet() ON STARTUP (crews/crew_forge_crew.py): it launches every
# APPROVED crew under its own watchdog (detached) and SKIPS the ones already running — so the
# whole fleet comes up on its own. crew-forge then stays in the foreground here; Ctrl+C stops
# ONLY crew-forge (the crews it launched keep running). Re-trigger a reconcile any time via
# crew-forge's /startall, or:
#   uv run python -c "from dotenv import load_dotenv; load_dotenv(); from crewaimeat.forge import reconcile_fleet; print(reconcile_fleet())"
#
# Note: freshly-forged/unapproved crews are reported, not auto-started (they need the owner's
# one-time device-flow approval first). install-autostart.ps1 does this same thing at logon.

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path "$PSScriptRoot\..").Path
Set-Location $root

# Put the venv's Scripts dir first on PATH so `uv` (and the watchdog's `uv run`) resolve even
# if this shell's PATH lacks uv (uv.exe lives next to the venv python).
$venvScripts = Join-Path $root '.venv\Scripts'
if (Test-Path $venvScripts) { $env:PATH = "$venvScripts;$env:PATH" }

Write-Host "[start_fleet] uv sync ..."
uv sync
if ($LASTEXITCODE -ne 0) { Write-Error "uv sync failed (exit $LASTEXITCODE)"; exit $LASTEXITCODE }

Write-Host "[start_fleet] starting crew-forge under the watchdog (it reconciles the fleet on startup) ..."
Write-Host "[start_fleet] crew-forge stays in THIS window; the other crews launch detached. Ctrl+C stops only crew-forge."
& "$root\scripts\watchdog.ps1" 'crews\crew_forge_crew.py'

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

# Pin the AIMEAT connector home to THIS repo (isolated from other projects' fleets on the machine, so
# two `aimeat connect serve` daemons can never collide on one global ~/.aimeat/serve.json). Every child
# inherits it — ensure_serve's daemon, the serve-watchdog, and crew-forge -> reconcile_fleet -> each
# detached crew — so all fleet processes resolve the SAME serve.json/tokens regardless of cwd. An
# explicitly preset AIMEAT_HOME wins (same precedence as the connector).
if (-not $env:AIMEAT_HOME) { $env:AIMEAT_HOME = Join-Path $root '.aimeat' }
Write-Host "[start_fleet] AIMEAT_HOME = $env:AIMEAT_HOME"

# Put the venv's Scripts dir first on PATH so `uv` (and the watchdog's `uv run`) resolve even
# if this shell's PATH lacks uv (uv.exe lives next to the venv python).
$venvScripts = Join-Path $root '.venv\Scripts'
if (Test-Path $venvScripts) { $env:PATH = "$venvScripts;$env:PATH" }

Write-Host "[start_fleet] uv sync ..."
uv sync
if ($LASTEXITCODE -ne 0) { Write-Error "uv sync failed (exit $LASTEXITCODE)"; exit $LASTEXITCODE }

# Start the SHARED loopback serve daemon once, before any crew. Every crew attaches to this one
# daemon (serve.json discovery): all MCP + deterministic calls multiplex over one persistent
# WebSocket per agent to the node — no per-call subprocess/TLS. ensure_serve is idempotent
# (pid-guarded), so this simply adopts an already-running daemon. Crews can also auto-start it,
# but doing it here once avoids a 30-crew thundering-herd on a cold boot.
Write-Host "[start_fleet] ensuring the shared loopback serve daemon (aimeat connect serve --http) ..."
uv run python "$root\scripts\ensure_serve.py"
if ($LASTEXITCODE -ne 0) { Write-Error "serve daemon failed to start (exit $LASTEXITCODE)"; exit $LASTEXITCODE }

# Supervise that daemon. It is the fleet's single point of failure — if it ever dies (a rare
# native crash, exit 0xC0000409, has been seen) nothing else restarts it and the WHOLE fleet's
# tunnel goes down silently. The supervisor calls the idempotent ensure_serve on a timer, so a
# crashed daemon comes back in seconds and never double-spawns. Detached + single-instance.
Write-Host "[start_fleet] starting the serve-daemon supervisor (auto-restarts the shared tunnel) ..."
Start-Process powershell -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File',"$root\scripts\serve_watchdog.ps1" `
    -WorkingDirectory $root -WindowStyle Hidden -RedirectStandardOutput "$root\logs\serve_watchdog.log" -RedirectStandardError "$root\logs\serve_watchdog.err.log"

Write-Host "[start_fleet] starting crew-forge under the watchdog (it reconciles the fleet on startup) ..."
Write-Host "[start_fleet] crew-forge stays in THIS window; the other crews launch detached. Ctrl+C stops only crew-forge."
& "$root\scripts\watchdog.ps1" 'crews\crew_forge_crew.py'

# start_fleet.ps1 — bring the whole crewaimeat fleet up (the counterpart to terminate_fleet.ps1).
#
# Usage:   ./scripts/start_fleet.ps1
#
# It does three things:
#   1. uv sync                                  — make sure the venv matches pyproject/uv.lock
#   2. ensure the shared serve daemon + its supervisor (the forward tunnel + auto-restart)
#   3. start the fleet HOST in THIS terminal    — every agent as a thread in ONE process
#
# MEMORY-LIGHT BY DEFAULT (since 0.5.0): this no longer launches one OS process per crew (which
# imported crewai ~N times and cost several GB). It runs the **fleet host** — every approved agent
# as a thread in ONE Python process, crewai imported once — ~20x less RAM for I/O-bound work.
# Ctrl+C stops the WHOLE fleet. To run the legacy per-process model instead (one watchdog per crew),
# start crew-forge directly: ./scripts/watchdog.ps1 crews/crew_forge_crew.py
#
# Note: only APPROVED agents (with a token) come online; an unapproved one waits for its one-time
# device-flow approval and joins by itself once approved.

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

# Run the fleet HOST: every agent as a thread in ONE Python process (crewai imported once), instead
# of one OS process per crew. ~20x less RAM for I/O-bound work (poll, shuffle text, call an LLM API);
# see scripts/start_host.ps1 / README "Fleet host". crew-forge is excluded (its job is launching the
# per-process fleet, redundant here) and reconcile_fleet no-ops under AIMEAT_FLEET_HOST, so nothing
# spawns a shadow per-process fleet. The host stays in THIS window; Ctrl+C stops the WHOLE fleet.
Write-Host "[start_fleet] starting the fleet HOST (all agents as threads in ONE process — memory-light) ..."
Write-Host "[start_fleet] the host stays in THIS window; Ctrl+C stops the WHOLE fleet."
uv run python -m crewaimeat.fleet_host

# Supervises the shared loopback serve daemon (the forward tunnel) — the fleet's single point of
# failure. If it dies (a rare native crash, exit 0xC0000409, has been observed) nothing else
# restarts it and the whole fleet's tunnel goes down silently. This runs the idempotent
# ensure_serve loop (crewaimeat.serve_watchdog): a crashed daemon comes back in seconds and is
# never double-spawned. Single-instance (the module holds a lock). Launch detached; logs to
# logs/serve_watchdog.log.
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
if (-not $env:AIMEAT_HOME) { $env:AIMEAT_HOME = Join-Path $root '.aimeat' }
$env:PATH = "$root\.venv\Scripts;" + $env:PATH
uv run python -m crewaimeat.serve_watchdog

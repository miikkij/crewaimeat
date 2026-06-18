#!/usr/bin/env bash
# serve_watchdog.sh — supervise the shared loopback serve daemon (the forward tunnel) on
# Linux/macOS. The counterpart to serve_watchdog.ps1.
#
# The serve daemon is the fleet's single point of failure: if it dies (a rare native crash
# has been observed) nothing else restarts it and the whole fleet's tunnel goes down silently.
# This runs the idempotent ensure_serve loop (crewaimeat.serve_watchdog): a crashed daemon
# comes back in seconds and is never double-spawned. Single-instance (the module holds a lock).
# Launch detached; logs to logs/serve_watchdog.log.
set -u
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
# Resolve the connector home the same way every entrypoint does, so the supervised daemon shares
# the fleet's serve.json/tokens. A preset/inherited AIMEAT_HOME wins.
: "${AIMEAT_HOME:="$root/.aimeat"}"; export AIMEAT_HOME
[ -d "$root/.venv/bin" ] && export PATH="$root/.venv/bin:$PATH"
exec uv run python -m crewaimeat.serve_watchdog

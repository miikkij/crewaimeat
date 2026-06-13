"""Start (or adopt) the shared loopback serve daemon and print its pid/port/agents.

Called by start_fleet.ps1. Lives in a file rather than an inline `python -c '...'` because the
inline form's nested quotes/brackets were fragile across PowerShell/uv versions (a truncated arg
raised a Python SyntaxError and aborted the fleet start). ensure_serve is idempotent (pid-guarded):
it adopts an already-running daemon or spawns exactly one."""

from aimeat_crewai import ensure_serve

d = ensure_serve(auto_start=True)
print("[start_fleet] serve daemon: port", d.get("port"), "pid", d.get("pid"),
      "agents", len(d.get("agents") or []))

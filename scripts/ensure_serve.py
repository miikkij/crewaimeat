"""Start (or adopt) the shared loopback serve daemon and print its pid/port/agents.

Called by start_fleet.ps1. Lives in a file rather than an inline `python -c '...'` because the
inline form's nested quotes/brackets were fragile across PowerShell/uv versions (a truncated arg
raised a Python SyntaxError and aborted the fleet start).

Goes through `ensure_single_serve` (not the bare `ensure_serve`) so the EXACTLY-ONE invariant holds
even though start_fleet and the serve-watchdog both reach for the daemon at boot: a cross-process
lock serializes the check->spawn, and a dedup pass reaps any daemon serve.json does not point at.
Two daemons steal each other's tunnels (a reconnect storm) and dispatched tasks silently time out —
the "(L)AIMEAT Sanomat just didn't update, no error" failure."""

from crewaimeat.serve_guard import ensure_single_serve

d = ensure_single_serve()
print("[start_fleet] serve daemon: port", d.get("port"), "pid", d.get("pid"),
      "agents", len(d.get("agents") or []),
      ("(reaped %d duplicate(s))" % d["_reaped_duplicates"]) if d.get("_reaped_duplicates") else "")

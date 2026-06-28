"""fleet_ops — make a newly-approved agent usable without a manual fleet restart.

The shared serve daemon loads its agent set AT STARTUP. An agent registered AFTER it started — the
normal case for a brand-new brain — isn't attached, so task create/list fail with UNKNOWN_AGENT. That
was the missing link between "approve" and "run" for a fresh agent: the operator had to restart the
whole fleet by hand. This restarts THIS home's serve daemon (the crews are NOT restarted — they
reconnect) so it reloads and picks up the new agent. Reuses serve_guard's single-instance machinery.
"""

from __future__ import annotations

import os
import subprocess
import time

from crewaimeat.tui import fleet_state


def serve_agents() -> set[str]:
    """Agent ids the shared serve daemon currently has loaded (from serve.json)."""
    return {a.get("agent") for a in (fleet_state.collect_serve().get("agents") or []) if a.get("agent")}


def _kill(pid: int) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=15)
        else:
            os.kill(pid, 9)
    except Exception:  # noqa: BLE001
        pass


def ensure_attached(agent: str) -> dict:
    """Ensure `agent` is loaded in the serve daemon. If it already is, no-op. Otherwise restart THIS
    home's serve daemon (kill + respawn one via serve_guard) so it reloads and attaches the agent; the
    crews stay running and reconnect. Returns {attached, restarted}. Only an APPROVED agent (one with a
    token) can attach — for an unapproved agent this returns attached=False (it needs approval first)."""
    if agent in serve_agents():
        return {"attached": True, "restarted": False}
    # An unapproved agent has no token; a serve restart can't attach it — don't disrupt the fleet for it.
    from crewaimeat.agency import account
    from crewaimeat.aimeat_crew import _token_exists

    if not _token_exists(agent, account.load()["owner"]):
        return {"attached": False, "restarted": False}
    from crewaimeat.serve_guard import ensure_single_serve, this_home_serve_pids

    for pid in this_home_serve_pids():
        _kill(pid)
    time.sleep(1.5)
    doc = ensure_single_serve()  # spawns one fresh daemon that loads every registered agent
    time.sleep(2)
    loaded = {a.get("agent") for a in (doc.get("agents") or []) if a.get("agent")}
    return {"attached": agent in loaded, "restarted": True}

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


_WATCHDOG_STARTED = (
    False  # spawn the supervisor at most once per cockpit process (its OS lock dedupes across processes)
)


def ensure_serve_watchdog() -> bool:
    """Start the serve_watchdog SUPERVISOR (keeps ONE serve daemon alive — restarts it on death/tunnel-drop).
    Safe to call repeatedly: a module flag avoids re-spawning in THIS process, and the supervisor's OS
    singleton lock makes any duplicate just exit. This is the piece the appliance was missing — without it,
    a dropped serve daemon was never revived and crews (auto_start=False) could not find the bridge."""
    global _WATCHDOG_STARTED
    if _WATCHDOG_STARTED:
        return True
    # NEVER spawn the DETACHED supervisor under pytest — it outlives the test process (CREATE_NO_WINDOW /
    # close_fds) and leaks real serve daemons onto the machine. A test must never mutate live/shared state.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    import sys

    try:
        creationflags = (
            subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        )  # silent supervisor; the serve daemon keeps its window
        os.makedirs("logs", exist_ok=True)
        log = open(os.path.join("logs", "serve_watchdog.log"), "a", encoding="utf-8")  # noqa: SIM115
        subprocess.Popen(
            [sys.executable, "-m", "crewaimeat.serve_watchdog"],
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            close_fds=True,
        )
        _WATCHDOG_STARTED = True
        return True
    except Exception:  # noqa: BLE001 — the daemon still works for this session; long-term resilience is best-effort
        return False


def ensure_serve_alive() -> dict:
    """Bring up ONE serve daemon AND a supervisor that keeps it alive — the appliance equivalent of the dev
    fleet's start_fleet. Idempotent + single-instance (safe to call on cockpit boot and on every agent
    start). After this, a crew's `auto_start=False` reliably finds a live, stable bridge."""
    from crewaimeat.serve_guard import ensure_single_serve

    doc = {}
    try:
        doc = ensure_single_serve()
    except Exception:  # noqa: BLE001
        pass
    ensure_serve_watchdog()
    return {"serve_pid": doc.get("pid"), "port": doc.get("port"), "watchdog": True}


# Per-agent cooldown so rapid repeated start/restart clicks can't hammer the serve daemon with
# back-to-back restarts (each restart drops the tunnel for every agent). Once we restart to attach an
# agent, a fresh daemon loads ALL approved tokens, so a second restart within the window is pointless.
_LAST_ATTACH_RESTART: dict[str, float] = {}
_ATTACH_RESTART_COOLDOWN = 20.0  # seconds


def ensure_attached(agent: str) -> dict:
    """Ensure `agent` is loaded in the serve daemon. If it already is, no-op (NO restart — the steady
    state must never drop the tunnel). Otherwise, for an APPROVED agent the running daemon never loaded,
    do ONE coordinated restart via serve_guard.restart_serve (kill+respawn under the spawn lock, serve.json
    re-pointed) so it reloads and attaches the agent; crews stay running and ride out the blip (they wait
    for the bridge). Returns {attached, restarted}. An unapproved agent (no token) returns attached=False."""
    if agent in serve_agents():
        return {"attached": True, "restarted": False}
    # An unapproved agent has no token; a serve restart can't attach it — don't disrupt the fleet for it.
    from crewaimeat.agency import account
    from crewaimeat.aimeat_crew import _token_exists

    if not _token_exists(agent, account.load()["owner"]):
        return {"attached": False, "restarted": False}
    # Cooldown: if we just restarted to attach (for any agent), a fresh daemon already loaded every
    # approved token — re-check rather than restart again. Prevents a restart storm on repeated clicks.
    last = _LAST_ATTACH_RESTART.get("_any", 0.0)
    if time.monotonic() - last < _ATTACH_RESTART_COOLDOWN:
        return {"attached": agent in serve_agents(), "restarted": False}
    from crewaimeat.serve_guard import restart_serve

    doc = restart_serve()  # ONE coordinated kill+respawn under the spawn lock; serve.json re-pointed
    _LAST_ATTACH_RESTART["_any"] = time.monotonic()
    ensure_serve_watchdog()  # keep the fresh daemon alive (revive on death/tunnel-drop)
    loaded = {a.get("agent") for a in (doc.get("agents") or []) if a.get("agent")}
    return {"attached": agent in loaded, "restarted": True}


def ensure_bridge(agent: str) -> dict:
    """The 'approve -> run' bridge with MINIMUM disruption — what fleet_action calls before starting a crew.

    Fast path: the daemon already serves `agent` -> only make sure the supervisor is running (no reap, no
    restart, no tunnel drop). Otherwise ensure one live, supervised daemon (idempotent), then attach the
    agent (a no-op if the just-ensured daemon already has it, else one coordinated restart). Folding the
    old two unconditional calls (ensure_serve_alive + ensure_attached on EVERY action) into this fast path
    is what stops the steady-state churn."""
    if agent in serve_agents():
        ensure_serve_watchdog()
        return {"attached": True, "restarted": False}
    ensure_serve_alive()  # one daemon + supervisor (idempotent: reaps, re-points serve.json, no restart)
    return ensure_attached(agent)

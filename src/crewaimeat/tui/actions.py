"""Fleet actions for the TUI — thin, safety-respecting wrappers over the existing control functions.
Each returns a short human result string and is called from a thread worker behind a confirm modal.

Safety (docs/internal/tui-plan.md):
  - start goes through forge.restart_crew / launch under the watchdog (single-instance lock; never a
    raw double-spawn). Fleet start goes through serve_guard.ensure_single_serve (one daemon) +
    forge.reconcile_fleet (idempotent, skips running crews).
  - stop kills WATCHDOG-then-daemon by crew filename only (forge.stop_crew) — never the serve daemon.
    Fleet stop uses the authoritative scripts/terminate_fleet.ps1 (correct kill order).
Imports are lazy so importing this module (and the TUI) stays cheap and side-effect-free.
"""

from __future__ import annotations

import os
import subprocess


# ── single crew ───────────────────────────────────────────────────────────────
def start_crew(agent: str) -> str:
    from crewaimeat.forge import start_crew as _start  # plain fn (not the @tool wrapper)

    return _start(agent)


def stop_crew(agent: str) -> str:
    from crewaimeat.forge import stop_crew as _stop

    return _stop(agent)


def restart_crew(agent: str) -> str:
    from crewaimeat.forge import recycle_crew  # true restart: stop → relaunch

    return recycle_crew(agent)


def reauth_crew(agent: str) -> str:
    from crewaimeat.forge import reauth as _reauth  # plain fn (not the @tool wrapper)

    return _reauth(agent)


# ── whole fleet ─────────────────────────────────────────────────────────────────
def reconcile_fleet() -> str:
    from crewaimeat.forge import reconcile_fleet as _reconcile

    return _reconcile()


def start_fleet() -> str:
    """Bring the fleet up: ensure exactly one serve daemon, then launch every approved crew."""
    from crewaimeat.serve_guard import ensure_single_serve

    doc = ensure_single_serve()
    recon = reconcile_fleet()
    return f"serve daemon pid {doc.get('pid')} port {doc.get('port')}. {recon}"


def stop_fleet() -> str:
    """Tear the fleet down in the correct order. Windows: scripts/terminate_fleet.ps1 (kills the
    serve-watchdog → connectors → crews). Elsewhere: best-effort pkill of watchdog/crews/serve."""
    if os.name == "nt":
        from crewaimeat.forge import _project_root

        script = _project_root() / "scripts" / "terminate_fleet.ps1"
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                capture_output=True,
                text=True,
                timeout=120,
            )
            tail = (r.stdout or "").strip().splitlines()[-1:] or [""]
            return f"terminate_fleet.ps1 ran. {tail[0]}"
        except Exception as exc:  # noqa: BLE001
            return f"terminate_fleet.ps1 failed: {exc!r}"
    killed = []
    for pat in ("watchdog.sh", "crews/", "connect serve"):  # watchdogs first, then crews, then serve
        try:
            r = subprocess.run(["pkill", "-f", pat], capture_output=True, timeout=20)
            killed.append(f"{pat}:{'ok' if r.returncode in (0, 1) else 'err'}")
        except Exception:  # noqa: BLE001
            killed.append(f"{pat}:err")
    return "fleet stop (posix pkill): " + ", ".join(killed)


def restart_fleet() -> str:
    return f"{stop_fleet()}  |  {start_fleet()}"


def reap_serve_daemons() -> str:
    """Enforce exactly one serve daemon (lock + dedup). Returns what it found/reaped."""
    from crewaimeat.serve_guard import ensure_single_serve

    doc = ensure_single_serve()
    reaped = doc.get("_reaped_duplicates", 0)
    return f"serve daemon: pid {doc.get('pid')} port {doc.get('port')}, {len(doc.get('agents') or [])} agents" + (
        f" — reaped {reaped} duplicate(s)" if reaped else " — already single"
    )

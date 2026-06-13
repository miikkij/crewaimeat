"""Supervisor for the shared loopback serve daemon (the forward tunnel).

The serve daemon is a single point of failure: every agent multiplexes its MCP + deterministic
calls over this one daemon. If it dies — a rare native crash was observed (Windows exit
0xC0000409) — nothing restarts it: start_fleet spawns it once, and crews use auto_start=False on
purpose (the single-spawner discipline that prevents tunnel-stealing storms). So a crashed serve
daemon takes the whole fleet's tunnel down and every task silently stalls until a human re-runs
start_fleet. That is the systemic cause behind "the newspaper just didn't update" with no error.

This loop keeps EXACTLY ONE serve daemon alive. `ensure_serve(auto_start=True)` is idempotent: it
discovers and returns the live daemon if present, and spawns only when none is alive — so a
periodic call restarts a crashed daemon WITHOUT ever double-spawning (which is what the
single-spawner discipline forbids). A single-instance lock stops two supervisors from racing.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

INTERVAL = 20  # seconds between liveness checks
_LOCK = Path("logs/.locks/serve-watchdog.lock")


def _another_instance_live() -> bool:
    try:
        return _LOCK.is_file() and (time.time() - _LOCK.stat().st_mtime) < INTERVAL * 3
    except OSError:
        return False


def run() -> None:
    from aimeat_crewai import ensure_serve

    if _another_instance_live():
        print("[serve-watchdog] another supervisor instance is live — exiting", file=sys.stderr)
        return
    _LOCK.parent.mkdir(parents=True, exist_ok=True)
    print(f"[serve-watchdog] supervising the shared serve daemon (every {INTERVAL}s)", flush=True)
    last_pid = None
    while True:
        _LOCK.write_text(str(time.time()), encoding="utf-8")  # heartbeat for the single-instance lock
        try:
            doc = ensure_serve(auto_start=True)  # idempotent: returns live, spawns only if dead
            pid = doc.get("pid")
            if pid != last_pid:
                n = len(doc.get("agents") or [])
                if last_pid is not None:
                    print(f"[serve-watchdog] RESTARTED the shared tunnel — it had died "
                          f"(was pid {last_pid}); now pid {pid}, port {doc.get('port')}, {n} agents",
                          flush=True)
                else:
                    print(f"[serve-watchdog] serve daemon live: pid {pid}, port {doc.get('port')}, "
                          f"{n} agents", flush=True)
                last_pid = pid
        except Exception as exc:  # noqa: BLE001 — a transient discover/spawn error must not kill the supervisor
            print(f"[serve-watchdog] ensure_serve failed (will retry): {exc!r}", file=sys.stderr, flush=True)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    run()

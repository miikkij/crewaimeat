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

import os
import sys
import time
from pathlib import Path

INTERVAL = 20  # seconds between liveness checks
_LOCK = Path("logs/.locks/serve-watchdog.lock")


def _acquire_singleton_lock(path: Path):
    """Acquire an EXCLUSIVE, process-lifetime lock. Returns the open file handle if WE are the only
    supervisor, or None if another live supervisor already holds it.

    Unlike a heartbeat-mtime check (which two supervisors can both pass on startup → permanent
    duplicates, and which a hung-but-recent holder can use to lock out its replacement), a real OS
    lock is atomic and self-healing: exactly one process holds it, a second fails to acquire and exits,
    and the OS releases it the instant the holder dies — so a crashed supervisor never blocks its
    successor. The lock lives under the repo's logs/.locks (per cwd), so different AIMEAT_HOMEs each
    run their own supervisor."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except OSError:
        fh.close()
        return None


def run() -> None:
    from crewaimeat.serve_guard import ensure_single_serve

    lock_fh = _acquire_singleton_lock(_LOCK)
    if lock_fh is None:
        print("[serve-watchdog] another supervisor already holds the lock — exiting", file=sys.stderr)
        return
    print(f"[serve-watchdog] supervising the shared serve daemon (every {INTERVAL}s)", flush=True)
    last_pid = None
    try:
        while True:
            try:
                doc = ensure_single_serve()  # idempotent: returns live, spawns only if dead, reaps duplicates
                pid = doc.get("pid")
                if doc.get("_reaped_duplicates"):
                    print(f"[serve-watchdog] reaped {doc['_reaped_duplicates']} duplicate serve daemon(s) "
                          f"— enforcing single instance (kept pid {pid})", flush=True)
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
    finally:
        try:
            lock_fh.close()  # releases the OS lock so a successor can take over
        except OSError:
            pass


if __name__ == "__main__":
    run()

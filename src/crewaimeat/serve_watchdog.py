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
MAX_BACKOFF = 300  # seconds: the longest wait between attempts after the daemon keeps failing to start
_LOCK = Path("logs/.locks/serve-watchdog.lock")


def has_agent_credential(home: Path) -> bool:
    """True when the connector home holds at least one agent credential the serve daemon would serve:
    a non-empty `tokens/{agent}@{owner}.token` or a `keys/{agent}@{owner}.key` (the two stores
    `aimeat connect serve` reads through listAllTokens / listAllAgentKeys). Retired credentials live in
    subdirectories and are not counted.

    WHY: with no credential, `aimeat connect serve` prints "No agents configured" and exits 1. On an
    empty fleet place (a pooled place, or a sold one before its first agent) the watchdog used to
    respawn it every ~27 s for ever. Measured on pannu-01 2026-11-14: 133-157 failed starts an hour on
    each of the five pooled places, each start a full Node boot at the tier's CPU cap, together about
    0.7 of a core burned on places with nothing to serve."""
    for sub, suffix in (("tokens", ".token"), ("keys", ".key")):
        d = home / sub
        try:
            entries = list(d.iterdir())
        except OSError:
            continue
        for p in entries:
            if not p.name.endswith(suffix) or "@" not in p.name:
                continue
            try:
                if p.is_file() and p.stat().st_size > 0:
                    return True
            except OSError:
                continue
    return False


def retry_delay(failures: int) -> int:
    """Seconds to wait after `failures` consecutive failed ticks: the plain INTERVAL for the first, then
    doubling up to MAX_BACKOFF, so a daemon that cannot start (a bad credential, a broken install) is
    retried a few times an hour instead of three times a minute. A crash of a daemon that WAS running
    is the first failure, so its restart is not slowed."""
    if failures <= 1:
        return INTERVAL
    return min(INTERVAL * 2 ** (failures - 1), MAX_BACKOFF)


def _say(msg: str, *, err: bool = False) -> None:
    """Print one timestamped line so the supervisor log reads as a timeline (when each daemon
    restart/reap/liveness check happened). Local time, sortable format."""
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", file=sys.stderr if err else sys.stdout, flush=True)


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
        _say("[serve-watchdog] another supervisor already holds the lock — exiting", err=True)
        return
    from aimeat_crewai import serve_auth_headers  # ensure_single_serve loads the package anyway

    from crewaimeat._home import aimeat_home
    from crewaimeat.wake_spin import MIN_STUCK_SECONDS, SpinSweeper

    _say(f"[serve-watchdog] supervising the shared serve daemon (every {INTERVAL}s)")
    last_pid = None
    sweeper = SpinSweeper()
    waiting = False  # no agent credential yet: said once, then quiet until one appears
    failures = 0
    try:
        while True:
            home = aimeat_home()
            if not has_agent_credential(home):
                if not waiting:
                    _say(
                        f"[serve-watchdog] no agent credential in {home} yet — the serve daemon starts "
                        f"when the first agent is enrolled (checking every {INTERVAL}s)"
                    )
                    waiting = True
                failures = 0
                time.sleep(INTERVAL)
                continue
            if waiting:
                _say("[serve-watchdog] an agent credential appeared — starting the serve daemon")
                waiting = False
            try:
                doc = ensure_single_serve()  # idempotent: returns live, spawns only if dead, reaps duplicates
                pid = doc.get("pid")
                if doc.get("_reaped_duplicates"):
                    _say(
                        f"[serve-watchdog] reaped {doc['_reaped_duplicates']} duplicate serve daemon(s) "
                        f"— enforcing single instance (kept pid {pid})"
                    )
                if pid != last_pid:
                    n = len(doc.get("agents") or [])
                    if last_pid is not None:
                        _say(
                            f"[serve-watchdog] RESTARTED the shared tunnel — it had died "
                            f"(was pid {last_pid}); now pid {pid}, port {doc.get('port')}, {n} agents"
                        )
                    else:
                        _say(f"[serve-watchdog] serve daemon live: pid {pid}, port {doc.get('port')}, {n} agents")
                    last_pid = pid

                # WAKE-SPIN SWEEP. `/local/wake/next` is level-triggered and consumes nothing, so one
                # queued push makes every wake return instantly and the daemon's idle wait becomes
                # zero. Measured on this machine 2026-08-17: ONE stuck agent of 61 drove 28 req/s at
                # the node and 76 % of a CPU; the fleet was the node's largest single traffic source.
                # Safe because the node's store is the truth for tasks and the daemon re-lists them
                # every cycle — this drops a redundant repeat, never an event. See wake_spin.py for
                # why nothing younger than MIN_STUCK_SECONDS is ever touched.
                port = doc.get("port")
                names = [a.get("agent") for a in (doc.get("agents") or []) if isinstance(a, dict) and a.get("agent")]
                if port and names:
                    for agent, took in sweeper.sweep(int(port), names, headers=serve_auth_headers(doc)):
                        _say(
                            f"[serve-watchdog] cleared a wake-queue spin on {agent!r}: consumed {took} "
                            f"stale element(s) after it read hot for over {MIN_STUCK_SECONDS}s"
                        )
                failures = 0
            except Exception as exc:  # noqa: BLE001 — a transient discover/spawn error must not kill the supervisor
                failures += 1
                _say(
                    f"[serve-watchdog] ensure_serve failed (attempt {failures}, next in "
                    f"{retry_delay(failures)}s): {exc!r}",
                    err=True,
                )
            time.sleep(retry_delay(failures))
    finally:
        try:
            lock_fh.close()  # releases the OS lock so a successor can take over
        except OSError:
            pass


if __name__ == "__main__":
    run()

"""Hard single-instance guarantee for the shared loopback serve daemon.

WHY THIS EXISTS: two `aimeat connect serve` daemons steal each other's tunnels (a reconnect storm),
so the node's dispatched tasks aren't delivered to the agents and workflow steps silently time out —
the exact "(L)AIMEAT Sanomat just didn't update, no error" failure. The root race: `ensure_serve(
auto_start=True)` is idempotent but NOT atomic across processes, so start_fleet's `ensure_serve.py` and
the serve-watchdog's first tick could both observe "no daemon" and both spawn before either wrote
serve.json — two daemons, same second.

This module makes "EXACTLY ONE" a hard invariant, two ways that back each other up:
  1. a CROSS-PROCESS LOCK serializes the check->spawn critical section (no birth-race), and
  2. a DEDUP pass KILLS any serve daemon that is not the one serve.json points at — run on every
     watchdog tick, so even a daemon that somehow slipped through is reaped within seconds.

Every spawn path (scripts/ensure_serve.py at fleet start, and crewaimeat.serve_watchdog on its timer)
goes through `ensure_single_serve()`. Crews never spawn (they discover, auto_start=False).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

_LOCK = Path("logs/.locks/serve-spawn.lock")
_SERVE_JSON = Path(os.path.expanduser("~")) / ".aimeat" / "serve.json"


class _CrossProcessLock:
    """Exclusive lock across processes (msvcrt on Windows, fcntl elsewhere). On timeout it proceeds
    anyway — the dedup pass is the real guarantee, so blocking forever is never worth it."""

    def __init__(self, path: Path, timeout: float = 60.0) -> None:
        self.path = path
        self.timeout = timeout
        self._fh = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+")
        deadline = time.time() + self.timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.time() > deadline:
                    print("[serve-guard] lock wait timed out — proceeding (dedup will enforce one)",
                          file=sys.stderr)
                    return self
                time.sleep(0.3)

    def __exit__(self, *exc):
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            try:
                self._fh.close()
            except OSError:
                pass


def _serve_pids() -> list[int]:
    """PIDs of every live `aimeat connect serve` process (matches the daemon's command line)."""
    if os.name != "nt":
        try:
            out = subprocess.run(["pgrep", "-f", "connect.*serve"], capture_output=True, text=True,
                                 timeout=15).stdout
            return [int(x) for x in out.split()]
        except Exception:  # noqa: BLE001
            return []
    ps = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'connect.*serve' "
          "-and $_.Name -notmatch 'pwsh|powershell' } | ForEach-Object { $_.ProcessId }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, text=True, timeout=20).stdout
        return [int(x) for x in out.split() if x.strip().isdigit()]
    except Exception as exc:  # noqa: BLE001
        print(f"[serve-guard] could not enumerate serve daemons: {exc}", file=sys.stderr)
        return []


def _kill(pid: int) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=15)
        else:
            os.kill(pid, 9)
    except Exception as exc:  # noqa: BLE001
        print(f"[serve-guard] failed to kill stray serve daemon {pid}: {exc}", file=sys.stderr)


def _reap_duplicates(keep_pid: int | None) -> int:
    """Kill every serve daemon except keep_pid. Returns how many were reaped."""
    pids = _serve_pids()
    reaped = 0
    if keep_pid is None:
        # No canonical pid known: keep the lowest pid (oldest-ish), kill the rest.
        if len(pids) <= 1:
            return 0
        keep_pid = min(pids)
    for pid in pids:
        if pid != keep_pid:
            _kill(pid)
            reaped += 1
            print(f"[serve-guard] reaped duplicate serve daemon pid {pid} (kept {keep_pid})", flush=True)
    return reaped


def ensure_single_serve(timeout: float = 60.0) -> dict:
    """Ensure EXACTLY ONE serve daemon is running and return its discovery doc.

    Serializes the check->spawn with a cross-process lock, then reaps any daemon that is not the one
    serve.json points at. Idempotent + safe to call on a timer."""
    from aimeat_crewai import ensure_serve

    with _CrossProcessLock(_LOCK, timeout):
        doc = ensure_serve(auto_start=True)  # discovers a live daemon, or spawns one if none
        reaped = _reap_duplicates(doc.get("pid"))
        if reaped:
            doc["_reaped_duplicates"] = reaped
        return doc


if __name__ == "__main__":
    d = ensure_single_serve()
    print(f"[serve-guard] single serve daemon: pid {d.get('pid')} port {d.get('port')} "
          f"agents {len(d.get('agents') or [])}"
          + (f" (reaped {d['_reaped_duplicates']} duplicate(s))" if d.get("_reaped_duplicates") else ""))

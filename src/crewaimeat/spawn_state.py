"""Where the spawned run mode keeps its state on disk, and how a run is measured.

ONE definition of the layout, imported by the spawner, the worker (`run_once`) and the TUI, so they
can never disagree about where a pidfile or an audit record lives. Everything hangs off AIMEAT_HOME —
NOT off the current working directory. That is deliberate: the connector home IS the tenant boundary
(it holds serve.json and the tokens), so anchoring here is what lets two users run same-named agents
side by side without their state colliding.

Deliberately free of crewai, requests and every heavy import: the spawner sits idle almost all the
time and must stay small enough that idle is genuinely free.

    <AIMEAT_HOME>/spawn/
      .spawner.lock            OS advisory lock — one spawner per home
      .spawner_status.json     heartbeat the TUI reads (same shape idea as logs/.host_status.json)
      running/<agent>.pid      {pid, run_id, started, manager_pid} — the orphan sweep reads these
      audit/<agent>/<run>.json one record per run: who woke it, what it cost, how it ended
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path


def aimeat_home() -> Path:
    """The connector home — resolved WITHOUT importing `aimeat_crewai`, when the env allows it.

    `crewaimeat._home.aimeat_home()` deliberately prefers `aimeat_crewai.paths` so the two can never
    drift. That is right for a crew, and wrong here: importing `aimeat_crewai` drags in crewai, and
    MEASURED that turns an idle spawner from ~25 MB into 193.8 MB — it would spend the whole day
    holding the very import the spawned run mode exists to avoid paying while idle.

    AIMEAT_HOME wins in the package's own precedence too, so when it is set (every fleet entrypoint
    pins it, and `spawner.main` sets it) this answers identically at zero import cost. Only an unset
    env falls through to the shared resolver, where correctness matters more than the megabytes.
    """
    env = os.environ.get("AIMEAT_HOME")
    if env:
        return Path(env)
    from crewaimeat._home import aimeat_home as _shared

    return _shared()


def spawn_dir() -> Path:
    return aimeat_home() / "spawn"


def running_dir() -> Path:
    return spawn_dir() / "running"


def audit_dir(agent: str) -> Path:
    return spawn_dir() / "audit" / _safe(agent)


def pid_file(agent: str) -> Path:
    return running_dir() / f"{_safe(agent)}.pid"


def log_file(agent: str, run_id: str) -> Path:
    """Where one worker's console output lands. A run whose log went to /dev/null cannot be audited —
    "montako kutsua, mita tapahtui" has to be answerable AFTER the process is gone."""
    return spawn_dir() / "logs" / f"{_safe(agent)}-{_safe(run_id)}.log"


def status_file() -> Path:
    return spawn_dir() / ".spawner_status.json"


def lock_file() -> Path:
    return spawn_dir() / ".spawner.lock"


def audit_file(agent: str, run_id: str) -> Path:
    return audit_dir(agent) / f"{_safe(run_id)}.json"


def _safe(name: str) -> str:
    """A filename that cannot escape its directory or upset Windows."""
    return "".join(c if (c.isalnum() or c in "._-") else "_" for c in str(name))[:120] or "unnamed"


def write_json(path: Path, payload: dict) -> None:
    """Atomic write — a reader (the TUI polls every 2 s) must never catch a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json(path: Path) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc if isinstance(doc, dict) else None
    except (OSError, ValueError):
        return None


def merge_audit(agent: str, run_id: str, fields: dict) -> Path:
    """Fold `fields` into this run's audit record, creating it if needed.

    Two writers touch one record and they do not overlap: the WORKER writes what only it knows (peak
    memory, the model's own view of the cycle) just before it exits, and the SPAWNER writes what only
    it knows (why it woke, when it started it, the exit code, whether it had to kill it) after the
    process is gone. Read-modify-write is therefore safe here, and a lost update would cost a field in
    a log rather than correctness.
    """
    path = audit_file(agent, run_id)
    doc = read_json(path) or {"agent": agent, "run_id": run_id}
    doc.update(fields)
    write_json(path, doc)
    return path


def peak_rss_mb() -> float | None:
    """This process's PEAK resident memory in MB, or None if the platform will not say.

    Peak, not current: a run's cost is the high-water mark it forced the machine to hold, and sampling
    "current" from outside would miss it between polls. Windows exposes PeakWorkingSetSize directly;
    POSIX gives ru_maxrss (KB on Linux, BYTES on macOS — hence the split).
    """
    try:
        if os.name == "nt":
            import ctypes
            import ctypes.wintypes as wt

            class _PMC(ctypes.Structure):
                _fields_ = [
                    ("cb", wt.DWORD),
                    ("PageFaultCount", wt.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            psapi = ctypes.WinDLL("psapi", use_last_error=True)
            psapi.GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.POINTER(_PMC), wt.DWORD]
            psapi.GetProcessMemoryInfo.restype = wt.BOOL
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.GetCurrentProcess.restype = wt.HANDLE
            counters = _PMC()
            counters.cb = ctypes.sizeof(_PMC)
            if not psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
                return None
            return round(counters.PeakWorkingSetSize / 1048576, 1)
        import resource

        raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round((raw if sys.platform == "darwin" else raw * 1024) / 1048576, 1)
    except Exception:  # noqa: BLE001 — a missing measurement must never fail a run
        return None


def pid_alive(pid: int) -> bool:
    """True when `pid` is a live process. Mirrors serve_guard's check rather than inventing a second."""
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                return False
            code = ctypes.c_ulong()
            ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
            k32.CloseHandle(handle)
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except Exception:  # noqa: BLE001
        return False

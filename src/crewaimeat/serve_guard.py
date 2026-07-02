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

import json
import os
import subprocess
import sys
import time
from pathlib import Path

_LOCK = Path("logs/.locks/serve-spawn.lock")


def _say(msg: str, *, err: bool = False) -> None:
    """Print one timestamped line so reap/re-point/error events line up on the supervisor log timeline."""
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", file=sys.stderr if err else sys.stdout, flush=True)


def _aimeat_home() -> Path:
    """The connector home for THIS process — crewaimeat._home.aimeat_home(), the single source
    (AIMEAT_HOME env wins, else <cwd>/.aimeat, matching the connector + aimeat_crewai). This used to
    re-derive with a legacy ~/.aimeat fallback, so an env-less caller inside a repo scoped to a
    PHANTOM home: this_home_serve_pids() found nothing and restart_serve() silently no-opped while
    ensure_serve (cwd precedence) adopted the old daemon — a mixed-home resolution in one call.
    Every home still gets its OWN serve daemon; foreign homes are never reaped."""
    from crewaimeat._home import aimeat_home

    return aimeat_home()


def _norm(p: str | Path | None) -> str | None:
    if not p:
        return None
    try:
        return os.path.normcase(os.path.realpath(str(p)))
    except OSError:
        return None


# ── per-home daemon PID registry ─────────────────────────────────────────────
# The env-read ownership check (_process_aimeat_home) is fragile on Windows (ReadProcessMemory can
# return None for a daemon that IS ours), and a None result means "leave it alone" — so a same-home
# duplicate whose env we can't read would survive and steal tunnels. Defence in depth: we also RECORD
# the pid of every serve daemon we spawn/adopt in a per-home file, so the reap can positively identify
# our OWN stale daemons by pid even when the env-read fails. The file lives under AIMEAT_HOME (per-home,
# so prod/dev never see each other's pids). Dead pids are pruned on every touch; pid-reuse is guarded by
# only reaping a registered pid whose env does NOT positively read as a DIFFERENT home.
def _registry_path() -> Path:
    return _aimeat_home() / ".serve-daemons.json"


def _load_registry() -> set[int]:
    try:
        data = json.loads(_registry_path().read_text(encoding="utf-8"))
        return {int(x) for x in data} if isinstance(data, list) else set()
    except Exception:  # noqa: BLE001 — missing/corrupt registry is just an empty set
        return set()


def _save_registry(pids: set[int]) -> None:
    try:
        p = _registry_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(sorted(pids)), encoding="utf-8")
    except OSError:
        pass


def _record_daemon(pid: int | None) -> None:
    """Remember `pid` as one of OUR serve daemons, pruning any registered pid no longer a live serve."""
    live = set(_serve_pids())
    reg = (_load_registry() | ({int(pid)} if pid else set())) & live
    _save_registry(reg)


def _process_aimeat_home(pid: int) -> str | None:
    """Best-effort: the AIMEAT_HOME env of another process, normalized. None if it can't be read
    (in which case the caller MUST NOT reap it — we only ever kill serves we can prove are ours)."""
    # POSIX: /proc/<pid>/environ is a NUL-separated KEY=VALUE list.
    if os.name != "nt":
        try:
            data = Path(f"/proc/{pid}/environ").read_bytes().decode("utf-8", "ignore")
            for kv in data.split("\x00"):
                if kv.startswith("AIMEAT_HOME="):
                    return _norm(kv.split("=", 1)[1])
            return _norm(str(Path(os.path.expanduser("~")) / ".aimeat"))  # env unset → legacy home
        except OSError:
            return None
    # Windows: read the target PEB → ProcessParameters → Environment via ctypes (x64). Any failure
    # (dead pid, access denied, layout mismatch) returns None → the process is left alone.
    try:
        import ctypes
        import struct

        k = ctypes.windll.kernel32
        ntdll = ctypes.windll.ntdll
        k.OpenProcess.restype = ctypes.c_void_p
        k.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        h = k.OpenProcess(0x0410, False, int(pid))  # QUERY_INFORMATION | VM_READ
        if not h:
            return None
        try:
            buf = (ctypes.c_void_p * 6)()
            if ntdll.NtQueryInformationProcess(ctypes.c_void_p(h), 0, buf, ctypes.sizeof(buf), None) != 0:
                return None
            peb = buf[1]
            if not peb:
                return None

            def rd(addr: int, size: int) -> bytes | None:
                b = ctypes.create_string_buffer(size)
                n = ctypes.c_size_t()
                if not k.ReadProcessMemory(ctypes.c_void_p(h), ctypes.c_void_p(addr), b, size, ctypes.byref(n)):
                    return None
                return b.raw

            d = rd(peb + 0x20, 8)
            if not d:
                return None
            pp = struct.unpack("<Q", d)[0]
            env_ptr = struct.unpack("<Q", rd(pp + 0x80, 8))[0]
            env_len = struct.unpack("<Q", rd(pp + 0x3F0, 8))[0]
            if not env_ptr:
                return None
            raw = rd(env_ptr, min(env_len or 32768, 1 << 20))
            if not raw:
                return None
            for kv in raw.decode("utf-16-le", "ignore").split("\x00"):
                if kv[:1] and "=" in kv[1:] and kv.split("=", 1)[0].upper() == "AIMEAT_HOME":
                    return _norm(kv.split("=", 1)[1])
            return _norm(str(Path(os.path.expanduser("~")) / ".aimeat"))  # env unset → legacy home
        finally:
            k.CloseHandle(ctypes.c_void_p(h))
    except Exception:  # noqa: BLE001
        return None


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
                    _say("[serve-guard] lock wait timed out — proceeding (dedup will enforce one)", err=True)
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
            out = subprocess.run(["pgrep", "-f", "connect.*serve"], capture_output=True, text=True, timeout=15).stdout
            return [int(x) for x in out.split()]
        except Exception:  # noqa: BLE001
            return []
    ps = (
        "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'connect.*serve' "
        "-and $_.Name -notmatch 'pwsh|powershell' } | ForEach-Object { $_.ProcessId }"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=20
        ).stdout
        return [int(x) for x in out.split() if x.strip().isdigit()]
    except Exception as exc:  # noqa: BLE001
        _say(f"[serve-guard] could not enumerate serve daemons: {exc}", err=True)
        return []


def _kill(pid: int) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, timeout=15)
        else:
            os.kill(pid, 9)
    except Exception as exc:  # noqa: BLE001
        _say(f"[serve-guard] failed to kill stray serve daemon {pid}: {exc}", err=True)


def _reap_duplicates(keep_pid: int | None) -> int:
    """Kill duplicate serve daemons FOR THIS HOME only. A serve daemon belongs to whichever
    AIMEAT_HOME it was launched with; the desktop's isolated home and the user's global ~/.aimeat
    fleet each run their own serve and must NOT reap each other (that caused a relaunch storm where
    the desktop's serve killed the fleet's and the fleet watchdog relaunched all 40 agents). So we
    only kill a process we can POSITIVELY confirm shares our home; unknown/other-home serves are
    left untouched. Returns how many were reaped."""
    our_home = _norm(_aimeat_home())
    live = _serve_pids()
    registry = _load_registry()
    # OURS = a live serve that EITHER reads as our home (env) OR we previously recorded (registry).
    # Pid-reuse guard: never reap a registered pid whose env POSITIVELY reads as a DIFFERENT home
    # (the pid was reused by another home's daemon) — only when it's our home or unreadable (None).
    pids = [
        p
        for p in live
        if _process_aimeat_home(p) == our_home or (p in registry and _process_aimeat_home(p) in (our_home, None))
    ]
    reaped = 0
    if keep_pid is None:
        if len(pids) <= 1:
            return 0
        keep_pid = min(pids)  # keep the oldest-ish of OUR home's serves
    for pid in pids:
        if pid != keep_pid:
            _kill(pid)
            reaped += 1
            _say(f"[serve-guard] reaped duplicate serve daemon pid {pid} (kept {keep_pid}, home {our_home})")
    if reaped:  # prune the killed pids from the registry so a future reused pid can't match
        _save_registry((registry | {keep_pid}) & set(_serve_pids()))
    return reaped


def this_home_serve_pids() -> list[int]:
    """PIDs of serve daemons that serve THIS AIMEAT_HOME. terminate_fleet uses this to stop ONLY our
    own daemon — never another home's (the desktop's isolated serve must survive our shutdown). A pid
    whose home cannot be read is treated as NOT ours (fail-safe: leave foreign/unknown serves alone)."""
    our_home = _norm(_aimeat_home())
    return [p for p in _serve_pids() if _process_aimeat_home(p) == our_home]


def _assert_serve_json_owner(doc: dict) -> bool:
    """Make serve.json name the kept LIVE daemon described by `doc`, atomically.

    The Node daemon owns serve.json writes, but a LOSER daemon that wrote serve.json naming itself
    right before we reaped it leaves serve.json pointing at a now-dead pid — the exact stale window a
    crew's `ensure_serve(auto_start=False)` hard-crashes on ("No live serve daemon … auto_start=False"),
    which then crash-loops the crew. After the reap we re-assert the kept daemon as serve.json's owner:
    only when its pid is alive AND answers /local/status (so we never publish a doc for a dead daemon),
    and only when serve.json doesn't already name it. The write is atomic (temp file + os.replace) so a
    concurrent reader never sees a half-written file. Returns True when serve.json names the kept live
    daemon afterwards."""
    pid = doc.get("pid")
    port = doc.get("port")
    if not pid or not isinstance(port, int):
        return False
    try:
        from aimeat_crewai.mcp_client import _pid_alive, _probe_serve, _read_discovery, serve_discovery_path
    except Exception:  # noqa: BLE001 — a future version moving these -> skip (the Node daemon still owns the file)
        return False
    path = serve_discovery_path()
    cur = _read_discovery(path)
    if cur and cur.get("pid") == pid and _pid_alive(pid):
        return True  # serve.json already names the kept live daemon — nothing to do
    # Only ever publish a doc for a daemon we can PROVE is live right now.
    if not (_pid_alive(pid) and _probe_serve(port, pid)):
        return False
    clean = {k: v for k, v in doc.items() if not str(k).startswith("_")}  # drop internal markers
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(clean, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)  # atomic on Windows + POSIX
        _say(f"[serve-guard] serve.json re-pointed to kept live daemon pid {pid} port {port}")
        return True
    except OSError as exc:
        _say(f"[serve-guard] could not rewrite stale serve.json: {exc}", err=True)
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def _guard_pytest() -> None:
    """A test must NEVER spawn or kill a real serve daemon. Every existing test mocks these functions;
    this backstop makes a forgotten mock fail LOUD instead of silently mutating the live machine."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        raise RuntimeError("refusing to touch the serve daemon under pytest — mock serve_guard in the test")


def ensure_single_serve(timeout: float = 60.0) -> dict:
    """Ensure EXACTLY ONE serve daemon is running and return its discovery doc.

    Serializes the check->spawn with a cross-process lock, then reaps any daemon that is not the one
    serve.json points at, and re-asserts serve.json's owner so it never names a reaped/dead pid.
    Idempotent + safe to call on a timer."""
    _guard_pytest()
    from aimeat_crewai import ensure_serve

    from crewaimeat.node_engine import serve_command

    with _CrossProcessLock(_LOCK, timeout):
        # serve_command() resolves the connector CLI even when a JUST-installed one isn't on this
        # process's PATH yet (the appliance's engine step installs it mid-session).
        doc = ensure_serve(aimeat_command=serve_command(), auto_start=True)  # discovers, or spawns one
        _record_daemon(doc.get("pid"))  # remember our canonical daemon so the reap can ID our strays by pid
        reaped = _reap_duplicates(doc.get("pid"))
        # A reaped loser may have left serve.json naming a now-dead pid — re-point it at the kept live
        # daemon atomically so a crew's auto_start=False discovery never reads a stale (dead) pid.
        _assert_serve_json_owner(doc)
        if reaped:
            doc["_reaped_duplicates"] = reaped
        return doc


def restart_serve(timeout: float = 60.0) -> dict:
    """Replace THIS home's serve daemon with a single fresh one — atomically vs the watchdog/guard.

    The connector loads its agent set ONLY at startup (no hot-reload route), so attaching a brand-new
    agent needs a restart. Doing the kill+respawn OUTSIDE the spawn lock is what produced the churn: the
    watchdog could spawn a second daemon in the gap, and serve.json transiently named the just-killed pid
    (the crew's crash trigger). Here the kill and the single respawn happen UNDER the spawn lock, and
    serve.json is re-pointed at the fresh daemon before returning — one coordinated replacement, no race."""
    _guard_pytest()
    from aimeat_crewai import ensure_serve

    from crewaimeat.node_engine import serve_command

    with _CrossProcessLock(_LOCK, timeout):
        for pid in this_home_serve_pids():
            _kill(pid)
        doc = ensure_serve(aimeat_command=serve_command(), auto_start=True)  # dead pid -> fresh spawn, waits live
        _record_daemon(doc.get("pid"))
        _reap_duplicates(doc.get("pid"))
        _assert_serve_json_owner(doc)
        return doc


if __name__ == "__main__":
    d = ensure_single_serve()
    print(
        f"[serve-guard] single serve daemon for home {_norm(_aimeat_home())}: "
        f"pid {d.get('pid')} port {d.get('port')} agents {len(d.get('agents') or [])}"
        + (f" (reaped {d['_reaped_duplicates']} duplicate(s))" if d.get("_reaped_duplicates") else "")
    )

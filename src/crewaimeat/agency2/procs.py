"""The processes agency 2.0 owns: one runtime per local agent, and this home's serve daemon.

A runtime is `python -m crewaimeat.agency2.runner <name>` = `json_agent.run_json_agent(<name>)`, started
in the data dir (so it finds `.env` and a staged first definition) with the bundled engine on PATH.

Every pid is written to `.agency2/pids/<name>.json`, and a stop KILLS ONLY a process whose command line
still names this runner and this agent: a pid reused after a reboot must never take down an innocent
process tree (the same rule the 0.8.x cockpit learned for ollama).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from crewaimeat.agency2 import engine, paths

RUNNER = "crewaimeat.agency2.runner"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _pid_path(name: str) -> Path:
    d = paths.state_dir() / "pids"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{name}.json"


def log_path(name: str) -> Path:
    return paths.logs_dir() / f"{name}.log"


def _cmdline(pid: int) -> str | None:
    """The live process's command line, or None when it is gone."""
    if os.name == "nt":
        ps = f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True,
                text=True,
                timeout=20,
                creationflags=_NO_WINDOW,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None
        return out or None
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
    except OSError:
        return None


def _ours(pid: int, name: str) -> bool:
    toks = (_cmdline(pid) or "").split()
    return RUNNER in toks and bool(toks) and toks[-1].strip('"') == name


def running(name: str) -> dict | None:
    rec = paths.read_json(_pid_path(name), None)
    if not rec:
        return None
    if _ours(int(rec["pid"]), name):
        return rec
    _pid_path(name).unlink(missing_ok=True)  # stale: the process is gone (or the pid is someone else's)
    return None


def start(name: str) -> dict:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        raise RuntimeError("procs.start spawns a runtime — never under pytest")
    cur = running(name)
    if cur:
        return cur
    log = open(log_path(name), "ab")  # noqa: SIM115 — handed to the child, closed below
    try:
        p = subprocess.Popen(
            [engine.python_exe(), "-m", RUNNER, name],
            cwd=str(paths.data_dir()),
            env=engine.child_env(),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=_NO_WINDOW,
        )
    finally:
        log.close()
    rec = {"pid": p.pid, "started": time.time()}
    paths.write_json(_pid_path(name), rec)
    return rec


def stop(name: str) -> bool:
    rec = paths.read_json(_pid_path(name), None)
    _pid_path(name).unlink(missing_ok=True)
    if not rec or not _ours(int(rec["pid"]), name):
        return False
    _kill_tree(int(rec["pid"]))
    return True


def _kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=_NO_WINDOW)
    else:
        import signal

        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def tail(name: str, max_bytes: int = 24_000) -> str:
    """The end of the agent's log, by BYTES (0.8.21: reading a whole grown log OOMed the cockpit)."""
    p = log_path(name)
    if not p.is_file():
        return ""
    with p.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - max_bytes))
        return f.read().decode("utf-8", errors="replace")


# ── the serve daemon of THIS home ───────────────────────────────────────────


def ensure_serve() -> dict:
    """Exactly one daemon for this home (serve_guard is home-scoped: another fleet's daemon is left alone)."""
    engine.apply_to_process()
    from crewaimeat.serve_guard import ensure_single_serve

    return ensure_single_serve()


def restart_serve() -> dict:
    """After a new agent is approved: the connector loads its agent set only at startup."""
    engine.apply_to_process()
    from crewaimeat.serve_guard import restart_serve as _restart

    return _restart()


def stop_serve() -> None:
    from crewaimeat.serve_guard import _kill, this_home_serve_pids

    for pid in this_home_serve_pids():
        _kill(pid)


def python_is_this() -> str:  # for the health view: which interpreter runs the agents
    return sys.executable

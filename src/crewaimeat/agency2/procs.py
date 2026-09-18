"""The processes agency 2.0 owns: ONE spawner for all local agents, and this home's serve daemon.

An agent costs nothing while it waits. `crewaimeat.agency2.spawn` parks on every local agent's wake and
starts a worker (`crewaimeat.run_once <agent>`) only when a task arrives; the worker exits when the task
is done. "Running" for an agent therefore means ON DUTY: the spawner is alive and the agent is on its
roster (connected here, not stopped). The roster is re-read every 30 s; a change restarts the spawner
so it takes hold at once — the spawner holds no work (tasks live on the node), so a restart loses none.

A FIRST CYCLE runs right after an agent is approved or given its first definition: a spawned agent
only runs when there is work, and until it has run once its staged definition is not published and
its identity (tags, offer, README) is not on the node. One `run_once` does that and exits.

Every pid is written under `.agency2/pids/`, and a stop KILLS ONLY a process whose command line still
names the module it was started as: a pid reused after a reboot must never take down an innocent
process tree (the rule the 0.8.x cockpit learned for ollama).
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

from crewaimeat.agency2 import engine, paths

SPAWN = "crewaimeat.agency2.spawn"
RUN_ONCE = "crewaimeat.run_once"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _pid_path(key: str) -> Path:
    d = paths.state_dir() / "pids"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{key}.json"


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


def _alive(key: str, module: str) -> dict | None:
    rec = paths.read_json(_pid_path(key), None)
    if not rec:
        return None
    if module in (_cmdline(int(rec["pid"])) or "").split():
        return rec
    _pid_path(key).unlink(missing_ok=True)  # stale: the process is gone (or the pid is someone else's)
    return None


def _launch(key: str, argv: list[str], log: Path) -> dict:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        raise RuntimeError("procs spawns processes — never under pytest")
    fh = open(log, "ab")  # noqa: SIM115 — handed to the child, closed below
    try:
        p = subprocess.Popen(
            argv,
            cwd=str(paths.data_dir()),
            env=engine.child_env(),
            stdin=subprocess.DEVNULL,
            stdout=fh,
            stderr=subprocess.STDOUT,
            creationflags=_NO_WINDOW,
        )
    finally:
        fh.close()
    rec = {"pid": p.pid, "started": time.time()}
    paths.write_json(_pid_path(key), rec)
    return rec


def _kill_tree(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, creationflags=_NO_WINDOW)
    else:
        import signal

        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


# ── the spawner ──────────────────────────────────────────────────────────────


def spawner() -> dict | None:
    return _alive("_spawner", SPAWN)


def ensure_spawner() -> dict:
    return spawner() or _launch("_spawner", [engine.python_exe(), "-m", SPAWN], paths.logs_dir() / "spawner.log")


def stop_spawner() -> bool:
    rec = spawner()
    _pid_path("_spawner").unlink(missing_ok=True)
    if not rec:
        return False
    _kill_tree(int(rec["pid"]))
    return True


def restart_spawner() -> dict:
    """Take a roster change (or a new daemon) into use now rather than at the next 30-second read."""
    stop_spawner()
    time.sleep(0.5)  # the OS releases the singleton lock when the old process is gone
    return ensure_spawner()


def running(name: str) -> dict | None:
    """On duty: the spawner is alive and this agent is on its roster."""
    from crewaimeat.agency2 import spawn, store

    rec = spawner()
    if rec and name in spawn.roster() and store.agent(name):
        return rec
    return None


def first_cycle(name: str) -> dict:
    """One `run_once` right away: publish a staged first definition, push the identity, drain the queue."""
    return _launch(f"first-{name}", [engine.python_exe(), "-m", RUN_ONCE, name], log_path(name))


def tail(name: str, max_bytes: int = 24_000) -> str:
    """The agent's own log (first cycles) followed by its latest spawned runs — by BYTES, never whole
    files (0.8.21: reading a grown log OOMed the cockpit)."""
    parts: list[Path] = []
    own = log_path(name)
    if own.is_file():
        parts.append(own)
    runs = paths.aimeat_home() / "spawn" / "logs"
    if runs.is_dir():
        mine = sorted(runs.glob(f"{name}-*.log"), key=lambda p: p.stat().st_mtime)
        parts.extend(mine[-2:])
    out = []
    budget = max_bytes
    for p in reversed(parts):
        with p.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            take = min(size, budget)
            f.seek(size - take)
            out.append(f.read().decode("utf-8", errors="replace"))
        budget -= take
        if budget <= 0:
            break
    return "\n".join(reversed(out))


# ── the serve daemon of THIS home ───────────────────────────────────────────


def _serve_guard(action: str) -> dict:
    """Run serve_guard in a SHORT-LIVED child and return its discovery doc.

    serve_guard needs `aimeat_crewai.ensure_serve`, and importing aimeat_crewai loads crewai: done in the
    cockpit it kept ~170 MB resident all day for a call that takes seconds (measured: idle cockpit
    198 MB). The daemon it starts is a detached node process, so it outlives the child; the child's
    memory goes back the moment it prints the doc."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        raise RuntimeError("procs spawns processes — never under pytest")
    code = "\n".join(
        [
            "import json",
            "from crewaimeat.agency2 import engine",
            "engine.quiet_env(); engine.apply_to_process()",
            "from crewaimeat import serve_guard",
            f"doc = serve_guard.{action}()",
            "print('AGENCY2-SERVE ' + json.dumps({k: doc.get(k) for k in ('port', 'pid')}))",
        ]
    )
    r = subprocess.run(
        [engine.python_exe(), "-c", code],
        cwd=str(paths.data_dir()),
        env=engine.child_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        creationflags=_NO_WINDOW,
    )
    for line in (r.stdout or "").splitlines():
        if line.startswith("AGENCY2-SERVE "):
            import json

            return json.loads(line[len("AGENCY2-SERVE ") :])
    raise RuntimeError(f"serve_guard.{action} failed (exit {r.returncode}): {(r.stderr or r.stdout)[-800:]}")


def ensure_serve() -> dict:
    """Exactly one daemon for this home (serve_guard is home-scoped: another fleet's daemon is left alone)."""
    return _serve_guard("ensure_single_serve")


def restart_serve() -> dict:
    """After a new agent is approved: the connector loads its agent set only at startup."""
    return _serve_guard("restart_serve")


def stop_serve() -> None:
    from crewaimeat.serve_guard import _kill, this_home_serve_pids

    for pid in this_home_serve_pids():
        _kill(pid)


def python_is_this() -> str:  # for the health view: which interpreter runs the agents
    return sys.executable

"""fleet_host — run MANY agents in ONE Python process (threads), not one process per crew.

Why: each `crews/<name>_crew.py` daemon imports crewai + litellm independently (~150-250 MB resident
PER process), so a 39-agent fleet costs ~8 GB of pure import bloat — absurd for I/O-bound work (poll
the queue, shuffle some text, call an LLM API). The host imports the heavy stack ONCE and runs each
agent as a thread: the work is network-bound, so the GIL is released during every poll / LLM call and
the agents run truly concurrently. Memory drops ~20x (one crewai + N thread stacks ≈ a few hundred MB).

This is OPT-IN and ADDITIVE — the per-process model (start_fleet -> watchdog per crew) is unchanged and
stays the default for prod. The host is ideal for a dev clone, where memory matters and per-process
crash isolation matters less. Each agent thread runs the SAME `run_crew` daemon loop; the per-agent
single-instance lock still applies (separate lock files, all held by this one process), so the host and
a stray per-process daemon for the same agent can never double-dispatch.

Run:
    uv run python -m crewaimeat.fleet_host                       # every approved crew, one process
    uv run python -m crewaimeat.fleet_host --agents joker,image-maker   # just these
    uv run python -m crewaimeat.fleet_host --list                # show what it would run, then exit

Trade-off: a hard NATIVE crash in one agent (e.g. a libxml2 segfault) takes the whole host down — but
that risk is already isolated to a subprocess (_extract_worker.py). A normal Python exception in one
agent is caught and that agent alone is restarted, the others keep running.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

# CrewAI registers SIGINT/SIGTERM handlers (telemetry + trace flushing) and calls signal.signal(),
# which RAISES "signal only works in main thread of the main interpreter" when a Crew runs in one of
# our worker threads. Two guards, applied AT IMPORT (before any crew runs):
#   1) opt out of CrewAI telemetry (it also phones home) so it never reaches signal registration;
#   2) make signal.signal a harmless no-op OFF the main thread — signals only ever fire on the main
#      thread anyway, so a worker-thread registration is meaningless; degrade it instead of crashing.
# The main thread keeps real signal handling (its Ctrl+C still stops the host).
for _var in ("CREWAI_DISABLE_TELEMETRY", "OTEL_SDK_DISABLED", "CREWAI_DISABLE_TRACKING"):
    os.environ.setdefault(_var, "true")

# Tell the rest of the code we're the in-process host. crew-forge's reconcile_fleet() checks this and
# becomes a no-op, so running crew-forge here never spawns a duplicate PER-PROCESS fleet (the bug that
# made the host launch 38 separate daemons). Set before any crew module runs. This guard is also why
# crew-forge RUNS IN the host like any other crew (it used to be excluded, which just left it dead —
# nothing else started it): its build deliverable (register + launch a NEW crew per-process) still
# works from a host thread, and the new crew is adopted as a thread at the next fleet restart.
os.environ["AIMEAT_FLEET_HOST"] = "1"

_ORIG_SIGNAL = signal.signal


def _safe_signal(sig, handler):
    if threading.current_thread() is threading.main_thread():
        return _ORIG_SIGNAL(sig, handler)
    return None  # no-op in worker threads — signals are main-thread-only


signal.signal = _safe_signal

_RESTART_DELAY_S = 10  # after an agent thread crashes, wait this long before restarting it
_MAX_RESTARTS = 5  # then give that ONE agent up (a persistent failure shouldn't hot-loop forever)
_STAGGER_S = 0.3  # gap between agent starts, so 39 onboarding bursts don't hit the node at once

# Status file the host heartbeats so the TUI (fleet_state) can SEE agents that run as threads here
# rather than as separate processes. Lives next to the lock dir; the TUI treats it as stale (host
# gone) if it stops being rewritten. {pid, agents: {AGENT_NAME: state}}.
_STATUS_FILE = Path("logs") / ".host_status.json"
_status: dict[str, str] = {}  # AGENT_NAME -> "running" | "crashed" | "stopped"
_status_lock = threading.Lock()


def _set_state(agent: str, state: str) -> None:
    with _status_lock:
        _status[agent] = state


def _write_status() -> None:
    try:
        _STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _status_lock:
            payload = {"pid": os.getpid(), "agents": dict(_status)}
        _STATUS_FILE.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass


def _clear_status() -> None:
    try:
        _STATUS_FILE.unlink()
    except OSError:
        pass


def _load_module(path: Path):
    """Import a crew file as a uniquely-named module WITHOUT triggering its __main__ block. The heavy
    `crewaimeat.aimeat_crew` import inside it is cached by Python, so it loads once across all crews."""
    mod_name = f"_host_crew_{path.stem}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _select_crews(agents: list[str] | None) -> list[Path]:
    """The crew files to run: all of crews/*_crew.py, optionally restricted to `agents` (by AGENT_NAME
    or by filename stem). Reuses forge's roster so discovery matches the per-process fleet exactly."""
    from crewaimeat.forge import _agent_name_of, _crew_files

    files = _crew_files()
    if not agents:
        return files  # default = EVERY crew, crew-forge included (its reconcile no-ops under the host env)
    want = {a.strip().lower() for a in agents if a.strip()}
    out = []
    for p in files:
        name = (_agent_name_of(p) or "").lower()
        stem = p.stem.lower().replace("_crew", "").replace("_", "-")
        if name in want or stem in want or p.stem.lower() in want:
            out.append(p)
    return out


def _supervise(path: Path, agent: str, stop: threading.Event) -> None:
    """Run ONE crew's daemon loop, restarting it on an unexpected crash (bounded). A clean return or a
    SystemExit (single-instance lock already held, or an auth exit) is final — we don't restart those.
    Reports the agent's state into the shared status the host heartbeats for the TUI."""
    label = path.stem
    try:
        mod = _load_module(path)
    except Exception as exc:  # noqa: BLE001 — a bad crew file must not take down the host
        print(f"[host] {label}: import failed ({exc!r}); skipping", file=sys.stderr)
        _set_state(agent, "crashed")
        return
    run = getattr(mod, "run", None)
    if not callable(run):
        print(f"[host] {label}: no run() — skipping", file=sys.stderr)
        _set_state(agent, "stopped")
        return

    restarts = 0
    while not stop.is_set():
        try:
            _set_state(agent, "running")
            run()  # blocks in run_crew's daemon loop for the lifetime of the agent
            print(f"[host] {label}: exited cleanly (will not restart)", file=sys.stderr)
            _set_state(agent, "stopped")
            return
        except SystemExit:
            print(f"[host] {label}: SystemExit (lock held or auth) — not restarting", file=sys.stderr)
            _set_state(agent, "stopped")
            return
        except Exception as exc:  # noqa: BLE001 — isolate: one agent's crash never kills the others
            restarts += 1
            _set_state(agent, "crashed")
            if restarts > _MAX_RESTARTS:
                print(f"[host] {label}: crashed {restarts}x ({exc!r}); giving up on this agent", file=sys.stderr)
                return
            print(
                f"[host] {label}: crashed ({exc!r}); restart {restarts}/{_MAX_RESTARTS} in {_RESTART_DELAY_S}s",
                file=sys.stderr,
            )
            stop.wait(_RESTART_DELAY_S)


def run_host(agents: list[str] | None = None) -> int:
    """Start every selected agent as a supervised thread in THIS process and block until Ctrl+C."""
    # Timestamp every log line (ours + the package's [daemon:*] lines) by wrapping stdout/stderr once,
    # before any agent thread starts sharing them. Opt out with AIMEAT_LOG_TIMESTAMPS=0.
    from crewaimeat.log_timestamps import install as _install_timestamps

    _install_timestamps()
    crews = _select_crews(agents)
    if not crews:
        print("[host] no matching crews to run.", file=sys.stderr)
        return 1

    # Bring up the ONE shared loopback serve daemon first, so every agent's liaison multiplexes over it
    # (serve_params) instead of each spawning its own stdio MCP subprocess — which would defeat the
    # whole point. Idempotent; adopts an already-running daemon.
    try:
        from crewaimeat.serve_guard import ensure_single_serve

        doc = ensure_single_serve()
        print(f"[host] shared serve daemon: pid {doc.get('pid')} port {doc.get('port')}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 — agents can still auto-start/poll; just warn
        print(f"[host] could not ensure serve daemon ({exc!r}); agents will fall back per-call", file=sys.stderr)

    from crewaimeat.forge import _agent_name_of

    print(f"[host] starting {len(crews)} agent(s) in ONE process: {', '.join(p.stem for p in crews)}", file=sys.stderr)
    stop = threading.Event()
    threads: list[threading.Thread] = []
    for path in crews:
        agent = _agent_name_of(path) or path.stem
        _set_state(agent, "starting")
        t = threading.Thread(target=_supervise, args=(path, agent, stop), name=path.stem, daemon=True)
        t.start()
        threads.append(t)
        _write_status()  # so the TUI sees agents appear as they start
        time.sleep(_STAGGER_S)  # avoid a thundering herd of simultaneous onboarding

    print("[host] all agents launched. Ctrl+C to stop the whole host.", file=sys.stderr)
    try:
        while any(t.is_alive() for t in threads):
            _write_status()  # heartbeat: the TUI treats a stale file as 'host gone'
            time.sleep(2.0)
    except KeyboardInterrupt:
        print("\n[host] stopping (Ctrl+C) — agents will be torn down with the process.", file=sys.stderr)
        stop.set()
    finally:
        _clear_status()
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Run many AIMEAT agents in ONE Python process (threads).")
    ap.add_argument("--agents", default="", help="comma-separated subset (AGENT_NAME or stem); default: all crews")
    ap.add_argument("--list", action="store_true", help="list the crews that would run, then exit")
    args = ap.parse_args()

    # Pin AIMEAT_HOME to this checkout (mirrors the entrypoints) so a dev clone uses its own tokens/serve.
    os.environ.setdefault("AIMEAT_HOME", str(Path.cwd() / ".aimeat"))

    selected = [a for a in args.agents.split(",") if a.strip()] or None
    if args.list:
        for p in _select_crews(selected):
            print(p.stem)
        return
    raise SystemExit(run_host(selected))


if __name__ == "__main__":
    main()

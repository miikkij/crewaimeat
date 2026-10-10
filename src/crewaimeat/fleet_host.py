"""fleet_host — run MANY agents in ONE Python process (threads), not one process per crew.

Why: each `crews/<name>_crew.py` daemon imports crewai + litellm independently (~150-250 MB resident
PER process), so a 39-agent fleet costs ~8 GB of pure import bloat — absurd for I/O-bound work (poll
the queue, shuffle some text, call an LLM API). The host imports the heavy stack ONCE and runs each
agent as a thread: the work is network-bound, so the GIL is released during every poll / LLM call and
the agents run truly concurrently. Memory drops ~20x (one crewai + N thread stacks ≈ a few hundred MB).

start_fleet starts this host for resident agents and a separate spawner for agents the node marks
run_mode=spawn. An all-spawn roster leaves the host empty while the detached spawner keeps running.

TWO KINDS OF RESIDENT AGENT. A crew FILE in `crews/` that the node does not mark spawn, as always;
and an agent that has no file here at all — ordered on aimeat.io, defined at `crews.registry.<agent>`
on the node, marked run_mode=resident ("Always on") and carried by THIS computer's connector. The
second kind is read from the node every 30 s, like the spawner reads its own half, so a new one
joins without a restart. A home that tells the node it keeps agents resident (AIMEAT_RUN_MODES, the
variable the connector reads) keeps this host up even with nothing to run, because the node then
accepts an always-on agent for this computer at any moment. An empty host imports no crewai.

Each agent thread runs the SAME `run_crew` daemon loop; the per-agent
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
# How often the node's resident roster is re-read: the spawner's own cadence for its half, so "approve
# it on the page and it runs" takes the same half minute whichever run mode the owner picked.
_ROSTER_INTERVAL_S = 30.0
# A node-defined agent whose thread ENDED (no definition yet, a credential refused, crashed five
# times) is started again this long after it ended, for as long as the node still lists it here. Each
# attempt costs one read of its definition and one status write, and no model call. An agent whose
# thread ended because a spawn worker still held its lock is the exception: that clears by itself in
# the time one run takes, so it is tried again at the next roster read.
_READMIT_S = 600.0

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
    except OSError as exc:
        # The TUI and doctor read this file to say which agents are up. A silent failure leaves
        # them describing a fleet that has since changed, with nothing indicating staleness.
        print(f"[host] status file write FAILED: {exc!r}", file=sys.stderr)


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


def _spawn_mode_files() -> set[Path]:
    """Crew files the SPAWNER owns, so the host does not thread them too.

    The host must skip exactly those or BOTH runtimes would start the same agent: the OS lock would
    then pick a winner arbitrarily and the loser would exit 0, so the agent would appear to be running
    while the wrong half of the system held it. Read statically (ast), never by importing — the host
    must not pay a crew's import cost just to decide it is not going to run it.
    """
    from crewaimeat import agent_manifest

    try:
        # THE NODE DECIDES, and the spawner reads the same answer — otherwise the two runtimes can
        # disagree about who owns an agent. A crew file's RUN_MODE is only a request: a crew that
        # asks for spawn while the node does not list it as spawn is served by NOBODY if we skip it
        # here, so it stays a thread. When the node cannot be asked the set is empty and every crew
        # runs here, which is the safe direction — the spawner's roster is empty in exactly that case.
        from crewaimeat.spawner import node_spawn_agents

        node_spawn = {agent_manifest.agent_local_name(a) for a in node_spawn_agents()[0]}
        if not node_spawn:
            return set()
        return {
            m.path.resolve()
            for m in agent_manifest.all_manifests(Path.cwd(), refresh=True)
            if m.live and m.agent in node_spawn
        }
    except Exception as exc:  # noqa: BLE001 — an unreadable manifest must not stop the whole fleet
        print(f"[host] could not read run modes ({exc!r}); starting every crew as continuous", file=sys.stderr)
        return set()


def _live_crew_files() -> list[Path]:
    """`crews/*_crew.py` minus the parked ones — the rule of `forge._crew_files`, read through
    `agent_manifest` instead.

    Not imported from forge on purpose: forge imports crewai (217 MB resident, measured 2026-10-10),
    and a host that stays up to wait for an always-on agent must not hold that while it has nothing
    to run. `agent_manifest` owns the parked rule (`PARKED_PREFIX`) and imports nothing heavy.
    """
    from crewaimeat import agent_manifest

    crews = agent_manifest.crews_dir(Path.cwd())
    if not crews.is_dir():
        return []
    return [p for p in sorted(crews.glob("*_crew.py")) if not p.name.startswith(agent_manifest.PARKED_PREFIX)]


def declares_resident() -> bool:
    """Whether this home tells the node it keeps agents resident: `resident` in AIMEAT_RUN_MODES.

    The connector reads the same variable and sends it as X-AIMEAT-Run-Modes; from then on the node
    stops changing a resident proposal to spawn for this computer, and the owner's page offers
    "Always on" for it. So the variable is a PROMISE that something here runs such an agent, and
    this host is what keeps it: with the variable set it stays up with an empty roster and waits.
    Read from the one place the promise is made, so the two cannot disagree.
    """
    from crewaimeat import agent_manifest

    modes = {m.strip().lower() for m in os.environ.get("AIMEAT_RUN_MODES", "").split(",")}
    return agent_manifest.RUN_RESIDENT in modes


_NOTED: dict[str, str] = {}


def _say_once(note: str | None, key: str) -> None:
    """One line per change of `note`; None forgets it, so the same condition coming back is said again."""
    if not note:
        _NOTED.pop(key, None)
    elif _NOTED.get(key) != note:
        _NOTED[key] = note
        print(f"[host] {note}", file=sys.stderr)


def _node_resident_agents() -> list[str] | None:
    """The agents this host runs from a definition on the NODE: resident there, no crew file here,
    carried by this computer's connector. GAIIs. None when it could not be read — which is not an
    empty list, and the caller keeps what it runs.

    WHY NO CREW FILE. An agent with a file is already this host's by the other road (`_select_crews`),
    parked files included: a leading underscore is the developer saying "not now", and the node's
    run mode does not overrule the checkout it runs from.

    WHY ONLY `resident` ROWS. The node's `run_mode` is null on an agent nobody has said anything
    about (a chat client, an outside bot), and `?run_mode=resident` returns none of those; every row
    is re-checked in `read_node_roster`, so a node that ignored the filter serves nothing here either.
    """
    from crewaimeat import agent_manifest
    from crewaimeat.spawner import carried_here, read_node_roster

    try:
        listed, notes, unreadable = read_node_roster(agent_manifest.RUN_RESIDENT)
    except Exception as exc:  # noqa: BLE001 — one failed read must not stop the agents already running
        _say_once(f"resident roster read failed ({exc!r}); keeping what runs", "resident-read")
        return None
    _say_once("resident roster: " + "; ".join(notes) if notes else None, "resident-notes")
    if unreadable:
        _say_once(
            f"resident roster: the node could not be asked for {', '.join(sorted(unreadable))}; keeping what runs",
            "resident-read",
        )
        return None
    _say_once(None, "resident-read")
    filed = {m.agent for m in agent_manifest.all_manifests(Path.cwd()) if m.agent}
    on_node_only = [a for a in listed if agent_manifest.agent_local_name(a) not in filed]
    return carried_here(on_node_only, what=agent_manifest.RUN_RESIDENT, say=_say_once)


def _select_crews(agents: list[str] | None) -> list[Path]:
    """The crew files to run: all of crews/*_crew.py, optionally restricted to `agents` (by AGENT_NAME
    or by filename stem). Reuses forge's roster so discovery matches the per-process fleet exactly.

    Crews declaring RUN_MODE = "spawn" are EXCLUDED even when named explicitly: they are started per
    wake by `crewaimeat spawner`, and running them here too would defeat the point (an always-on
    thread for an agent whose whole purpose is to cost nothing while idle)."""
    spawn_files = _spawn_mode_files()
    files = [p for p in _live_crew_files() if p.resolve() not in spawn_files]
    if spawn_files:
        print(
            f"[host] skipping {len(spawn_files)} spawn-mode crew(s) — run them with "
            f"`crewaimeat spawner`: {', '.join(sorted(p.stem for p in spawn_files))}",
            file=sys.stderr,
        )
    if not agents:
        return files  # default = EVERY crew, crew-forge included (its reconcile no-ops under the host env)
    from crewaimeat.forge import _agent_name_of

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


class _Resident:
    """One node-defined agent's thread, and how it last ended. Touched only by the host's main loop,
    except `ended_at`/`busy`, which the agent's own thread writes once, as its last act."""

    def __init__(self, identity: str, agent: str) -> None:
        self.identity, self.agent = identity, agent
        self.thread: threading.Thread | None = None
        self.ended_at = 0.0  # monotonic; 0 while it has never ended
        self.busy = False  # it ended because another runtime held the agent's lock


def _supervise_resident(res: _Resident, stop: threading.Event) -> None:
    """Run ONE node-defined agent's daemon loop: `run_json_agent`, which reads `crews.registry.<agent>`
    and reports what it loaded to `crews.runtime.<agent>`, exactly as a spawned run does.

    The restart policy on a crash is `_supervise`'s. The endings differ, because nothing here is a
    file somebody will edit and restart for: every ending leaves the thread, and the roster loop
    starts the agent again later for as long as the node still lists it on this connector.

      * exit 0 from the start: another runtime holds this agent's lock — a spawn worker finishing
        its run, when the owner has just switched the agent from spawn to always-on. Tried again at
        the next roster read.
      * any other SystemExit: the daemon's own exit on a credential the node refused. After a MOVE
        that is the end here, and the roster drops the agent in the same half minute.
      * no definition, or one this agent may not read: `run_json_agent` has said why in this log
        and in `crews.runtime.<agent>`, where the owner's Crew tab shows it.
    """
    from crewaimeat.crew_def import CrewDocError
    from crewaimeat.json_agent import run_json_agent
    from crewaimeat.memory_tools import MemoryReadRefused

    label, restarts = res.agent, 0
    try:
        while not stop.is_set():
            try:
                _set_state(res.agent, "running")
                run_json_agent(res.identity)  # blocks in run_crew's daemon loop while the agent lives
                print(f"[host] {label}: its loop returned", file=sys.stderr)
                _set_state(res.agent, "stopped")
                return
            except SystemExit as exc:
                res.busy = exc.code in (0, None)
                why = (
                    "another runtime holds this agent's lock; trying again at the next roster read"
                    if res.busy
                    else f"exit {exc.code}: the node refused its credential on this connector"
                )
                print(f"[host] {label}: ended ({why})", file=sys.stderr)
                _set_state(res.agent, "stopped")
                return
            except (CrewDocError, MemoryReadRefused):
                print(
                    f"[host] {label}: CANNOT START (the reason is above and in crews.runtime.{label}); "
                    f"asking again in {_READMIT_S / 60:.0f} min",
                    file=sys.stderr,
                )
                _set_state(res.agent, "stopped")
                return
            except Exception as exc:  # noqa: BLE001 — isolate: one agent's crash never kills the others
                restarts += 1
                _set_state(res.agent, "crashed")
                if restarts > _MAX_RESTARTS:
                    print(
                        f"[host] {label}: crashed {restarts}x ({exc!r}); leaving it for {_READMIT_S / 60:.0f} min",
                        file=sys.stderr,
                    )
                    return
                print(
                    f"[host] {label}: crashed ({exc!r}); restart {restarts}/{_MAX_RESTARTS} in {_RESTART_DELAY_S}s",
                    file=sys.stderr,
                )
                stop.wait(_RESTART_DELAY_S)
    finally:
        res.ended_at = time.monotonic()


def _follow_residents(residents: dict[str, _Resident], wanted: list[str] | None, stop: threading.Event) -> None:
    """Make the running node-defined agents match `wanted` (None = the roster could not be read:
    nobody is started and nobody is given up).

    A THREAD CANNOT BE STOPPED FROM OUTSIDE, and this does not pretend otherwise. An agent that
    leaves the roster because it MOVED ends by itself: the node refuses its credential here, the
    connector reports `auth_failed`, and the daemon loop exits on that within one cycle. An agent
    that leaves because the owner changed its run mode keeps its thread until this host restarts —
    it still holds the agent's lock, so the spawner's workers stand down and the work is done once,
    here. That is said once, when it happens, so nobody reads a quiet log as a change taken.
    """
    from crewaimeat import agent_manifest, spawn_state

    if wanted is None:
        return
    try:
        from datetime import datetime, timezone

        spawn_state.write_json(
            spawn_state.resident_roster_file(),
            {"read_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "agents": sorted(wanted)},
        )
        _say_once(None, "keep")
    except OSError as exc:
        _say_once(f"could not keep the resident roster on disk ({exc!r}); doctor will not know these agents", "keep")
    now = time.monotonic()
    for identity in sorted(wanted):
        res = residents.get(identity)
        if res is not None and res.thread is not None and res.thread.is_alive():
            continue
        if res is not None and res.ended_at and not res.busy and now - res.ended_at < _READMIT_S:
            continue  # it ended a moment ago for a reason that does not clear in thirty seconds
        _prepare_runtime()
        res = residents.setdefault(identity, _Resident(identity, agent_manifest.agent_local_name(identity)))
        res.busy = False
        _set_state(res.agent, "starting")
        res.thread = threading.Thread(target=_supervise_resident, args=(res, stop), name=res.agent, daemon=True)
        res.thread.start()
        print(f"[host] {res.agent}: on the resident roster — running its definition from the node", file=sys.stderr)
        _write_status()
        time.sleep(_STAGGER_S)
    for identity in sorted(set(residents) - set(wanted)):
        res = residents[identity]
        if res.thread is not None and res.thread.is_alive():
            _say_once(
                f"{res.agent}: no longer a resident agent of this connector on the node. Its loop ends by "
                "itself when the node refuses its credential here (a move); a changed run mode takes hold "
                "when this host restarts.",
                f"left:{identity}",
            )
            continue
        residents.pop(identity)
        _say_once(None, f"left:{identity}")
        with _status_lock:
            _status.pop(res.agent, None)
        print(f"[host] {res.agent}: left the resident roster", file=sys.stderr)


_runtime_ready = False


def _prepare_runtime() -> None:
    """What every agent thread needs once, before the FIRST one starts — and not before.

    This is where crewai is imported. A host with nothing to run never gets here, so waiting for an
    always-on agent costs what the spawner costs, not what a fleet costs.
    """
    global _runtime_ready
    if _runtime_ready:
        return
    _runtime_ready = True
    # Make CrewAI surface OpenRouter's per-call cost so the ledger records real spend, not $0
    # (CrewAI's OpenAI usage extractor drops response.usage.cost — this restores it), and pre-warm
    # CrewAI's litellm loader single-threaded so the 40 concurrent agent startups don't race it
    # (that race makes NVIDIA NIM etc. fail with "LiteLLM fallback not installed").
    from crewaimeat.crewai_cost_patch import install as _install_cost_patch
    from crewaimeat.crewai_cost_patch import prewarm_litellm as _prewarm_litellm

    _install_cost_patch()
    _prewarm_litellm()
    # Bring up the ONE shared loopback serve daemon first, so every agent's liaison multiplexes over it
    # (serve_params) instead of each spawning its own stdio MCP subprocess — which would defeat the
    # whole point. Idempotent; adopts an already-running daemon.
    try:
        from crewaimeat.serve_guard import ensure_single_serve

        doc = ensure_single_serve()
        print(f"[host] shared serve daemon: pid {doc.get('pid')} port {doc.get('port')}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 — agents can still auto-start/poll; just warn
        print(f"[host] could not ensure serve daemon ({exc!r}); agents will fall back per-call", file=sys.stderr)


def _report_health() -> None:
    """Print the doctor's verdict at fleet start — WARN, never block.

    The fleet coming up is the moment someone is actually watching the terminal, and it is the moment
    the divergence matters: an unregistered crew is about to idle silently, a ghost is about to open a
    tunnel with nothing behind it, an unrouted crew is about to pick a model nobody chose. Printing the
    count here turns all of that from "discovered in an audit months later" into "seen at every start".

    It never blocks the fleet: a drifted registry is a reason to look, not a reason to be offline. A
    failure inside the check itself is reported and ignored for the same reason.
    """
    try:
        from crewaimeat.doctor.cli import run as doctor_run

        report = doctor_run(Path.cwd())
    except Exception as exc:  # noqa: BLE001 — the health report must never keep the fleet down
        print(f"[host] doctor check skipped ({exc!r}) — starting anyway", file=sys.stderr)
        return
    n_err, n_warn = len(report.errors), len(report.warnings)
    if not n_err and not n_warn:
        print("[host] doctor: every registry agrees and every route is sanctioned.", file=sys.stderr)
        return
    print(f"[host] doctor: {n_err} error(s), {n_warn} warning(s) — run `crewaimeat doctor` for detail", file=sys.stderr)
    grouped = report.by_rule()
    for rule in sorted(grouped, key=lambda r: (grouped[r][0].severity != "error", r))[:6]:
        subjects = ", ".join(f.subject for f in grouped[rule][:4])
        more = f" (+{len(grouped[rule]) - 4})" if len(grouped[rule]) > 4 else ""
        print(f"[host]   {rule}: {subjects}{more}", file=sys.stderr)
    # The routing fallback is called out by NAME, because it is the one that changes behaviour without
    # any visible symptom: an unmapped crew silently resolves to the default profile, and that is how
    # 20 of 46 crews ended up on a free meta-router that picks a different model per call.
    unrouted = [f.subject for f in report.findings if f.rule == "registry.routing.unmapped"]
    if unrouted:
        print(
            f"[host]   {len(unrouted)} crew(s) have NO routing entry and will use the default profile: "
            f"{', '.join(sorted(unrouted))}",
            file=sys.stderr,
        )


def run_host(agents: list[str] | None = None) -> int:
    """Start every selected agent as a supervised thread in THIS process and block until Ctrl+C."""
    # Timestamp every log line (ours + the package's [daemon:*] lines) by wrapping stdout/stderr once,
    # before any agent thread starts sharing them. Opt out with AIMEAT_LOG_TIMESTAMPS=0.
    from crewaimeat.log_timestamps import install as _install_timestamps

    _install_timestamps()
    crews = _select_crews(agents)
    # The node's resident agents are followed by the WHOLE-fleet host only: `--agents a,b` means
    # exactly a and b, as it always has.
    follow = not agents
    stay = follow and declares_resident()
    wanted = _node_resident_agents() if follow else []
    if not crews and not wanted and not stay:
        print("[host] no matching crews to run.", file=sys.stderr)
        return 1

    stop = threading.Event()
    threads: list[threading.Thread] = []
    residents: dict[str, _Resident] = {}
    if crews:
        _report_health()
        _prepare_runtime()
        from crewaimeat.forge import _agent_name_of

        print(
            f"[host] starting {len(crews)} agent(s) in ONE process: {', '.join(p.stem for p in crews)}", file=sys.stderr
        )
        for path in crews:
            agent = _agent_name_of(path) or path.stem
            _set_state(agent, "starting")
            t = threading.Thread(target=_supervise, args=(path, agent, stop), name=path.stem, daemon=True)
            t.start()
            threads.append(t)
            _write_status()  # so the TUI sees agents appear as they start
            time.sleep(_STAGGER_S)  # avoid a thundering herd of simultaneous onboarding
        print("[host] all agents launched. Ctrl+C to stop the whole host.", file=sys.stderr)
    elif not wanted:
        print(
            "[host] nothing resident yet. This home tells the node it keeps agents resident "
            f"(AIMEAT_RUN_MODES), so the host stays and reads the node's roster every {_ROSTER_INTERVAL_S:.0f}s. "
            "Ctrl+C stops the host; the spawner and the serve daemon keep running.",
            file=sys.stderr,
        )

    def _alive() -> bool:
        return any(t.is_alive() for t in threads) or any(
            r.thread is not None and r.thread.is_alive() for r in residents.values()
        )

    next_roster = 0.0
    try:
        while True:
            if follow and time.monotonic() >= next_roster:
                # The first pass uses the answer already in hand; every later one asks again.
                _follow_residents(residents, wanted if next_roster == 0.0 else _node_resident_agents(), stop)
                next_roster = time.monotonic() + _ROSTER_INTERVAL_S
            if not stay and not _alive():
                break  # nothing runs and this home never promised to wait for more
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

    # Load .env BEFORE any crew imports or LLM use, and report anything the ambient environment
    # shadows. Both halves are load-bearing: nothing else on this path ever read .env (`uv run` does
    # not, and llm.py just calls os.getenv), so the fleet silently ran on whatever the launching shell
    # exported; and when both existed the environment won without a word. A stale OPENROUTER_API_KEY
    # inherited by every VS Code terminal cost two days that way — twice.
    from crewaimeat.env_guard import load_env

    load_env()

    selected = [a for a in args.agents.split(",") if a.strip()] or None
    if args.list:
        for p in _select_crews(selected):
            print(p.stem)
        for identity in [] if selected else _node_resident_agents() or []:
            print(f"{identity}  (defined on the node, no crew file)")
        return
    raise SystemExit(run_host(selected))


if __name__ == "__main__":
    main()

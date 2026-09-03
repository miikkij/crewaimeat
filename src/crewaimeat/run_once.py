"""`crewaimeat run-once <agent>` — run ONE agent for ONE cycle, in THIS process, then exit.

This is the WORKER half of the spawned run mode. The spawner (crewaimeat.spawner) parks on the
agent's tunnel wake and starts this module as a subprocess when work arrives; when the cycle is done
the process exits and the operating system takes every byte back. Measured on this repo: a real
5-agent/5-task run peaks at ~225 MB RSS and the cold `import crewai` + `crewaimeat` costs ~2.6 s, so
a run that takes a minute pays ~4% for the privilege of costing NOTHING while idle.

WHY A SUBPROCESS AND NOT A THREAD. A thread in fleet_host would be cheaper per run (crewai is already
imported) but it can never give the memory back: the fleet's 49 always-on agents hold ~3 GB of commit
and burn 12.6% of a core doing nothing. Idle is the common case, so the model that makes idle free
wins even though each individual run costs a little more.

WHY THIS RE-USES `run()` INSTEAD OF ITS OWN EXECUTION PATH. The crew file already declares everything
(`run()` builds the CrewSpec and hands it to run_crew). Every crew imports `run_crew` at MODULE level,
so patching `crewaimeat.aimeat_crew.run_crew` BEFORE importing the crew binds the crew's own name to
our capture shim. We get the author's real, complete spec — and not one of the 49 crew files changes.
The only thing we alter is `one_shot=True`, which makes `run_crew_daemon` do exactly one cycle
(PROPOSE -> EXECUTE -> messages -> records -> dms) and return. So a spawned run executes the SAME code
a continuous daemon executes; it just stops after one lap.

EXIT CODES (the spawner reads these):
  0  the cycle completed — including the case where another daemon holds the agent's single-instance
     lock, which is a correct, expected outcome and not a failure.
  2  the token was rejected (the daemon's own auth-failure exit) — re-auth needed, do not hot-loop.
  1  anything else: the crew raised, the crew file has no run(), the agent is unknown.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def _find_crew(agent: str, root: Path):
    """The manifest for `agent`, or None. Static (ast) — importing every crew to find one is absurd.

    MATCHED ON THE LOCAL NAME. The spawner hands identities as GAIIs (its roster comes from the node,
    which names agents that way), while a crew file declares the bare `AGENT_NAME = "web-researcher"`.
    Comparing the two whole strings never matches, and the miss is SILENT in the worst way: it looks
    like "no crew file", so run_once falls through to the node-backed path and reports that the agent
    has no definition published — naming a real defect that is not the one in front of it. Measured
    2026-09-03 on the first spawn-mode agent that actually has a Python crew: every ISO agent before
    it was node-backed, so nothing had exercised this comparison.
    """
    from crewaimeat import agent_manifest

    want = agent_manifest.agent_local_name(agent)
    for m in agent_manifest.all_manifests(root, refresh=True):
        if m.agent == want:
            return m
    return None


def _install_preamble() -> None:
    """The process-wide setup fleet_host installs, for the same reasons: timestamped log lines, and
    CrewAI surfacing OpenRouter's per-call cost so the ledger records real spend instead of $0.

    No `prewarm_litellm()` — that exists to stop 40 CONCURRENT agent startups racing the loader. One
    worker has nothing to race, and the prewarm costs seconds we would pay on every single run.
    """
    from crewaimeat.crewai_cost_patch import install as _install_cost_patch
    from crewaimeat.log_timestamps import install as _install_timestamps

    _install_timestamps()
    _install_cost_patch()


def _run_node_backed(agent: str, *, quiet: bool = False) -> int:
    """Run one cycle for an agent whose definition lives on the node, not in this repo.

    `json_agent.run_json_agent` already loads `crews.registry.<agent>`, builds the CrewSpec from it,
    wires the Crew tab's validate/try handler and re-reads the definition per task. Passing
    `one_shot=True` flows straight through to the CrewSpec, so this is the same scaffold a repo crew
    runs — the only difference is where the definition came from.
    """
    from crewaimeat import json_agent

    if not quiet:
        print(f"[run-once] {agent}: one cycle (node-backed, {json_agent.registry_key(agent)})", file=sys.stderr)
    try:
        json_agent.run_json_agent(agent, one_shot=True)
    except SystemExit as exc:  # the lock guard and the auth guard both leave this way
        return int(exc.code or 0)
    except Exception as exc:  # noqa: BLE001 — the spawner needs the real cause, not a stack in a log
        print(f"[run-once] {agent}: FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


RSS_HEARTBEAT_S = 30  # how often a running worker records its own peak memory


def _start_rss_heartbeat(agent: str, started: float) -> None:
    """Write this worker's peak memory into the audit WHILE it runs, not only when it finishes.

    A worker the spawner reaps (the run timeout) never reaches `_finish`, so the number is lost for
    exactly the run that most needs one: on 2026-09-02 a worker was killed at 3601.8 s and its audit
    record read `peak_rss_mb: null`. The manager holds the Popen handle but cannot read the child's
    peak reliably — on Windows the venv launcher is a SHIM whose child holds the real memory — while
    the worker always knows its own. So the worker says so as it goes.
    """
    import threading

    run_id = os.environ.get("AIMEAT_SPAWN_RUN_ID")
    if not run_id:  # run by hand, not by the spawner: nothing is reading an audit record
        return

    def _beat() -> None:
        while True:
            time.sleep(RSS_HEARTBEAT_S)
            if not _record_rss(agent, run_id, started):
                return

    threading.Thread(target=_beat, name="rss-heartbeat", daemon=True).start()


def _record_rss(agent: str, run_id: str, started: float) -> bool:
    """One heartbeat write. False when it failed, which stops the beating."""
    from crewaimeat import spawn_state

    try:
        spawn_state.merge_audit(
            agent,
            run_id,
            {"peak_rss_mb": spawn_state.peak_rss_mb(), "worker_seconds": round(time.monotonic() - started, 1)},
        )
    except Exception:  # noqa: BLE001 — bookkeeping must never change a run's outcome
        return False
    return True


def _finish(agent: str, started: float, code: int) -> int:
    """Record what only the worker knows (its own peak memory) and say how it went."""
    seconds = round(time.monotonic() - started, 1)
    peak = None
    try:
        from crewaimeat import spawn_state

        peak = spawn_state.peak_rss_mb()
        run_id = os.environ.get("AIMEAT_SPAWN_RUN_ID")
        if run_id:
            spawn_state.merge_audit(
                agent, run_id, {"peak_rss_mb": peak, "worker_seconds": seconds, "worker_exit": code}
            )
    except Exception as exc:  # noqa: BLE001 — bookkeeping must never change a run's outcome
        print(f"[run-once] {agent}: audit write skipped ({exc!r})", file=sys.stderr)
    print(
        f"[run-once] {agent}: exit={code} seconds={seconds}" + (f" peak_rss={peak} MB" if peak is not None else ""),
        file=sys.stderr,
    )
    return code


def run_once(agent: str, *, root: Path | None = None, quiet: bool = False) -> int:
    """Run one cycle for `agent`. Returns the process exit code (see the module docstring)."""
    root = root or Path.cwd()
    started = time.monotonic()
    _start_rss_heartbeat(agent, started)

    man = _find_crew(agent, root)
    if man is None:
        # NO LOCAL CREW FILE. That is not an error any more: an agent created by the node's
        # basic-agents button never touches this disk — its definition lives at
        # `crews.registry.<agent>`. json_agent.run_json_agent loads it from there and runs the same
        # scaffold, so a spawned worker serves it exactly like a repo crew.
        _install_preamble()
        return _finish(agent, started, _run_node_backed(agent, quiet=quiet))
    if man.parked:
        # A parked crew is parked on purpose. Running it from the side door would defeat the one
        # mechanism the repo has for taking an agent out of service.
        print(f"[run-once] {agent}: crew file is PARKED ({man.path.name}) — refusing to run it.", file=sys.stderr)
        return 1

    _install_preamble()

    import dataclasses

    from crewaimeat import aimeat_crew

    captured: dict = {}
    real_run_crew = aimeat_crew.run_crew

    def _capture(spec):
        """Stand in for run_crew during `mod.run()`: take the spec, run nothing."""
        captured["spec"] = spec

    aimeat_crew.run_crew = _capture
    try:
        import importlib.util

        spec_ = importlib.util.spec_from_file_location(f"_once_{man.path.stem}", man.path)
        if spec_ is None or spec_.loader is None:
            print(f"[run-once] {agent}: cannot load {man.path}", file=sys.stderr)
            return 1
        mod = importlib.util.module_from_spec(spec_)
        sys.modules[spec_.name] = mod
        spec_.loader.exec_module(mod)  # this is where `import crewai` is actually paid for
        runner = getattr(mod, "run", None)
        if not callable(runner):
            print(f"[run-once] {agent}: {man.path.name} has no run() — nothing to spawn.", file=sys.stderr)
            return 1
        runner()  # builds the CrewSpec and calls our shim instead of the real run_crew
    finally:
        aimeat_crew.run_crew = real_run_crew

    spec = captured.get("spec")
    if spec is None:
        print(f"[run-once] {agent}: run() did not call run_crew — cannot determine the crew spec.", file=sys.stderr)
        return 1

    if not quiet:
        print(
            f"[run-once] {agent}: one cycle (crew {man.path.name}, "
            f"max_concurrent_tasks={spec.max_concurrent_tasks}, listen_for={tuple(spec.listen_for)})",
            file=sys.stderr,
        )

    code = 0
    try:
        real_run_crew(dataclasses.replace(spec, one_shot=True))
    except SystemExit as exc:  # the lock guard and the auth guard both leave this way
        code = int(exc.code or 0)
    except Exception as exc:  # noqa: BLE001 — the spawner needs the real cause, not a stack in a log
        print(f"[run-once] {agent}: FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
        code = 1
    return _finish(agent, started, code)


def answer_invoke(path: Path) -> int:
    """Answer ONE server-initiated invoke (the Crew tab's Validate / Try) and write the result back.

    WHY A WORKER AND NOT THE SPAWNER. `crew.validate` is a pure function, but reaching it imports
    `crew_def` -> `aimeat_crewai.workflow_spec` -> crewai, which MEASURED takes an idle process from
    ~30 MB to ~194 MB. The spawner's whole value is that idling is free, so the import is paid here,
    in a process that exits. It fits: cold start is ~2.6 s against the node's 30 s validate ceiling,
    and a real `crew.try` run measured 101-103 s against its 5 min ceiling.

    The spawner consumed the invoke from the loopback queue (it holds the poll so the node never sees
    NO_HANDLER), so it hands the frame over in a file and posts the answer itself. Every path writes
    a result: an invoke nobody answers is a button that spins forever.
    """
    job = json.loads(path.read_text(encoding="utf-8"))
    agent = str(job.get("agent") or "")
    capability = str(job.get("capability") or "")
    out = path.with_suffix(".out.json")
    try:
        from crewaimeat.crew_invoke import handle

        ok, result = handle(capability, job.get("input") or {}, agent_name=agent)
    except Exception as exc:  # noqa: BLE001 — a raise here must still become an answer
        ok, result = False, {"code": "HANDLER_ERROR", "message": f"{type(exc).__name__}: {exc}"}
    out.write_text(json.dumps({"ok": bool(ok), "result": result}), encoding="utf-8")
    print(f"[run-once] {agent}: answered {capability} ok={ok}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="crewaimeat run-once",
        description="Run one agent for exactly one daemon cycle, then exit and give the memory back.",
    )
    ap.add_argument("agent", help="the AIMEAT agent name (matches AGENT_NAME in crews/<x>_crew.py)")
    ap.add_argument("--root", default=None, help="repo root holding crews/ (default: cwd)")
    ap.add_argument("--quiet", action="store_true", help="skip the per-run banner")
    ap.add_argument(
        "--answer-invoke",
        default=None,
        metavar="FILE",
        help="answer ONE invoke frame read from FILE (the spawner's validate/try path) and exit",
    )
    a = ap.parse_args(argv)
    os.environ.setdefault("AIMEAT_HOME", str((Path(a.root) if a.root else Path.cwd()) / ".aimeat"))
    from crewaimeat import env_guard

    env_guard.load_env()
    if a.answer_invoke:
        return answer_invoke(Path(a.answer_invoke))
    return run_once(a.agent, root=Path(a.root) if a.root else Path.cwd(), quiet=a.quiet)


if __name__ == "__main__":
    raise SystemExit(main())

"""Reusable AIMEAT crew scaffold — the validated, pitfall-covered base.

DO NOT reimplement the AIMEAT wiring in your own crew. Define only your DOMAIN
agents + tasks (a `build_domain` function) and hand them to `run_crew(CrewSpec(...))`.
This module locks down everything that was hard to get right and verified
end-to-end against https://aimeat.io:

- deterministic onboarding gate + one-shot Hello Integration (no LLM in the gate)
- run_crew_daemon wiring with the right LLM (two-phase: propose on queued /
  execute on active)
- the liaison `finalize` task: publish to AIMEAT memory + mark todos done ONE AT
  A TIME with read-after-write verify + complete the task
- the live progress bridge (crewfive.progress): milestones -> aimeat_task_event,
  5s live status -> memory key agents.<agent>.tasks.<id>.live
- current-date injection so the crew never hallucinates "today"

Why this is locked: each item above was a real failure we diagnosed and fixed
(tool-call races losing todo writes, OpenRouter empty-choices crashes, date
hallucination, onboarding cache loops). Routing around it reintroduces those bugs.

Minimal usage:

    from crewai import Agent, Task
    from crewfive.aimeat_crew import BuildContext, CrewSpec, run_crew

    def build_domain(ctx: BuildContext) -> tuple[list, list]:
        worker = Agent(role="Worker", goal="...", backstory="...", llm=ctx.llm)
        task = Task(description=f"{ctx.today}\\n\\n{ctx.prompt}", agent=worker,
                    expected_output="...")
        return [worker], [task]  # (agents, tasks) — last task's output is published

    def run():
        run_crew(CrewSpec(agent_name="my-crew", build_domain=build_domain))
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

for _s in (sys.stdout, sys.stderr):
    _r = getattr(_s, "reconfigure", None)
    if _r:
        _r(encoding="utf-8")

from crewai import Agent, Crew, Process, Task  # noqa: E402
from aimeat_crewai import create_liaison_agent, run_crew_daemon, stdio_params  # noqa: E402
from aimeat_crewai.daemon import DAEMON_DEFAULT_TOOL_FILTER  # noqa: E402

from crewfive.llm import get_llm  # noqa: E402
from crewfive.progress import install_progress  # noqa: E402


# --------------------------------------------------------------------------- #
# Public API: what a crew author fills in
# --------------------------------------------------------------------------- #
@dataclass
class BuildContext:
    """Passed to your build_domain(ctx). Everything you need to define agents/tasks."""

    task: dict          # the raw AIMEAT task (id, title, description, ...)
    prompt: str         # task.description or task.title — the user's actual request
    llm: Any            # the shared LLM (crewfive.llm.get_llm); pass to your Agents
    today: str          # current-time context string — prepend to time-sensitive tasks


# build_domain returns (agents, tasks). Tasks run in `process` order; the LAST
# task's output is what the liaison publishes to AIMEAT memory.
BuildDomain = Callable[[BuildContext], "tuple[list[Agent], list[Task]]"]


@dataclass
class CrewSpec:
    """Declares one AIMEAT-connected crew. Only `agent_name` + `build_domain` are required."""

    agent_name: str                       # the AIMEAT agent identity (from `aimeat connect add`)
    build_domain: BuildDomain             # returns (domain_agents, domain_tasks)
    process: Any = Process.sequential     # sequential is the validated path; hierarchical is advanced
    poll_seconds: int = 30                # daemon poll interval
    memory_key_prefix: str | None = None  # default: crews.<agent_name>
    manager_agent: Any = None             # only for Process.hierarchical


# --------------------------------------------------------------------------- #
# Locked machinery — do not copy/reimplement this into your crew
# --------------------------------------------------------------------------- #
def _now_context() -> str:
    """Deterministic current-time context (no LLM). Without it the model
    hallucinates the date and cannot anchor time-related questions.

    UTC is always the baseline. Europe/Helsinki is best-effort: if zoneinfo's
    tz database is missing (Windows without `tzdata`) it degrades to UTC only.
    """
    now_utc = datetime.now(timezone.utc)
    local_part = ""
    try:
        now_local = now_utc.astimezone(ZoneInfo(os.getenv("AIMEAT_CREW_TZ", "Europe/Helsinki")))
        local_part = f" = {now_local:%Y-%m-%d %H:%M} {now_local.tzname()} ({now_local:%A})"
    except Exception:  # noqa: BLE001 — tzdata missing etc.; UTC is enough as reference
        pass
    return (
        f"CURRENT TIME (reference for anything time/date related): "
        f"{now_utc:%Y-%m-%d %H:%M} UTC{local_part}. Use THIS date for "
        f"'today'/'now' references; do not assume any other date. "
        f"Verify up-to-date facts with web search."
    )


def _aimeat_call(agent_name: str, tool: str, payload: dict) -> dict | None:
    """Deterministic AIMEAT call via the connector CLI (no LLM). Windows: cmd /c."""
    if shutil.which("aimeat") is None:
        return None
    base = ["aimeat", "connect", "call", tool, "--agent", agent_name, "--stdin"]
    cmd = ["cmd", "/c", *base] if os.name == "nt" else base
    try:
        proc = subprocess.run(
            cmd, input=json.dumps(payload), capture_output=True, text=True, timeout=90
        )
        return json.loads(proc.stdout)
    except Exception as exc:  # noqa: BLE001
        print(f"[{agent_name}] {tool} failed: {exc}", file=sys.stderr)
        return None


def _onboarding_completed(agent_name: str) -> bool:
    data = _aimeat_call(agent_name, "aimeat_onboarding_status", {})
    return bool(data) and data.get("onboarding", {}).get("status") == "completed"


def _memory_key(agent_name: str, prefix: str | None, task: dict) -> str:
    tid = task.get("id") or "manual"
    short = tid.split("-", 1)[0] if "-" in tid else tid[:8]
    text = task.get("description") or task.get("title") or ""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:32].strip("-")
    token = f"{slug}-{short}" if slug else short
    base = prefix or f"crews.{agent_name}"
    return f"{base}.{token}.latest_output"


def _run_onboarding_only(agent_name: str) -> None:
    """One-shot Hello Integration (liaison alone, no domain work)."""
    print(
        f"[{agent_name}] Hello Integration not done -> running ONBOARDING ONLY "
        "(liaison alone, no domain work).",
        file=sys.stderr,
    )
    with create_liaison_agent(
        mcp_server_params=stdio_params(agent_name=agent_name),
        agent_name=agent_name,
        llm=get_llm(),
        tool_filter=DAEMON_DEFAULT_TOOL_FILTER,  # ~24 tools, not 95 (smaller models cope)
        verbose=True,
    ) as liaison:
        task = Task(
            description=(
                "Complete AIMEAT Hello Integration. Work carefully and in order — do NOT "
                "rush, and do NOT fire several tool calls in the same turn.\n"
                "1. aimeat_onboarding_status to see pending steps.\n"
                "2. Complete each pending step with its matching aimeat_onboarding_* tool.\n"
                "3. Test task: aimeat_task_propose_todos ONCE, then mark TODOs done with "
                "aimeat_task_todo ONE AT A TIME (wait for each result). Then you MUST call "
                "aimeat_task_complete with the test task's id to complete it. Do NOT re-mark "
                "done TODOs.\n"
                "4. aimeat_onboarding_status once more and report. No domain work."
            ),
            expected_output="All onboarding steps passed; test task completed.",
            agent=liaison,
        )
        Crew(
            agents=[liaison], tasks=[task], process=Process.sequential, verbose=True, cache=False
        ).kickoff()
        print(f"\n=== {agent_name}: ONBOARDING-ONLY done ===", file=sys.stderr)


def _finalize_task(agent_name: str, tid: str, mem_key: str, liaison: Agent) -> Task:
    """The locked liaison task that publishes the deliverable and closes the task.

    Sequential + read-after-write verify on todos (concurrent aimeat_task_todo
    calls race on the server and silently lose writes). Combined with
    parallel_tool_calls=False in get_llm(), this keeps todo updates safe.
    """
    return Task(
        description=(
            "The crew has finished the work for an ACTIVE AIMEAT task (the owner has "
            "already approved its plan). Publish the deliverable and close the task. "
            "Work carefully and in order — do NOT fire several tool calls in the same turn.\n"
            f"1. Write the previous agent's final result to AIMEAT memory under the EXACT "
            f"key '{mem_key}' with visibility owner (aimeat_memory_write).\n"
            f"2. Fetch the todo list with aimeat_task_get for task '{tid}'. Then mark each "
            "todo done with aimeat_task_todo (status='done') ONE AT A TIME: fire ONE call, "
            "WAIT for its result, then the next. NEVER several aimeat_task_todo calls in the "
            "same turn — the node updates a todo by rewriting the whole task, so concurrent "
            "updates race and silently lose writes.\n"
            f"3. Call aimeat_task_get for '{tid}' again and CONFIRM every todo status == "
            "'done'. Re-mark (still one at a time) any that are still pending, then re-check. "
            "Only proceed once all todos are verified done.\n"
            f"4. Finally call aimeat_task_complete for task '{tid}', using the result as the "
            "completion summary.\n"
            "Do not repeat an already-verified successful write."
        ),
        expected_output=f"Memory written to '{mem_key}'; ALL todos verified done; task '{tid}' completed.",
        agent=liaison,
    )


def run_crew(spec: CrewSpec) -> None:
    """Entry point: ensure onboarding once, then run the daemon forever.

    The daemon polls the AIMEAT queue; for each active task it builds a crew of
    [liaison, *your domain agents] with tasks [*your domain tasks, finalize] and
    runs it. Stop with Ctrl+C.
    """
    progress = install_progress(spec.agent_name)

    # 1) Ensure Hello Integration once (one-shot) before the daemon.
    if not _onboarding_completed(spec.agent_name):
        _run_onboarding_only(spec.agent_name)

    # 2) Per-task crew builder handed to the daemon.
    def _build(task: dict, liaison: Agent) -> Crew:
        llm = get_llm()
        tid = task.get("id")
        prompt = task.get("description") or task.get("title") or ""
        mem_key = _memory_key(spec.agent_name, spec.memory_key_prefix, task)
        print(f"[{spec.agent_name}] build crew for task {tid} -> key {mem_key}", file=sys.stderr)

        # Bind the progress bridge: kickoff starts a 5s heartbeat writing live status.
        progress.bind(tid, prompt[:80])

        ctx = BuildContext(task=task, prompt=prompt, llm=llm, today=_now_context())
        agents, tasks = spec.build_domain(ctx)
        finalize = _finalize_task(spec.agent_name, tid, mem_key, liaison)

        crew_kwargs: dict[str, Any] = {
            "agents": [liaison, *agents],
            "tasks": [*tasks, finalize],
            "process": spec.process,
            "verbose": True,
            "cache": False,
        }
        if spec.manager_agent is not None:
            crew_kwargs["manager_agent"] = spec.manager_agent
        return Crew(**crew_kwargs)

    # 3) Daemon: poll the queue, execute the per-task crew. llm=get_llm() keeps the
    #    daemon's liaison on the configured model (not CrewAI's OpenAI default).
    run_crew_daemon(
        agent_name=spec.agent_name,
        build_crew=_build,
        poll_interval_seconds=spec.poll_seconds,
        listen_for=("tasks",),
        llm=get_llm(),
    )

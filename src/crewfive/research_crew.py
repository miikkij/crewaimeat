"""research-crew — AIMEAT Liaison + run_crew_daemon (aimeat-crewai 0.3.x).

Model:
- Onboarding: if Hello Integration is not done yet, run a ONE-SHOT once
  (liaison alone) before the daemon.
- Daemon (run_crew_daemon): keeps one liaison, polls the AIMEAT queue, and for
  each task builds a per-task crew (Researcher -> Analyst -> Writer -> liaison
  publishes to memory + completes) and runs it. Other same-owner agents can
  queue work via aimeat_task_create; the daemon picks it up within
  ~poll_interval_seconds.

Run:
    uv run python -m crewfive.research_crew
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()
for _s in (sys.stdout, sys.stderr):
    _r = getattr(_s, "reconfigure", None)
    if _r:
        _r(encoding="utf-8")

from crewai import Agent, Crew, Process, Task  # noqa: E402
from aimeat_crewai import create_liaison_agent, run_crew_daemon, stdio_params  # noqa: E402
from aimeat_crewai.daemon import DAEMON_DEFAULT_TOOL_FILTER  # noqa: E402

from crewfive.crew import _web_tools  # noqa: E402
from crewfive.llm import get_llm  # noqa: E402
from crewfive.progress import install_progress  # noqa: E402

AGENT_NAME = "research-crew"
POLL_SECONDS = int(os.getenv("RESEARCH_CREW_POLL_SECONDS", "30"))

# Deterministic progress bridge (CrewAI event bus -> AIMEAT). Registers the
# listener once; bind() per task. No LLM. See crewfive/progress.py.
_PROGRESS = install_progress(AGENT_NAME)


# --------------------------------------------------------------------------- #
# Onboarding-status check (deterministic, no LLM)
# --------------------------------------------------------------------------- #
def _aimeat_call(tool: str, payload: dict) -> dict | None:
    if shutil.which("aimeat") is None:
        return None
    base = ["aimeat", "connect", "call", tool, "--agent", AGENT_NAME, "--stdin"]
    cmd = ["cmd", "/c", *base] if os.name == "nt" else base
    try:
        proc = subprocess.run(
            cmd, input=json.dumps(payload), capture_output=True, text=True, timeout=90
        )
        return json.loads(proc.stdout)
    except Exception as exc:  # noqa: BLE001
        print(f"[research-crew] {tool} failed: {exc}", file=sys.stderr)
        return None


def _onboarding_completed() -> bool:
    data = _aimeat_call("aimeat_onboarding_status", {})
    return bool(data) and data.get("onboarding", {}).get("status") == "completed"


def _memory_key(task: dict) -> str:
    tid = task.get("id") or "manual"
    short = tid.split("-", 1)[0] if "-" in tid else tid[:8]
    text = task.get("description") or task.get("title") or ""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:32].strip("-")
    token = f"{slug}-{short}" if slug else short
    return f"research.{AGENT_NAME}.{token}.latest_output"


def _now_context() -> str:
    """Deterministic current-time context for the crew (no LLM). Without it the
    model hallucinates the date and cannot anchor time-related questions.

    UTC is always the baseline. Europe/Helsinki is best-effort: if zoneinfo's
    tz database is missing (Windows without the `tzdata` package) it degrades
    cleanly to UTC only."""
    now_utc = datetime.now(timezone.utc)
    local_part = ""
    try:
        now_hel = now_utc.astimezone(ZoneInfo("Europe/Helsinki"))
        local_part = f" = {now_hel:%Y-%m-%d %H:%M} Europe/Helsinki ({now_hel:%A})"
    except Exception:  # noqa: BLE001 — tzdata missing etc.; UTC is enough as reference
        pass
    return (
        f"CURRENT TIME (reference for anything time/date related): "
        f"{now_utc:%Y-%m-%d %H:%M} UTC{local_part}. Use THIS date for "
        f"'today'/'now' references; do not assume any other date. "
        f"Verify up-to-date facts with web search."
    )


# --------------------------------------------------------------------------- #
# ONBOARDING-ONLY (once; the liaison/LLM walks through Hello Integration)
# --------------------------------------------------------------------------- #
def _run_onboarding_only() -> None:
    print(
        "[research-crew] Hello Integration not done -> running ONBOARDING ONLY "
        "(liaison alone, no domain work).",
        file=sys.stderr,
    )
    with create_liaison_agent(
        mcp_server_params=stdio_params(agent_name=AGENT_NAME),
        agent_name=AGENT_NAME,
        llm=get_llm(),
        tool_filter=DAEMON_DEFAULT_TOOL_FILTER,  # ~24 tools, not 95 (owl-alpha copes)
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
            expected_output="All 7 onboarding steps passed; test task completed.",
            agent=liaison,
        )
        Crew(
            agents=[liaison], tasks=[task], process=Process.sequential, verbose=True, cache=False
        ).kickoff()
        print("\n=== research-crew: ONBOARDING-ONLY done ===", file=sys.stderr)


# --------------------------------------------------------------------------- #
# build_crew: per-task domain crew + liaison publishes & completes
# (run_crew_daemon calls this for each active task it has picked up)
# --------------------------------------------------------------------------- #
def build_crew_for_task(task: dict, liaison: Agent) -> Crew:
    llm = get_llm()
    tid = task.get("id")
    prompt = task.get("description") or task.get("title") or ""
    mem_key = _memory_key(task)
    print(f"[research-crew] build_crew for task {tid} -> key {mem_key}", file=sys.stderr)

    # Bind the progress bridge to this task: kickoff starts a 5s heartbeat that
    # writes live status to the key agents.research-crew.tasks.<tid>.live.
    _PROGRESS.bind(tid, prompt[:80])

    today = _now_context()  # injected into every domain task (deterministic)

    researcher = Agent(
        role="Researcher",
        goal="Gather relevant, up-to-date background for the given task",
        backstory=(
            "You are a thorough researcher who assembles facts and sources before analysis. "
            "You use web search to make sure the information is current."
        ),
        tools=_web_tools(),
        llm=llm,
        verbose=True,
    )
    analyst = Agent(
        role="Analyst",
        goal="Analyze the findings and identify the key conclusions",
        backstory="You are a sharp analyst who separates the essential and draws conclusions.",
        llm=llm,
        verbose=True,
    )
    writer = Agent(
        role="Writer",
        goal="Write a clear, concise, actionable final result",
        backstory="You are a professional writer who produces structured, concrete text.",
        llm=llm,
        verbose=True,
    )

    research = Task(
        description=f"{today}\n\nResearch and gather the key background for the task:\n{prompt}",
        expected_output="A list of the key findings and facts with sources.",
        agent=researcher,
    )
    analysis = Task(
        description=f"{today}\n\nAnalyze the findings and identify 3–5 key conclusions.",
        expected_output="A short analysis with the key conclusions.",
        agent=analyst,
    )
    writing = Task(
        description=(
            f"{today}\n\nWrite a clear, structured final result (title, summary, sections, "
            "concrete recommendations) based on the research and analysis. If the result "
            "refers to a date, use the current time given above. Follow any explicit "
            "instructions in the original request below (e.g. requested language, format, "
            "audience); otherwise choose what fits best:\n"
            f"{prompt}"
        ),
        expected_output="A structured final result.",
        agent=writer,
    )
    finalize = Task(
        description=(
            "The crew has finished the work for an ACTIVE AIMEAT task (the owner has "
            "already approved its plan). Publish the deliverable and close the task. "
            "Work carefully and in order — do NOT fire several tool calls in the same turn.\n"
            f"1. Write the writer's final result to AIMEAT memory under the EXACT key "
            f"'{mem_key}' with visibility owner (aimeat_memory_write).\n"
            f"2. Fetch the todo list with aimeat_task_get for task '{tid}'. Then mark each "
            "todo done with aimeat_task_todo (status='done') ONE AT A TIME: fire ONE call, "
            "WAIT for its result, then the next. NEVER several aimeat_task_todo calls in the "
            "same turn — the node updates a todo by rewriting the whole task, so concurrent "
            "updates race and silently lose writes.\n"
            f"3. Call aimeat_task_get for '{tid}' again and CONFIRM every todo status == "
            "'done'. Re-mark (still one at a time) any that are still pending, then re-check. "
            "Only proceed once all todos are verified done.\n"
            f"4. Finally call aimeat_task_complete for task '{tid}', using the writer's "
            "result as the completion summary.\n"
            "Do not repeat an already-verified successful write."
        ),
        expected_output=f"Memory written to '{mem_key}'; ALL todos verified done; task '{tid}' completed.",
        agent=liaison,
    )

    return Crew(
        agents=[liaison, researcher, analyst, writer],
        tasks=[research, analysis, writing, finalize],
        process=Process.sequential,
        verbose=True,
        cache=False,
    )


def run() -> None:
    # 1) Ensure Hello Integration once (one-shot) before the daemon.
    if not _onboarding_completed():
        _run_onboarding_only()

    # 2) Daemon: poll the queue, execute the per-task crew. Stop with Ctrl+C.
    #    llm=get_llm() ensures the daemon's liaison uses owl-alpha (OpenRouter)
    #    instead of falling back to CrewAI's OpenAI default (OPENAI_API_KEY).
    run_crew_daemon(
        agent_name=AGENT_NAME,
        build_crew=build_crew_for_task,
        poll_interval_seconds=POLL_SECONDS,
        listen_for=("tasks",),
        llm=get_llm(),
    )


if __name__ == "__main__":
    run()

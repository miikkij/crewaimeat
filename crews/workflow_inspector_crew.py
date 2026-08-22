"""workflow-inspector: detect a broken workflow run, auto-repair the deterministic steps, and
report the rest — so a chained pipeline (e.g. the (L)AIMEAT Sanomat newspaper) is never silently
incomplete again.

Reference implementation of the crew-side half of the Agent Workflows feature
(docs/internal/2026-06-13-agent-workflows-node-spec.md). Once the node engine ships, the node
invokes this agent on a failed step signal; until then its idle hook checks the day's workflow
after the evening deadline and acts. Deterministic check; LLM only for an `llm` signal leaf.

Register + run:
  npx aimeat@latest connect --url https://aimeat.io --owner <you> --agent workflow-inspector
  uv run python crews/workflow_inspector_crew.py
"""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.workflow_inspector import inspect, publish_inspection

AGENT_NAME = "workflow-inspector"

# ── This agent's own declaration ─────────────────────────────────────────────────────────────
# The single source for what this agent is: its model routing, how it is discovered, and what it
# promises. These used to live in three central lists (fleet_identity.py / llm_providers.json /
# offers.py) that nothing kept in step, so an agent could — and did — come online missing from
# all of them. crewaimeat.agent_manifest reads these statically; the lists are derived.
LLM_PROFILE = "coding"

# Why a step can be "run" and still never complete — the mode gate. Loaded fail-loud at start.
SKILLS = ["aimeat-agent-modes"]

TAGS = ["workflow-inspection", "diagnosis", "role.task-runner"]
CAPABILITIES = {
    "technical": [{"name": "workflow-inspector", "type": "skill"}],
    "domain": ["workflow run inspection: diagnose / auto-repair / escalate", "per-step signal health"],
    "languages": ["en"],
}


# What this agent advertises it can do. The `ask` states NEGATIVE SCOPE on purpose — what it
# will NOT do is the half a buyer needs and the half an author skips.
OFFERS = [
    {
        "id": "inspect-workflow-run",
        "title": "Diagnose a broken workflow run and repair what is safely repairable",
        "ask": "Point me at a workflow run and I inspect it step by step against its declared signals, re-run "
        "the steps that are deterministically repairable, and report the rest with a recommendation. I "
        "do NOT change workflow rules, do not fabricate a step's output, and escalate anything that "
        "needs a decision.",
        "example": "workflow: 'laimeat-sanomat-evening', date: '2026-08-22'",
        "cost": "cheap",
        "latency": "minutes",
        "repeatability": "idempotent",
        "verification": "ungated",
        "consequences": [
            {
                "type": "publishes-public",
                # `modifies-data` is not in the shared enum, and inventing a type would break the
                # node's validation. Re-running a repaired step makes that step re-publish its own
                # output — for the newspaper steps that output is public, so this is the accurate one.
                "note": "a repaired step re-runs and re-publishes its own output key",
            }
        ],
        "sample": (
            "**Workflow Inspection Report**  \n*Workflow ID:* `laimeat-sanomat-evening.26675fe6-084b-4985-aa18-028d9bbfda4e`  \n*Owner Memory Key:* `workflows.run.laimeat-sanomat-evening.26675fe6-084b-4985-aa18-028d9bbfda4e`  \n*Inspection Timestamp:* 2026‑08‑18\u202f00:30\u202fEEST (2026‑08‑17\u202f21:30\u202fUTC)  \n\n---\n\n### 1. Summary of Step States (as recorded in the run snapshot)\n\n| Step Name | Agent | Recorded State | Observed Condition (expected\u202fvs\u202fobserved) |\n|-----------|-------|----------------|------------------------------------------|\n| **fetch‑news‑feed** | news‑fetcher | ✅\u202fgreen | expected: key exists, observed: key exists |\n\n…"
        ),
    },
]

_TZ = ZoneInfo("Europe/Helsinki")

README = """[[FIGLET:slant]["Workflow Inspector"]]

Watches **agent workflows** (chained scheduled steps) and makes a broken run **loud, not silent**.
For each step it checks two signals — `required_to_function` (input) and `success_signal` (output)
— re-runs the deterministic steps that can be safely repaired (idempotent), and writes a
step-by-step report + recommendation for what it can't fix. First workflow: the (L)AIMEAT Sanomat
evening edition. I diagnose and repair; I don't change workflow rules — those I escalate.
"""


def build_domain(ctx: BuildContext):
    reader = Agent(
        role="Workflow Inspector",
        goal="Detect a broken workflow run, auto-repair the deterministic steps, report the rest.",
        backstory="You inspect a workflow run step by step using its declared signals, re-run the "
        "steps that are safely repairable, and write a clear report + recommendation for "
        "anything that needs attention. You never fabricate output and never change "
        "workflow rules — you fix what is deterministically fixable and escalate the rest.",
        llm=ctx.llm,
        # A stalled step is very often a MODE problem, not a broken step: only a task-runner's task
        # auto-activates, so an interactive agent's task sits in `queued` forever and reports
        # "Only active tasks can be completed". Without this the inspector diagnoses the symptom.
        skills=ctx.skills,
        tools=[],
    )
    task = Task(
        description=(
            f"Today is {ctx.today}. Request: '{ctx.prompt}'\n\n"
            "Inspect the day's workflow run and report. The deterministic inspection runs "
            "in code; just report the outcome it produces."
        ),
        agent=reader,
        expected_output="The workflow inspection report (per-step state + actions + recommendations).",
    )
    return ([reader], [task])


def run() -> None:
    def _poll() -> None:
        # After the evening deadline, inspect today's Sanomat workflow; auto-repair + report.
        now = datetime.datetime.now(_TZ)
        if (now.hour, now.minute) < (18, 30):
            return
        date = now.date().isoformat()
        res = inspect("laimeat-sanomat-evening", {"date": date, "edition": "evening"})
        publish_inspection(res)
        if res["overall"] != "GREEN" or res["actions"]:
            print(
                f"[{AGENT_NAME}] {date}: {res['overall']} — fixed={res['fixed']} still_red={res['still_red']}",
                flush=True,
            )

    run_crew(
        CrewSpec(
            agent_name=AGENT_NAME,
            build_domain=build_domain,
            readme_md=README,
            temperature=0.2,
            skills=SKILLS,
            idle_hook=_poll,
            idle_hook_seconds=300,
        )
    )


if __name__ == "__main__":
    run()

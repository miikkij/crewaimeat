"""workflow-inspector: detect a broken workflow run, auto-repair the deterministic steps, and
report the rest — so a chained pipeline (e.g. the (L)AIMEAT Sanomat newspaper) is never silently
incomplete again.

Reference implementation of the crew-side half of the Agent Workflows feature
(docs/internal/2026-06-13-agent-workflows-node-spec.md). Once the node engine ships, the node
invokes this agent on a failed step signal; until then its idle hook checks the day's workflow
after the evening deadline and acts. Deterministic check; LLM only for an `llm` signal leaf.

Register + run:
  npx aimeat@latest connect add --agent workflow-inspector --mode task-runner --url https://aimeat.io --owner <you>
  uv run python crews/workflow_inspector_crew.py
"""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.workflow_inspector import inspect, publish_inspection

AGENT_NAME = "workflow-inspector"
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
            idle_hook=_poll,
            idle_hook_seconds=300,
        )
    )


if __name__ == "__main__":
    run()

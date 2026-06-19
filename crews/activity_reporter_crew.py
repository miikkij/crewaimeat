"""activity-reporter: turns the workspace/organism activity feed into per-period reports.

A general-purpose workspace-contract agent. Reads `activity-tracking` config records (which workspace / "*"
for the whole organism, period, narrator), gathers the activity delta (who did what, when — from the
member-gated activity feed), and writes an `activity-report` DOCUMENT: a digest + changelog + attribution +
the story of what happened, narrated by a character. The loop is deterministic (crewaimeat.activity_contract);
only the report prose is the LLM's job (owl-alpha). It posts nothing external.

Reusable downstream: standup/digest · changelog/release notes · build-in-public (-> SOME pipeline) ·
attribution/credit · a running project log.

Quick test (after registering):
  uv run python -c "from crewaimeat.activity_contract import process_activity_reports; print(process_activity_reports())"

Run as a crew:
  npx aimeat@latest connect add --agent activity-reporter --mode task-runner --url https://aimeat.io --owner <you>
  uv run python crews/activity_reporter_crew.py
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.activity_contract import CONTRACT, make_activity_tools, process_activity_reports
from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.contract_adopt import build_adopt_domain, is_adopt_task

AGENT_NAME = "activity-reporter"

README = """[[FIGLET:slant]["Activity Reporter"]]

Turns the workspace / organism **activity feed** into per-period **reports** — a digest + changelog +
attribution + the **story of what happened, narrated by a character**. Reads `activity-tracking` config
records (which workspace, or `*` for the whole organism; period; narrator), gathers the activity delta
(who did what, when), and writes an `activity-report` **document**. Deterministic; the LLM (owl-alpha) only
writes the prose. **It posts nothing external.** Reusable as standup, changelog, build-in-public, credit.

**How to task me:** "report" — I run process_activity_reports ONCE and write any due reports.
"""


def build_domain(ctx: BuildContext):
    if is_adopt_task(ctx.task):  # UI "Adopt contract" chip -> provision our spaces there
        return build_adopt_domain(ctx, AGENT_NAME, CONTRACT)
    reporter = Agent(
        role="Activity Chronicler",
        goal="Turn DUE activity-tracking configs into activity-report documents — digest, changelog, story.",
        backstory="You read the activity feed (who did what, when) for the configured workspace or the whole "
        "organism, and write a report document with a digest, a changelog, attribution, and a short "
        "in-character story. You call process_activity_reports ONCE and report. You never post "
        "anywhere external; you never fabricate — only the real activity events.",
        llm=ctx.llm,
        tools=[*make_activity_tools(AGENT_NAME)],
    )

    report_task = Task(
        description=(
            f"Today is {ctx.today}. Request: '{ctx.prompt}'\n\n"
            "Call process_activity_reports() EXACTLY ONCE. It deterministically finds DUE activity-tracking "
            "configs, gathers each one's activity delta, distils it into a report document (digest + changelog "
            "+ attribution + an in-character story), and advances the config. Report the counts. Post NOTHING."
        ),
        agent=reporter,
        expected_output="The process_activity_reports report: how many activity-report documents were written.",
    )

    return ([reporter], [report_task])


def run() -> None:
    # idle_hook: a DETERMINISTIC poll (~5 min) that generates any DUE activity reports. The CHECK uses NO LLM
    # (read configs + activity feed + delta math); owl-alpha runs only to write a report that is actually due.
    def _poll() -> None:
        res = process_activity_reports()
        if res.get("reports") or res.get("failed"):
            print(f"[{AGENT_NAME}] activity-report poll: {res}")

    run_crew(
        CrewSpec(
            agent_name=AGENT_NAME,
            build_domain=build_domain,
            readme_md=README,
            temperature=0.5,
            idle_hook=_poll,
            idle_hook_seconds=300,
        )
    )


if __name__ == "__main__":
    run()

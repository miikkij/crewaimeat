"""some-analyst: drafts reply suggestions for Social Radar opportunities (a human approves + posts).

Reads the `opportunity` records in the Social Radar workspace (written by some-listener + the Grok scout)
and drafts a value-first `reply-draft` (status=draft) for each fresh, worth-it one. The loop + dedup are
deterministic (crewaimeat.some_analyst.draft_opportunities); only the reply prose is the LLM's job, following
the playbook (value first, disclose builder, mention AIMEAT only when it truly fits, never astroturf).
**A HUMAN reviews, approves, and posts — this crew NEVER posts, replies, or contacts anyone.**

Quick test (after registering the agent):
  uv run python -c "from crewaimeat.some_analyst import draft_opportunities; print(draft_opportunities(limit=3))"

Run as a crew:
  npx aimeat@latest connect add --agent some-analyst --mode task-runner --url https://aimeat.io --owner <you>
  uv run python crews/some_analyst_crew.py
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.some_analyst import make_analyst_tools

AGENT_NAME = "some-analyst"

README = '''[[FIGLET:slant]["Some Analyst"]]

Drafts **value-first reply suggestions** for Social Radar opportunities. Reads the `opportunity` records
(some-listener + Grok scout wrote them) and writes a `reply-draft` (status=draft) for each fresh, worth-it
one — following the playbook (value first, disclose builder, mention AIMEAT only when it truly fits, never
astroturf). **A HUMAN reviews, approves, and posts — this crew never posts or contacts anyone.**

**How to task me:** "draft" — I run draft_opportunities ONCE and report how many reply-drafts I wrote.
'''


def build_domain(ctx: BuildContext):
    analyst = Agent(
        role="Engagement Drafter",
        goal="Draft genuine, value-first reply suggestions for fresh Social Radar opportunities — drafts only.",
        backstory="You read the fresh opportunities in the Social Radar workspace and draft a helpful reply "
                  "for each worth-it one, following the playbook. You call draft_opportunities ONCE and "
                  "report. You never post, reply, vote, or contact anyone — a human reviews your drafts, "
                  "approves, and posts. You never fabricate.",
        llm=ctx.llm,
        tools=[*make_analyst_tools(AGENT_NAME)],
    )

    draft_task = Task(
        description=(
            f"Today is {ctx.today}. Request: '{ctx.prompt}'\n\n"
            "1. Decide a sensible limit (default 5) of opportunities to draft for.\n"
            "2. Call draft_opportunities(limit=<n>) EXACTLY ONCE. It deterministically reads the fresh "
            "Social Radar opportunities, drafts a value-first reply for each worth-it one, and writes "
            "reply-draft records (status=draft) — you do NOT write the replies by hand.\n"
            "3. Report how many reply-drafts it wrote. Post NOTHING; contact no one — a human approves + posts."
        ),
        agent=analyst,
        expected_output="The draft_opportunities report: how many reply-drafts were written for review.",
    )

    return ([analyst], [draft_task])


def run() -> None:
    run_crew(CrewSpec(agent_name=AGENT_NAME, build_domain=build_domain, readme_md=README, temperature=0.6))


if __name__ == "__main__":
    run()

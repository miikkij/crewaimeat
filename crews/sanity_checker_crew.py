"""sanity-checker: stress-test an idea from multiple angles, then advise.

Generated on the AIMEAT scaffold (crewaimeat). Edit build_domain to taste; the scaffold
provides the AIMEAT wiring (see SCAFFOLD_CANON.md). Register first:
  npx aimeat@latest connect add --agent sanity-checker --mode task-runner --url https://aimeat.io --owner <your-aimeat-account>

Run: uv run python crews/sanity_checker_crew.py
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.crew import _web_tools

AGENT_NAME = "sanity-checker"

README = """[[FIGLET:slant]["SANITY CHECK"]]

# sanity-checker — stress-test an idea, then advise

A skeptic, a pragmatist, and an advisor pull your idea apart: risks, hidden assumptions,
feasibility, and a grounded verdict (with a better alternative if one genuinely exists).
Shaky factual claims get checked with web search.

## How to task me
Queue an idea to pressure-test:
- `Launch a paid newsletter about local hiking trails`
- `Replace our REST API with GraphQL next quarter`
- `Open a board-game cafe in a small university town`
"""


def build_domain(ctx: BuildContext) -> tuple[list[Agent], list[Task]]:
    llm, today, idea = ctx.llm, ctx.today, ctx.prompt

    skeptic = Agent(
        role="Skeptic",
        goal="Find the holes, hidden assumptions, risks, and failure modes in the idea",
        backstory=(
            "You are a constructive devil's advocate. You surface what could go wrong and what "
            "the idea quietly assumes, and you check shaky claims against current sources."
        ),
        tools=_web_tools(),
        llm=llm,
        verbose=True,
    )
    pragmatist = Agent(
        role="Pragmatist",
        goal="Judge feasibility: resources, cost, effort, prerequisites, and real-world precedents",
        backstory=(
            "You ask whether this can actually be built and run. You look up comparable attempts and what they needed."
        ),
        tools=_web_tools(),
        llm=llm,
        verbose=True,
    )
    advisor = Agent(
        role="Advisor",
        goal="Weigh the critiques and give a clear verdict, recommendations, and a better alternative if one exists",
        backstory="You synthesize the skeptic's and pragmatist's findings into a grounded, honest recommendation.",
        llm=llm,
        verbose=True,
    )

    critique = Task(
        description=(
            f"{today}\n\nStress-test this idea. List the key risks, hidden assumptions, and failure "
            f"modes, and verify any shaky factual claims with web search. Idea:\n{idea}"
        ),
        expected_output="A focused list of risks, assumptions, and failure modes, with sources for any factual checks.",
        agent=skeptic,
    )
    feasibility = Task(
        description=(
            f"{today}\n\nAssess whether this idea is feasible: the resources, cost, effort, and "
            f"prerequisites it needs, and how comparable attempts have fared (use web search). Idea:\n{idea}"
        ),
        expected_output="A feasibility read: what it takes, the main practical obstacles, and relevant precedents.",
        agent=pragmatist,
    )
    verdict = Task(
        description=(
            "Using the skeptic's critique and the pragmatist's feasibility read above, give the final "
            "assessment of this idea:\n"
            "1. A clear verdict: does it make sense, and under what conditions?\n"
            "2. Concrete recommendations to make it work or to de-risk it.\n"
            "3. A better alternative if one is genuinely stronger; otherwise say the idea stands.\n"
            f"Keep it honest and grounded in the critiques above. Original idea:\n{idea}"
        ),
        expected_output="A verdict, recommendations, and a better alternative (or a note that the idea stands).",
        agent=advisor,
        context=[critique, feasibility],
    )

    return [skeptic, pragmatist, advisor], [critique, feasibility, verdict]


def run() -> None:
    # Critical analysis wants consistency, not divergence — run cool.
    run_crew(CrewSpec(agent_name=AGENT_NAME, build_domain=build_domain, readme_md=README, temperature=0.2))


if __name__ == "__main__":
    run()

"""research-crew — the canonical example crew on the AIMEAT scaffold.

This is the reference TEMPLATE. To make your own crew: copy this file, change
AGENT_NAME (it must match the name you used in `aimeat connect add`), and edit
ONLY `build_domain` — your agents and their tasks. Everything AIMEAT-related
(onboarding, daemon, liaison publish/complete, live progress, date injection) is
handled by crewaimeat.aimeat_crew.run_crew — reuse it as-is.

Run:
    uv run python -m crewaimeat.research_crew
    # or, after `pip install`:  research-crew
"""

from __future__ import annotations

import os

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.crew import _web_tools

AGENT_NAME = "research-crew"


def build_domain(ctx: BuildContext) -> tuple[list[Agent], list[Task]]:
    """Define the domain agents + tasks for this crew. (Customize this only.)

    Returns (agents, tasks). Tasks run in order; the LAST task's output is what
    the liaison publishes to AIMEAT memory and uses as the completion summary.
    """
    llm, today, prompt = ctx.llm, ctx.today, ctx.prompt

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

    return [researcher, analyst, writer], [research, analysis, writing]


def run() -> None:
    run_crew(
        CrewSpec(
            agent_name=AGENT_NAME,
            build_domain=build_domain,
            poll_seconds=int(os.getenv("RESEARCH_CREW_POLL_SECONDS", "30")),
            temperature=0.25,  # factual research — run cool to stay grounded
        )
    )


if __name__ == "__main__":
    run()

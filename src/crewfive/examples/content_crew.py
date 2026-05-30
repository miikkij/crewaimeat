"""A three-agent crew that researches a topic, builds a structured outline, and writes a polished final draft of the requested content piece.

Generated example crew on the AIMEAT scaffold. Edit build_domain to taste;
the scaffold provides the AIMEAT wiring (see SCAFFOLD_CANON.md). Register first:
  aimeat connect add --agent content-crew --mode task-runner --url https://aimeat.io --owner <owner>

Run: python -m crewfive.examples.content_crew
"""

from __future__ import annotations

from crewai import Agent, Task

from crewfive.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewfive.crew import _web_tools

AGENT_NAME = "content-crew"


def build_domain(ctx: BuildContext) -> tuple[list[Agent], list[Task]]:
    researcher = Agent(
        role="Topic Researcher",
        goal="Gather accurate, current facts, angles, and supporting details about the topic so the content is well-informed and credible.",
        backstory="You are a diligent content researcher who quickly maps out what matters about a topic and verifies facts against current sources on the web. You distill noise into the handful of points that make a piece genuinely useful.",
        tools=_web_tools(),
        llm=ctx.llm,
        verbose=True,
    )
    strategist = Agent(
        role="Content Strategist",
        goal="Turn the research and the user's request into a clear, audience-appropriate outline with a strong angle, structure, and key talking points.",
        backstory="You are a content strategist who shapes raw material into a compelling narrative arc. You decide the angle, the structure, and the beats that will resonate with the intended audience and format.",
        tools=[],
        llm=ctx.llm,
        verbose=True,
    )
    writer = Agent(
        role="Content Writer",
        goal="Write a polished, engaging final draft that follows the outline and matches the requested format, tone, and audience.",
        backstory="You are a professional writer who turns outlines into crisp, vivid, ready-to-publish prose. You adapt voice and structure to the format, whether a blog post, article, or social thread.",
        tools=[],
        llm=ctx.llm,
        verbose=True,
    )

    research_topic = Task(
        description=f"{ctx.today}\n\n" + "Read the user's content request below and identify the topic, intended format (blog post, article, or social thread), audience, and any constraints. Research the topic and gather the most relevant, accurate, and current facts, statistics, examples, and angles that would make the piece informative and credible. Note anything time-sensitive or recently changed. Capture sources for any specific claims." + f"\n\nRequest:\n{ctx.prompt}",
        expected_output="A concise briefing: the inferred topic/format/audience, 5-8 key facts or angles worth covering (with sources for specific claims), and any notable recent developments.",
        agent=researcher,
    )
    build_outline = Task(
        description="Using the research briefing, design a clear outline for the content piece. Choose a strong central angle and a hook. Lay out the structure appropriate to the requested format (e.g., headline plus intro, body sections with headings, and conclusion/CTA for a blog or article; or a numbered sequence of posts for a social thread). Under each section, list the key points or talking points to cover. Keep it tight and audience-appropriate.",
        expected_output="A structured outline: working title/hook, the chosen angle, and ordered sections (or thread beats) each with bullet talking points.",
        agent=strategist,
    )
    write_draft = Task(
        description=f"{ctx.today}\n\n" + "Write the polished final draft following the outline and the research. Match the format, tone, and audience implied by the original request below, and honor any explicit instructions (language, length, style, platform); otherwise choose what fits best. For a blog post or article, include a title, an engaging introduction, well-developed sections with headings, and a strong conclusion. For a social thread, write numbered posts that flow naturally and respect typical length limits. Present the approved outline at the top, then the full final draft beneath it. If you mention any date, use the current time provided above." + f"\n\nRequest:\n{ctx.prompt}",
        expected_output="The deliverable: first the outline, then a complete, polished, ready-to-publish draft in the requested format.",
        agent=writer,
    )

    return [researcher, strategist, writer], [research_topic, build_outline, write_draft]


def run() -> None:
    run_crew(CrewSpec(agent_name=AGENT_NAME, build_domain=build_domain))


if __name__ == "__main__":
    run()

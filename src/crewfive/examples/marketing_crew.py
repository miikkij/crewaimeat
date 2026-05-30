"""A crew that researches the market and builds a concrete, KPI-driven marketing plan for a product or service the user describes.

Generated example crew on the AIMEAT scaffold. Edit build_domain to taste;
the scaffold provides the AIMEAT wiring (see SCAFFOLD_CANON.md). Register first:
  aimeat connect add --agent marketing-crew --mode task-runner --url https://aimeat.io --owner <owner>

Run: python -m crewfive.examples.marketing_crew
"""

from __future__ import annotations

from crewai import Agent, Task

from crewfive.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewfive.crew import _web_tools

AGENT_NAME = "marketing-crew"


def build_domain(ctx: BuildContext) -> tuple[list[Agent], list[Task]]:
    market_researcher = Agent(
        role="Market & Audience Researcher",
        goal="You uncover the target audience, competitors, and current market trends for the product or service the user describes, grounding the plan in real, up-to-date facts.",
        backstory="You are a sharp market analyst who turns scattered signals into clear audience segments and competitive insight. You favor specific, verifiable findings over generic assumptions.",
        tools=_web_tools(),
        llm=ctx.llm,
        verbose=True,
    )
    strategist = Agent(
        role="Marketing Strategist",
        goal="You translate research into a sharp positioning statement, prioritized audience segments, channel strategy, and core messaging pillars.",
        backstory="You are a seasoned brand strategist who has launched products across B2B and consumer markets. You make decisive, defensible recommendations and tie every choice back to the audience.",
        tools=[],
        llm=ctx.llm,
        verbose=True,
    )
    plan_writer = Agent(
        role="Campaign Planner & Writer",
        goal="You assemble the research and strategy into a concrete, ready-to-execute marketing plan with channels, messaging, a phased timeline, and measurable KPIs.",
        backstory="You are an experienced campaign manager who writes clear, actionable plans that teams can run without further clarification. You always attach concrete metrics and targets to recommendations.",
        tools=[],
        llm=ctx.llm,
        verbose=True,
    )

    research_task = Task(
        description=f"{ctx.today}\n\n" + "Read the user's request describing their product or service. Identify and profile the target audience (2-4 distinct segments with needs, pain points, and buying triggers), the competitive landscape (3-5 key competitors and how they position themselves), and 3-5 current market or category trends relevant to this offering. Note any differentiators the product can credibly claim. Present findings as concise, organized notes that the strategist can build on." + f"\n\nRequest:\n{ctx.prompt}",
        expected_output="A structured research brief: audience segments with profiles, a competitor overview with each competitor's positioning, current market trends, and candidate differentiators for the product.",
        agent=market_researcher,
    )
    strategy_task = Task(
        description="Using the research brief, define the marketing strategy. Write a single clear positioning statement, rank the audience segments by priority with rationale, recommend the marketing channels best suited to reach the priority segments (with reasoning for each), and define 3-4 core messaging pillars with example messaging per pillar. Keep recommendations specific and tied to the audience insights.",
        expected_output="A strategy section: positioning statement, prioritized audience segments with rationale, recommended channels with reasoning, and core messaging pillars with example copy.",
        agent=strategist,
    )
    plan_task = Task(
        description="Combine the research brief and the strategy into a single, polished marketing plan. Include: an executive summary, positioning, prioritized target audience, channel plan, messaging (pillars plus sample copy/headlines for the top channels), a phased rollout timeline with key activities, and a KPI section that lists 5-8 measurable success metrics with concrete target ranges and how each will be tracked. Make it concrete enough that a team could execute it directly. Write in clear, professional prose with clear section headings." + f"\n\nRequest:\n{ctx.prompt}",
        expected_output="A complete, well-structured marketing plan in Markdown covering executive summary, positioning, audience, channels, messaging with sample copy, a phased timeline, and a KPI section with specific measurable targets.",
        agent=plan_writer,
    )

    return [market_researcher, strategist, plan_writer], [research_task, strategy_task, plan_task]


def run() -> None:
    run_crew(CrewSpec(agent_name=AGENT_NAME, build_domain=build_domain))


if __name__ == "__main__":
    run()

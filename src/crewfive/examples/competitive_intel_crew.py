"""A four-agent crew that researches a market or named competitors and produces a competitive intelligence brief covering landscape, strengths/weaknesses, and opportunities.

Generated example crew on the AIMEAT scaffold. Edit build_domain to taste;
the scaffold provides the AIMEAT wiring (see SCAFFOLD_CANON.md). Register first:
  aimeat connect add --agent competitive-intel-crew --mode task-runner --url https://aimeat.io --owner <owner>

Run: python -m crewfive.examples.competitive_intel_crew
"""

from __future__ import annotations

from crewai import Agent, Task

from crewfive.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewfive.crew import _web_tools

AGENT_NAME = "competitive-intel-crew"


def build_domain(ctx: BuildContext) -> tuple[list[Agent], list[Task]]:
    market_researcher = Agent(
        role="Market Researcher",
        goal="Gather current, factual intelligence on the target market and the named competitors using the web",
        backstory="You are a diligent competitive-intelligence researcher who scours the web for recent product launches, pricing, funding, market share, customer sentiment, and positioning. You always favor current sources and cite where each fact comes from.",
        tools=_web_tools(),
        llm=ctx.llm,
        verbose=True,
    )
    competitor_analyst = Agent(
        role="Competitor Analyst",
        goal="Turn raw findings into a structured per-competitor profile of strengths, weaknesses, and differentiators",
        backstory="You are a sharp competitive analyst who reads a pile of facts and distills each player's positioning, advantages, vulnerabilities, and target segments without speculation.",
        tools=[],
        llm=ctx.llm,
        verbose=True,
    )
    strategy_advisor = Agent(
        role="Strategy Advisor",
        goal="Identify market gaps, threats, and concrete opportunities from the competitive analysis",
        backstory="You are a seasoned strategist who spots whitespace, emerging trends, and defensible openings in a competitive landscape and frames them as actionable opportunities.",
        tools=[],
        llm=ctx.llm,
        verbose=True,
    )
    brief_writer = Agent(
        role="Brief Writer",
        goal="Write a clear, well-structured competitive intelligence brief for decision-makers",
        backstory="You are a professional business writer who produces concise, scannable briefs with clear sections, comparison tables, and prioritized recommendations.",
        tools=[],
        llm=ctx.llm,
        verbose=True,
    )

    research = Task(
        description=f"{ctx.today}\n\n" + "Research the market and/or named competitors described in the request below. Use web search to gather current facts: who the major players are, their products and services, pricing, positioning, recent news (launches, funding, partnerships, leadership changes), approximate market share or scale, and observable customer sentiment. If the request names specific competitors, focus on them; if it names only a market, identify and profile the leading players. Collect concrete facts with their sources and dates." + f"\n\nRequest:\n{ctx.prompt}",
        expected_output="A structured set of findings organized per competitor and for the overall market, each fact attributed to a source with its date where available.",
        agent=market_researcher,
    )
    analyze = Task(
        description="Using the research findings, build a profile for each competitor capturing: positioning and target segment, key strengths and competitive advantages, weaknesses and vulnerabilities, and notable differentiators. Then summarize the overall competitive landscape: how the players cluster, where competition is most intense, and the basis of competition (price, features, brand, distribution). Base everything strictly on the gathered facts; do not invent details.",
        expected_output="A per-competitor strengths/weaknesses/differentiators profile plus a concise summary of how the landscape is structured.",
        agent=competitor_analyst,
    )
    opportunities = Task(
        description="From the competitor profiles and landscape summary, identify the most important strategic insights: unmet customer needs and market gaps (whitespace), emerging trends shaping the market, the biggest competitive threats, and 3-6 concrete, prioritized opportunities. For each opportunity, briefly note why it exists and which competitor weakness or market gap it exploits.",
        expected_output="A short list of market gaps, threats, and 3-6 prioritized opportunities, each with a one-line rationale.",
        agent=strategy_advisor,
    )
    write_brief = Task(
        description=f"{ctx.today}\n\n" + "Write the final competitive intelligence brief based on the research, competitor analysis, and opportunities. Use a clear structure: title, executive summary, market landscape overview, a competitor comparison (use a table where it helps), strengths and weaknesses by player, and prioritized opportunities with recommendations. Keep it concise and decision-ready. If the result mentions a date, use the current time given above. Follow any explicit instructions in the original request below (e.g. requested language, format, audience, named competitors); otherwise choose what fits best." + f"\n\nRequest:\n{ctx.prompt}",
        expected_output="A complete, well-structured competitive intelligence brief covering landscape, strengths/weaknesses, and prioritized opportunities.",
        agent=brief_writer,
    )

    return [market_researcher, competitor_analyst, strategy_advisor, brief_writer], [research, analyze, opportunities, write_brief]


def run() -> None:
    run_crew(CrewSpec(agent_name=AGENT_NAME, build_domain=build_domain))


if __name__ == "__main__":
    run()

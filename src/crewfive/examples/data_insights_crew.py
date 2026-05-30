"""Analyzes user-provided data, metrics, or findings and produces an insights summary with clear conclusions and recommended actions.

Generated example crew on the AIMEAT scaffold. Edit build_domain to taste;
the scaffold provides the AIMEAT wiring (see SCAFFOLD_CANON.md). Register first:
  aimeat connect add --agent data-insights-crew --mode task-runner --url https://aimeat.io --owner <owner>

Run: python -m crewfive.examples.data_insights_crew
"""

from __future__ import annotations

from crewai import Agent, Task

from crewfive.aimeat_crew import BuildContext, CrewSpec, run_crew

AGENT_NAME = "data-insights-crew"


def build_domain(ctx: BuildContext) -> tuple[list[Agent], list[Task]]:
    analyst = Agent(
        role="Data Analyst",
        goal="Examine the data, metrics, or findings the user provides and surface the patterns, trends, anomalies, and relationships that matter most.",
        backstory="You are a rigorous data analyst who reads numbers and findings carefully, distinguishing signal from noise and never inventing figures that are not in the provided data.",
        tools=[],
        llm=ctx.llm,
        verbose=True,
    )
    strategist = Agent(
        role="Insights Strategist",
        goal="Turn the analytical observations into 3-5 clear, defensible conclusions and concrete recommended actions tied to the evidence.",
        backstory="You are a seasoned strategist who translates analysis into decisions, framing each conclusion around what it means and what the user should do next.",
        tools=[],
        llm=ctx.llm,
        verbose=True,
    )
    writer = Agent(
        role="Insights Writer",
        goal="Compose a clear, well-structured insights summary that communicates the conclusions and recommended actions to a decision-maker.",
        backstory="You are a professional writer who produces concise, structured business summaries with concrete takeaways and no filler.",
        tools=[],
        llm=ctx.llm,
        verbose=True,
    )

    analyze = Task(
        description="Carefully examine the data, metrics, or findings contained in the user's request below. Identify the most important patterns, trends, comparisons, outliers, and relationships. Work only from the information provided; do not fabricate numbers or assume data that is not present. If the data is incomplete or ambiguous, note what is missing or uncertain." + f"\n\nRequest:\n{ctx.prompt}",
        expected_output="A structured set of analytical observations: the key patterns, trends, notable figures, anomalies, and any data gaps or caveats.",
        agent=analyst,
    )
    conclude = Task(
        description="Using the analytical observations from the previous step, derive 3-5 clear, evidence-backed conclusions about what the data means. For each conclusion, recommend a concrete, actionable next step. Prioritize the conclusions by importance or impact, and make sure every recommendation is grounded in the observations rather than speculation.",
        expected_output="A prioritized list of 3-5 conclusions, each paired with a specific recommended action and the evidence that supports it.",
        agent=strategist,
    )
    summarize = Task(
        description="Write the final insights summary based on the analysis and conclusions. Structure it as: a short title; an executive summary (2-4 sentences); a 'Key Findings' section; a 'Conclusions' section; and a 'Recommended Actions' section with concrete next steps. Keep it concise and decision-focused. Follow any explicit instructions in the original request below regarding language, format, or audience; otherwise choose what fits best." + f"\n\nRequest:\n{ctx.prompt}",
        expected_output="A structured insights summary with a title, executive summary, key findings, conclusions, and recommended actions.",
        agent=writer,
    )

    return [analyst, strategist, writer], [analyze, conclude, summarize]


def run() -> None:
    run_crew(CrewSpec(agent_name=AGENT_NAME, build_domain=build_domain))


if __name__ == "__main__":
    run()

"""TEMPLATE — copy this file and mold it into your own AIMEAT crew.

Edit only these spots:
  1. Set AGENT_NAME to the name you registered with `aimeat connect add`.
  2. In build_domain(), define your domain agents and their tasks.
  3. (Optional) choose poll_seconds / memory_key_prefix in CrewSpec.

The scaffold handles the rest: crewaimeat.aimeat_crew.run_crew already provides the
AIMEAT onboarding, the task daemon, task completion, memory writes, and live
progress — all verified end-to-end. Keep your edits to build_domain and let the
scaffold do the rest (background: SCAFFOLD_CANON.md).

Prerequisites (see the project README / setup guide):
  - `aimeat connect add --agent <AGENT_NAME> --mode task-runner --url https://aimeat.io --owner <you>`
    then approve it in the dashboard.
  - An OPENROUTER_API_KEY in .env. Model via OPENROUTER_MODEL:
      * testing / free:  openrouter/owl-alpha  (the scaffold tolerates its hiccups)
      * fast / reliable: a paid model (e.g. a strong Opus/Sonnet tier) — "works first try"
  - Run:  python -m crewaimeat.templates.example_crew

The crew runs as a daemon: it waits for tasks queued to <AGENT_NAME> on AIMEAT
and processes each one (your agents do the work; the liaison publishes the
result to memory and completes the task).
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.crew import _web_tools  # Tavily web search if TAVILY_API_KEY is set, else []

# === CUSTOMIZE 1: your AIMEAT agent identity =============================== #
AGENT_NAME = "my-crew"  # must match `aimeat connect add --agent ...`


# === CUSTOMIZE 2: your domain agents + tasks ============================== #
def build_domain(ctx: BuildContext) -> tuple[list[Agent], list[Task]]:
    """Return (agents, tasks). Tasks run in order; the LAST task's output is
    what gets published to AIMEAT memory + used as the completion summary.

    ctx.llm    -> pass to every Agent(llm=ctx.llm)
    ctx.prompt -> the user's request (the task text)
    ctx.today  -> current-time string; prepend to any time-sensitive task
    """
    # Example: a single worker. Replace with your real roster (2–4 agents is typical).
    worker = Agent(
        role="Worker",  # e.g. "Market Researcher"
        goal="Do the core work for the task",
        backstory="You are an expert at this domain and produce concrete, useful output.",
        tools=_web_tools(),  # drop if this agent needs no web search
        llm=ctx.llm,
        verbose=True,
    )

    work = Task(
        description=f"{ctx.today}\n\nDo the task:\n{ctx.prompt}",
        expected_output="The concrete deliverable for the task.",
        agent=worker,
    )

    # Multi-step example (uncomment + add agents):
    # return [researcher, analyst, writer], [research, analysis, writing]
    return [worker], [work]


def run() -> None:
    run_crew(
        CrewSpec(
            agent_name=AGENT_NAME,
            build_domain=build_domain,
            # process=Process.sequential,   # default; sequential is the validated path
            # poll_seconds=30,
            # memory_key_prefix="crews.my-crew",
        )
    )


if __name__ == "__main__":
    run()

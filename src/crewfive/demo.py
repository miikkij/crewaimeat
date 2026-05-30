"""Lightweight 3-agent sequential crew as an AIMEAT task-runner.

researcher -> analyst -> writer (Process.sequential). Real implementation: a
real LLM (via OpenRouter/xAI `get_llm()`) and real Tavily web search (if
TAVILY_API_KEY is set). Prints a Deliverable JSON.

Run as an AIMEAT task-runner:
    AIMEAT_TASK_PROMPT="Make a small marketing plan ..." \
    uv run python -m crewfive.demo
"""

from __future__ import annotations

import sys

from dotenv import load_dotenv

from crewfive.aimeat import (
    Deliverable,
    coerce_deliverable,
    emit_deliverable,
    force_utf8_stdout,
    read_runner_env,
    write_memory_note,
)


def _build_and_run(prompt: str):
    from crewai import Agent, Crew, Process, Task

    from crewfive.crew import _web_tools
    from crewfive.llm import get_llm

    llm = get_llm()
    tools = _web_tools()  # real Tavily if TAVILY_API_KEY is set, otherwise empty

    researcher = Agent(
        role="Researcher",
        goal="Gather relevant, up-to-date background for the task: {request}",
        backstory=(
            "You are a thorough researcher who assembles facts and sources before analysis. "
            "You use web search to make sure the information is current."
        ),
        tools=tools,
        llm=llm,
        verbose=False,
    )
    analyst = Agent(
        role="Analyst",
        goal="Analyze the researcher's findings and identify the key conclusions",
        backstory="You are a sharp analyst who separates the essential and draws conclusions.",
        llm=llm,
        verbose=False,
    )
    writer = Agent(
        role="Writer",
        goal="Write a clear, actionable final result based on the analysis",
        backstory="You are a professional writer who produces structured, concrete text.",
        llm=llm,
        verbose=False,
    )

    research_task = Task(
        description="Research the task and gather the key background: {request}",
        expected_output="A list of the key findings and facts with sources.",
        agent=researcher,
    )
    analysis_task = Task(
        description="Analyze the findings and identify 3–5 key conclusions.",
        expected_output="A short analysis with the key conclusions.",
        agent=analyst,
    )
    writing_task = Task(
        description=(
            "Write the final result based on the analysis for the task: {request}. "
            "Include a title, summary, sections and concrete recommendations."
        ),
        expected_output=(
            "A structured final result: title, summary, sections and recommendations."
        ),
        agent=writer,
        output_pydantic=Deliverable,  # ask for structured JSON directly
    )

    crew = Crew(
        agents=[researcher, analyst, writer],
        tasks=[research_task, analysis_task, writing_task],
        process=Process.sequential,
        verbose=False,
    )
    return crew.kickoff(inputs={"request": prompt})


def run() -> None:
    force_utf8_stdout()
    load_dotenv()

    env = read_runner_env()
    print(
        f"[crewfive.demo] task={env.task_id} agent={env.agent_name}\n"
        f"[crewfive.demo] Directive: {env.prompt}",
        file=sys.stderr,
    )

    if env.task_id:
        write_memory_note(
            key=f"crews/demo/tasks/{env.task_id}/started",
            value={"prompt": env.prompt, "status": "started"},
            tags=["crewfive", "demo"],
        )

    try:
        result = _build_and_run(env.prompt)
        deliverable = coerce_deliverable(result, env.prompt)

        if env.task_id:
            write_memory_note(
                key=f"crews/demo/tasks/{env.task_id}/result",
                value={"title": deliverable.title, "summary": deliverable.summary},
                tags=["crewfive", "demo", "result"],
            )

        emit_deliverable(deliverable)

    except Exception as exc:  # noqa: BLE001
        print(f"[crewfive.demo] ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    run()

"""Kevyt 3 agentin sekventiaalinen kruu AIMEAT-task-runnerina.

researcher -> analyst -> writer (Process.sequential). Aito toteutus: oikea LLM
(OpenRouter/xAI `get_llm()`-kautta) ja oikea Tavily-web-haku (jos TAVILY_API_KEY
on asetettu). Tulostaa Deliverable-JSON:n.

Ajo AIMEAT-task-runnerina:
    AIMEAT_TASK_PROMPT="Tee pieni markkinointisuunnitelma ..." \
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
    tools = _web_tools()  # oikea Tavily jos TAVILY_API_KEY asetettu, muuten tyhjä

    researcher = Agent(
        role="Tutkija",
        goal="Kerää relevantti, ajantasainen taustatieto tehtävään: {request}",
        backstory=(
            "Olet perusteellinen tutkija, joka kokoaa faktat ja lähteet ennen analyysiä. "
            "Käytät web-hakua varmistaaksesi ajantasaisuuden."
        ),
        tools=tools,
        llm=llm,
        verbose=False,
    )
    analyst = Agent(
        role="Analyytikko",
        goal="Analysoi tutkijan löydökset ja tunnista keskeiset johtopäätökset",
        backstory="Olet terävä analyytikko, joka erottaa olennaisen ja tekee johtopäätökset.",
        llm=llm,
        verbose=False,
    )
    writer = Agent(
        role="Kirjoittaja",
        goal="Kirjoita selkeä, toteutuskelpoinen lopputulos analyysin pohjalta",
        backstory="Olet ammattikirjoittaja, joka tuottaa jäsenneltyä ja konkreettista tekstiä.",
        llm=llm,
        verbose=False,
    )

    research_task = Task(
        description="Tutki tehtävää ja kokoa keskeiset taustatiedot: {request}",
        expected_output="Lista keskeisistä löydöksistä ja faktoista lähteineen.",
        agent=researcher,
    )
    analysis_task = Task(
        description="Analysoi löydökset ja tunnista 3–5 keskeistä johtopäätöstä.",
        expected_output="Lyhyt analyysi keskeisine johtopäätöksineen.",
        agent=analyst,
    )
    writing_task = Task(
        description=(
            "Kirjoita lopputulos analyysin pohjalta tehtävään: {request}. "
            "Sisällytä otsikko, tiivistelmä, osiot ja konkreettiset suositukset."
        ),
        expected_output=(
            "Jäsennelty lopputulos: otsikko, tiivistelmä, osiot ja suositukset."
        ),
        agent=writer,
        output_pydantic=Deliverable,  # pyydetään strukturoitu JSON suoraan
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
        f"[crewfive.demo] Direktiivi: {env.prompt}",
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
        print(f"[crewfive.demo] VIRHE: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    run()

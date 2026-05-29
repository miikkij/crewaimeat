"""AIMEAT-task-runner: ajaa 5-agentin hierarkisen company-kruun (CrewFive).

Käynnistetään aliprosessina, esim. `aimeat connect serve`:n toimesta:

    AIMEAT_TASK_PROMPT="Tee Q3 markkinointisuunnitelma ..." \
    AIMEAT_TASK_ID="abc123" AIMEAT_AGENT_NAME="marketing-crew" AIMEAT_TOKEN="..." \
    uv run python -m crewfive.runner

Lukee tehtävän envistä, ajaa kruun, tulostaa Deliverable-JSON:n (stdout ja/tai
CREW_OUTPUT_FILE). Suuren/verbosen kruun kanssa suositellaan file-capturea
(output_capture: file:<path>) jotta stdout pysyy puhtaana.
"""

from __future__ import annotations

import sys

from dotenv import load_dotenv

from crewfive.aimeat import (
    coerce_deliverable,
    emit_deliverable,
    force_utf8_stdout,
    read_runner_env,
    write_memory_note,
)


def run() -> None:
    force_utf8_stdout()
    load_dotenv()

    env = read_runner_env()
    print(
        f"[crewfive.runner] Aloitetaan task={env.task_id} agent={env.agent_name}\n"
        f"[crewfive.runner] Direktiivi: {env.prompt}",
        file=sys.stderr,
    )

    # Best-effort: merkitse aloitus AIMEATin muistiin (jos aimeat-CLI saatavilla).
    if env.task_id:
        write_memory_note(
            key=f"crews/company/tasks/{env.task_id}/started",
            value={"prompt": env.prompt, "status": "started"},
            tags=["crewfive", "task-runner"],
        )

    try:
        # Tuodaan vasta load_dotenv():n jälkeen, jotta LLM saa avaimet ympäristöstä.
        from crewfive.crew import CrewFive
        from crewfive.main import _save_outputs

        crew = CrewFive(verbose=False).crew()
        result = crew.kickoff(inputs={"request": env.prompt})

        deliverable = coerce_deliverable(result, env.prompt)

        # Paikallinen arkisto (md + json) – uudelleenkäytetään olemassa olevaa apuria.
        try:
            _save_outputs(env.prompt, result)
        except Exception as exc:  # noqa: BLE001 – arkistointi ei saa kaataa ajoa
            print(f"[crewfive.runner] Arkistointi epäonnistui: {exc}", file=sys.stderr)

        # Best-effort: tallenna lopputuloksen tiivistelmä AIMEATin muistiin.
        if env.task_id:
            write_memory_note(
                key=f"crews/company/tasks/{env.task_id}/result",
                value={"title": deliverable.title, "summary": deliverable.summary},
                tags=["crewfive", "task-runner", "result"],
            )

        emit_deliverable(deliverable)

    except Exception as exc:  # noqa: BLE001 – ilmoita epäonnistuminen non-zero exitillä
        print(f"[crewfive.runner] VIRHE: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    run()

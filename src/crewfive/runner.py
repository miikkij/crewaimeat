"""AIMEAT task-runner: runs the 5-agent hierarchical company crew (CrewFive).

Launched as a subprocess, e.g. by `aimeat connect serve`:

    AIMEAT_TASK_PROMPT="Make a Q3 marketing plan ..." \
    AIMEAT_TASK_ID="abc123" AIMEAT_AGENT_NAME="marketing-crew" AIMEAT_TOKEN="..." \
    uv run python -m crewfive.runner

Reads the task from env, runs the crew, prints a Deliverable JSON (stdout and/or
CREW_OUTPUT_FILE). For a large/verbose crew, prefer file capture
(output_capture: file:<path>) so stdout stays clean.
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
        f"[crewfive.runner] Starting task={env.task_id} agent={env.agent_name}\n"
        f"[crewfive.runner] Directive: {env.prompt}",
        file=sys.stderr,
    )

    # Best-effort: mark the start in AIMEAT memory (if the aimeat CLI is available).
    if env.task_id:
        write_memory_note(
            key=f"crews/company/tasks/{env.task_id}/started",
            value={"prompt": env.prompt, "status": "started"},
            tags=["crewfive", "task-runner"],
        )

    try:
        # Import only after load_dotenv() so the LLM picks up keys from the env.
        from crewfive.crew import CrewFive
        from crewfive.main import _save_outputs

        crew = CrewFive(verbose=False).crew()
        result = crew.kickoff(inputs={"request": env.prompt})

        deliverable = coerce_deliverable(result, env.prompt)

        # Local archive (md + json) – reuse the existing helper.
        try:
            _save_outputs(env.prompt, result)
        except Exception as exc:  # noqa: BLE001 – archiving must not crash the run
            print(f"[crewfive.runner] Archiving failed: {exc}", file=sys.stderr)

        # Best-effort: save the result summary to AIMEAT memory.
        if env.task_id:
            write_memory_note(
                key=f"crews/company/tasks/{env.task_id}/result",
                value={"title": deliverable.title, "summary": deliverable.summary},
                tags=["crewfive", "task-runner", "result"],
            )

        emit_deliverable(deliverable)

    except Exception as exc:  # noqa: BLE001 – report failure with a non-zero exit
        print(f"[crewfive.runner] ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    run()

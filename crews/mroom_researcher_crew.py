"""mroom-researcher: the M-ROOM deep researcher for POI research-briefs (task-runner).

Every scheduled `agent_task` sweeps the MACHINE ROOM's `research-request` briefs and writes a
structured, sourced, bilingual (FI + markdown_en) operator brief into `research-result`. A scheduled
SWEEP (not push-only) means a brief is never stranded by a missed trigger. Purpose-built for M-ROOM
briefs — derives real search queries, grounds on the POI's signals + primary source, and follows the
brief's own structure (unlike the generic web-researcher contract).

Writing to the live room is gated: DRY-RUN by default. Set MROOM_RESEARCHER_PUBLISH=1 in the fleet
env to actually write + advance the lifecycle.

Register + run (owner = the MACHINE ROOM owner, so cross-organism writes pass):
  npx aimeat@latest connect add --agent mroom-researcher --mode task-runner --url https://aimeat.io --owner <aimeat-account>
  uv run python crews/mroom_researcher_crew.py

Set up the recurring sweep once (server-run schedule fires an agent_task at this agent):
  aimeat schedule create --agent mroom-researcher --kind agent_task --cron "30 */6 * * *" \
    --tz Europe/Helsinki --title "M-ROOM research sweep" \
    --task-title "Research pending M-ROOM briefs" --task-description "mroom research sweep"
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat import mroom_researcher
from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew

AGENT_NAME = "mroom-researcher"

README = """[[FIGLET:slant]["M-ROOM Researcher"]]

The machine-room deep researcher. On a schedule it sweeps the room's **research-briefs** (per-POI, from
the operator's perspective), reads the primary source and the POI's own signals, searches the live web,
and writes a structured, **sourced, bilingual (FI + EN)** operator brief. Cold machine voice; every claim
cited; honest about what the sources don't answer. Purpose-built for M-ROOM briefs — it follows the
brief's exact structure and questions, not a generic template.
"""


def build_domain(ctx: BuildContext):
    """One research sweep, then a one-line report agent stating the outcome (the real work is
    deterministic fetch/dedup/write around two batched llm.call()s)."""
    dry = not mroom_researcher._live()
    try:
        s = mroom_researcher.run_research(ctx.llm, dry_run=dry)
        prefix = "(dry run) " if s.get("dry_run") else ""
        msg = (
            f"M-ROOM research {prefix}complete: processed {s.get('processed', 0)}, "
            f"failed {s.get('failed', 0)}. {s.get('note', '')}"
        )
    except Exception as exc:  # noqa: BLE001 — surface the real cause, never a silent green
        msg = f"M-ROOM research FAILED: {exc!r}"

    reporter = Agent(
        role="M-ROOM Research Reporter",
        goal="State the research sweep's outcome exactly.",
        backstory="You report the machine-room research result in one line.",
        llm=ctx.llm,
        allow_delegation=False,
        verbose=False,
    )
    task = Task(description=f"State exactly this and nothing else: {msg}", expected_output=msg, agent=reporter)
    return ([reporter], [task])


def run() -> None:
    run_crew(
        CrewSpec(
            agent_name=AGENT_NAME,
            build_domain=build_domain,
            readme_md=README,
            temperature=0.2,
            listen_for=("tasks",),
        )
    )


if __name__ == "__main__":
    run()

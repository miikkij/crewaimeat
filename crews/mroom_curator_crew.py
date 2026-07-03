"""mroom-curator: the M-ROOM research curator (agentic loop, task-runner).

Every scheduled `agent_task` (≈4 h) fires one curation pass: read the public `ext:mroom` feed hits,
judge them against the operator's criteria (batch-first — one judgement over the whole list, at most
1-2 extra web lookups for ambiguous items), and write verdicts as `signal` records into the live
MACHINE ROOM. The strongest accept becomes an insight + proposal DRAFT the operator decides on.

Writing to the live room is gated: DRY-RUN by default (fetch + judge + build records, ZERO room
writes). Set MROOM_CURATOR_PUBLISH=1 in the fleet env to actually write + publish.

Register + run (owner = the MACHINE ROOM owner, so cross-organism writes pass):
  npx aimeat@latest connect add --agent mroom-curator --mode task-runner --url https://aimeat.io --owner <aimeat-account>
  uv run python crews/mroom_curator_crew.py

Set up the recurring pass once (server-run schedule, fires an agent_task at this agent):
  aimeat schedule create --agent mroom-curator --kind agent_task --cron "0 */4 * * *" \
    --tz Europe/Helsinki --title "M-ROOM curation pass" \
    --task-title "Curate the M-ROOM feeds" --task-description "mroom curation pass"
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat import mroom
from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew

AGENT_NAME = "mroom-curator"

README = """[[FIGLET:slant]["M-ROOM Curator"]]

The machine-room research curator. On a schedule it reads the raw HN / arXiv / MCP-release / EU-AI-Act
feed hits, opens each one, and judges it against the operator's criteria — **popularity + AIMEAT
relevance together** is the strongest signal. Verdicts land as `signal` records in the MACHINE ROOM
(ACCEPTED / REJECTED + reason + follow-up idea + relation). The strongest accepts become an insight +
proposal **draft** — the machine proposes, the operator decides. Cold machine voice; it never sneers.
"""


def build_domain(ctx: BuildContext):
    """One curation pass, then a one-line report agent stating the outcome (the real work is done in
    code — deterministic fetch/dedup/write around a batched judgement)."""
    dry = not mroom._live()
    try:
        s = mroom.run_curation(ctx.llm, dry_run=dry)
        drafted = s.get("drafted") or 0
        drafted_txt = f", drafted {drafted}" if drafted else ""
        prefix = "(dry run) " if s.get("dry_run") else ""
        msg = (
            f"M-ROOM curation {prefix}complete: "
            f"scanned {s.get('scanned', 0)}, accepted {s.get('accepted', 0)}, "
            f"rejected {s.get('rejected', 0)}{drafted_txt}. {s.get('note', '')}"
        )
    except Exception as exc:  # noqa: BLE001 — surface the real cause, never a silent green
        msg = f"M-ROOM curation FAILED: {exc!r}"

    reporter = Agent(
        role="M-ROOM Curation Reporter",
        goal="State the curation run's outcome exactly.",
        backstory="You report the machine-room curation result in one line.",
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

"""mroom-researcher: the M-ROOM deep researcher for POI research-briefs (records-mode + catch-up).

Turns a `research-request` (per-POI, operator-perspective brief) in the MACHINE ROOM into a structured,
sourced, bilingual (FI + markdown_en) operator brief in `research-result`. Purpose-built for M-ROOM
briefs — derives real search queries, grounds on the POI's signals + primary source, and follows the
brief's own structure (unlike the generic web-researcher contract).

Reliable, human-nudge-free triggering — THREE layers, all behind ONE deterministic sweep (NO LLM unless
there is a real pending brief):
  1. PUSH — records-mode wakes the instant a research-request is written. mroom-researcher holds an ACTIVE
     research engagement, so the daemon PROCESSES the push (a retired engagement, e.g. web-researcher's, is
     skipped — which was exactly why briefs stopped auto-firing).
  2. STARTUP — the records reconnect fires a `catchup` event on every daemon start -> re-sweep.
  3. PERIODIC — idle_hook re-sweeps every 15 min for a stranded `requested` / stale `in-progress` claim (a
     run a restart killed mid-way), per the AIMEAT dev's 'a lost run must never require a human nudge'.

Writing to the live room is gated: DRY-RUN by default. Set MROOM_RESEARCHER_PUBLISH=1 in the fleet env to
actually write + advance the lifecycle.

Register + run (owner = the MACHINE ROOM owner, so cross-organism writes pass):
  npx aimeat@latest connect add --agent mroom-researcher --mode task-runner --url https://aimeat.io --owner <aimeat-account>
  uv run python crews/mroom_researcher_crew.py
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat import mroom_researcher
from crewaimeat.aimeat_crew import BuildContext, CrewSpec, record_event_targets, run_crew
from crewaimeat.mroom import ROOM_ORG, ROOM_WS

AGENT_NAME = "mroom-researcher"
RESEARCH_REQUEST_NS = "shared.research_requests"  # the room's research-request publish namespace (mroom.NS)

README = """[[FIGLET:slant]["M-ROOM Researcher"]]

The machine-room deep researcher. The instant a **research-brief** is requested (per-POI, from the
operator's perspective) it wakes, reads the primary source and the POI's own signals, searches the live web,
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
    # ONE deterministic catch-up sweep (NO LLM unless there is a real pending/stranded brief) behind all
    # three triggers below — it fulfils whatever is waiting and reports only when it actually did something.
    def _sweep() -> None:
        res = mroom_researcher.run_research(dry_run=not mroom_researcher._live())
        if res.get("processed") or res.get("failed"):
            print(f"[{AGENT_NAME}] research sweep: {res}")

    def _on_record(event) -> None:
        record_event_targets(event)  # a research-request changed (or the catch-up on connect) -> sweep
        _sweep()

    run_crew(
        CrewSpec(
            agent_name=AGENT_NAME,
            build_domain=build_domain,
            readme_md=README,
            temperature=0.2,
            # PUSH — wake the instant a research-request is written. mroom-researcher holds an ACTIVE research
            # engagement in the room, so the daemon gate PROCESSES (not skips) the push (unlike a retired one).
            listen_for=("tasks", "records"),
            record_spaces=[{"organism_id": ROOM_ORG, "ws": ROOM_WS, "space": RESEARCH_REQUEST_NS}],
            on_record=_on_record,
            # CATCH-UP (AIMEAT dev directive — a lost run must never wait for a human nudge): the records
            # reconnect fires a `catchup` event on daemon start, and idle_hook re-sweeps periodically for a
            # stranded `requested` / stale `in-progress` claim. Both call _sweep -> NO LLM unless there's work.
            idle_hook=_sweep,
            idle_hook_seconds=900,  # every 15 min
        )
    )


if __name__ == "__main__":
    run()

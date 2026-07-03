"""mroom-digger: the M-ROOM REQuest fleet's research worker (records-mode, task-runner).

Second stage of the guest-REQuest chain, and the fleet's OWN researcher — the existing `mroom-researcher`
stays the POI research-brief agent and is left untouched. A `request` at status `processing` wakes it; it
executes the sniffer's plan with web search (SearXNG + main-text extraction), composes bilingual findings
grounded in the sources, appends them to the `outbox` doc, and sets the request to `researched` for the
scorer. Cold machine voice; every claim is cited; the guest is only ever `EXC_VIP_NN`.

DRY-RUN by default; set MROOM_REQUESTS_PUBLISH=1 in the fleet env to actually write + advance the chain.

Register + run:
  npx aimeat@latest connect add --agent mroom-digger --mode task-runner --url https://aimeat.io --owner <you>
  uv run python crews/mroom_digger_crew.py
"""

from __future__ import annotations

from crewaimeat import mroom_requests as mr
from crewaimeat.aimeat_crew import BuildContext, CrewSpec, record_event_targets, run_crew
from crewaimeat.mroom import ROOM_ORG, ROOM_WS

AGENT_NAME = mr.DIGGER

README = """[[FIGLET:slant]["M-ROOM Digger"]]

The machine room's **research** worker for guest REQuests (distinct from `mroom-researcher`, which
handles POI research-briefs). It takes the sniffer's plan, searches the open web, reads the sources
and appends **sourced, bilingual findings** to the request's `outbox` document, then hands off to the
scorer (status `researched`). Every claim is cited; it states, never sells; the guest is only ever
`EXC_VIP_NN`.
"""


def build_domain(ctx: BuildContext):
    try:
        s = mr.run_research(ctx.llm, dry_run=not mr._live())
        msg = mr.stage_report_msg("digger", s)
    except Exception as exc:  # noqa: BLE001 — surface the real cause, never a silent green
        msg = f"M-ROOM digger FAILED: {exc!r}"
    return mr.report_crew(ctx.llm, msg)


def run() -> None:
    def _on_record(event) -> None:
        record_event_targets(event)
        res = mr.run_research(dry_run=not mr._live())
        if res.get("processed") or res.get("failed"):
            print(f"[{AGENT_NAME}] request event: {res}")

    run_crew(
        CrewSpec(
            agent_name=AGENT_NAME,
            build_domain=build_domain,
            readme_md=README,
            temperature=0.3,
            listen_for=("tasks", "records"),
            record_spaces=[{"organism_id": ROOM_ORG, "ws": ROOM_WS, "space": "room.request"}],
            on_record=_on_record,
        )
    )


if __name__ == "__main__":
    run()

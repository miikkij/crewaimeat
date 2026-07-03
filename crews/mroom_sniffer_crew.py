"""mroom-sniffer: the M-ROOM REQuest fleet's intake worker (records-mode, task-runner).

First stage of the guest-REQuest chain. A pushed `request` record (status `sniffing`) — or the catch-up
on connect — wakes it; it classifies the ask, drafts a processing plan into an `outbox` doc, and sets the
request to `processing` for mroom-digger. Cold machine voice; the guest is only ever `EXC_VIP_NN`.

DRY-RUN by default; set MROOM_REQUESTS_PUBLISH=1 in the fleet env to actually write + advance the chain.

Register + run (owner = the MACHINE ROOM owner so same-owner writes pass):
  npx aimeat@latest connect add --agent mroom-sniffer --mode task-runner --url https://aimeat.io --owner <you>
  uv run python crews/mroom_sniffer_crew.py
"""

from __future__ import annotations

from crewaimeat import mroom_requests as mr
from crewaimeat.aimeat_crew import BuildContext, CrewSpec, record_event_targets, run_crew
from crewaimeat.mroom import ROOM_ORG, ROOM_WS

AGENT_NAME = mr.SNIFFER

README = """[[FIGLET:slant]["M-ROOM Sniffer"]]

The machine room's **intake** worker. When a guest leaves a REQuest, this picks it up (status
`sniffing`), classifies the ask, drafts a short processing plan into a visible `outbox` document and
hands it to the researcher (status `processing`). Cold machine voice; the guest is only ever
`EXC_VIP_NN` — never a name or an address.
"""


def build_domain(ctx: BuildContext):
    try:
        s = mr.run_sniff(ctx.llm, dry_run=not mr._live())
        msg = mr.stage_report_msg("sniffer", s)
    except Exception as exc:  # noqa: BLE001 — surface the real cause, never a silent green
        msg = f"M-ROOM sniffer FAILED: {exc!r}"
    return mr.report_crew(ctx.llm, msg)


def run() -> None:
    def _on_record(event) -> None:
        record_event_targets(event)  # the room is fixed; the wake just means "a request changed"
        res = mr.run_sniff(dry_run=not mr._live())
        if res.get("processed") or res.get("failed"):
            print(f"[{AGENT_NAME}] request event: {res}")

    run_crew(
        CrewSpec(
            agent_name=AGENT_NAME,
            build_domain=build_domain,
            readme_md=README,
            temperature=0.2,
            listen_for=("tasks", "records"),
            record_spaces=[{"organism_id": ROOM_ORG, "ws": ROOM_WS, "space": "room.request"}],
            on_record=_on_record,
        )
    )


if __name__ == "__main__":
    run()

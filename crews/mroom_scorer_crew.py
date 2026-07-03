"""mroom-scorer: the M-ROOM REQuest fleet's cold evaluator (records-mode, task-runner).

Third stage of the guest-REQuest chain. A `request` at status `researched` wakes it; it reads the
outbox's research trail and states a cold `SIGNAL VALUE: X.X — RETAINED|DISCARDED` plus one factual
line, appends that scorecard to the `outbox`, and sets the request to `scored` (with verdict +
signal_value) for the archivist. It judges the CONTENT, NEVER the person — a discard is "the request
produced no signal", never an insult. The guest is only ever `EXC_VIP_NN`.

DRY-RUN by default; set MROOM_REQUESTS_PUBLISH=1 in the fleet env to actually write + advance the chain.

Register + run:
  npx aimeat@latest connect add --agent mroom-scorer --mode task-runner --url https://aimeat.io --owner <you>
  uv run python crews/mroom_scorer_crew.py
"""

from __future__ import annotations

from crewaimeat import mroom_requests as mr
from crewaimeat.aimeat_crew import BuildContext, CrewSpec, record_event_targets, run_crew
from crewaimeat.mroom import ROOM_ORG, ROOM_WS

AGENT_NAME = mr.SCORER

README = """[[FIGLET:slant]["M-ROOM Scorer"]]

The machine room's **cold evaluator**. It reads what the research produced and states a single
`SIGNAL VALUE: X.X — RETAINED|DISCARDED` with one factual line, then hands off to the archivist
(status `scored`). It judges the **content, never the person**: a discard is "the request produced no
signal", never an insult. The guest is only ever `EXC_VIP_NN`.
"""


def build_domain(ctx: BuildContext):
    try:
        s = mr.run_score(ctx.llm, dry_run=not mr._live())
        msg = mr.stage_report_msg("scorer", s)
    except Exception as exc:  # noqa: BLE001 — surface the real cause, never a silent green
        msg = f"M-ROOM scorer FAILED: {exc!r}"
    return mr.report_crew(ctx.llm, msg)


def run() -> None:
    def _on_record(event) -> None:
        record_event_targets(event)
        res = mr.run_score(dry_run=not mr._live())
        if res.get("processed") or res.get("failed"):
            print(f"[{AGENT_NAME}] request event: {res}")

    run_crew(
        CrewSpec(
            agent_name=AGENT_NAME,
            build_domain=build_domain,
            readme_md=README,
            temperature=0.1,
            listen_for=("tasks", "records"),
            record_spaces=[{"organism_id": ROOM_ORG, "ws": ROOM_WS, "space": "room.request"}],
            on_record=_on_record,
        )
    )


if __name__ == "__main__":
    run()

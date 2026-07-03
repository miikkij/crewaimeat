"""mroom-archivist: the M-ROOM REQuest fleet's archivist (records-mode, task-runner).

Final stage of the guest-REQuest chain. A `request` at status `scored` wakes it. RETAINED -> it writes
and PUBLISHES a bilingual `archive-entry` (the permanent trail: a PATH / DECISION / STATUS / PARTIES
header, the story, the scorecard, follow-ups, and the sources). DISCARDED -> a light deterministic
archive note. Either way it sets the request to `archived`. Parties are named ONLY as `EXC_VIP_NN` +
the agent names — never a real identity.

DRY-RUN by default; set MROOM_REQUESTS_PUBLISH=1 in the fleet env to actually write + publish.

Register + run:
  npx aimeat@latest connect add --agent mroom-archivist --mode task-runner --url https://aimeat.io --owner <you>
  uv run python crews/mroom_archivist_crew.py
"""

from __future__ import annotations

from crewaimeat import mroom_requests as mr
from crewaimeat.aimeat_crew import BuildContext, CrewSpec, record_event_targets, run_crew
from crewaimeat.mroom import ROOM_ORG, ROOM_WS

AGENT_NAME = mr.ARCHIVIST

README = """[[FIGLET:slant]["M-ROOM Archivist"]]

The machine room's **archivist**. A RETAINED request becomes a published, bilingual `archive-entry` —
the permanent trail: PATH / DECISION / STATUS / PARTIES, the story, the scorecard, follow-ups and the
sources. A DISCARDED request gets a light note (a discard is still archived, factually). Parties are
named only as `EXC_VIP_NN` + the agent names — never a real identity.
"""


def build_domain(ctx: BuildContext):
    try:
        s = mr.run_archive(ctx.llm, dry_run=not mr._live())
        msg = mr.stage_report_msg("archivist", s)
    except Exception as exc:  # noqa: BLE001 — surface the real cause, never a silent green
        msg = f"M-ROOM archivist FAILED: {exc!r}"
    return mr.report_crew(ctx.llm, msg)


def run() -> None:
    def _on_record(event) -> None:
        record_event_targets(event)
        res = mr.run_archive(dry_run=not mr._live())
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

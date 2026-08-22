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

# ── This agent's own declaration ─────────────────────────────────────────────────────────────
# The single source for what this agent is: its model routing, how it is discovered, and what it
# promises. These used to live in three central lists (fleet_identity.py / llm_providers.json /
# offers.py) that nothing kept in step, so an agent could — and did — come online missing from
# all of them. crewaimeat.agent_manifest reads these statically; the lists are derived.
LLM_PROFILE = "content-free"
TAGS = ["mroom", "request-fleet", "scoring", "role.task-runner"]
CAPABILITIES = {
    "technical": [{"name": "mroom-scorer", "type": "skill"}],
    "domain": [
        "M-ROOM cold evaluation: SIGNAL VALUE X.X + RETAINED/DISCARDED + one factual line",
        "judges the CONTENT never the person; a discard states 'no signal', never an insult",
        "hands off to the archivist (status researched -> scored)",
    ],
    "languages": ["en", "fi"],
}
OFFERS = [
    {
        "id": "score-guest-request",
        "title": "Cold-score what a guest REQuest produced",
        "ask": "I read the research trail and state one SIGNAL VALUE: X.X — RETAINED or DISCARDED plus one factual "
        "line, then hand off to the archivist. I judge the CONTENT, never the person — a discard is 'the "
        "request produced no signal', never an insult. Runs automatically per request — I score and hand off; "
        "I don't decide or act on the result.",
        "example": "Score the MCP-vs-AIMEAT messaging research",
        "cost": "cheap",
        "latency": "seconds",
        "repeatability": "idempotent",
        "verification": "gated",
        "consequences": [{"type": "publishes-public", "note": "the scorecard is visible MACHINE ROOM outbox content"}],
        "sample": "## Scorecard\nSIGNAL VALUE: 6.5 — RETAINED\nConfirms a real gap MCP leaves that AIMEAT fills.\n\n…",
    }
]


# Declared opt-out from the ctx.prompt-injection floor (tests/test_build_domain.py).
PROMPT_INDEPENDENT = (
    "record-driven: the scoring pass reads the researched request from the room; ctx.prompt carries no target."
)

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

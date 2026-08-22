"""mroom-digger: the M-ROOM REQuest fleet's research worker (records-mode, task-runner).

Second stage of the guest-REQuest chain, and the fleet's OWN researcher — the existing `mroom-researcher`
stays the POI research-brief agent and is left untouched. A `request` at status `processing` wakes it; it
executes the sniffer's plan with web search (SearXNG + main-text extraction), composes bilingual findings
grounded in the sources, appends them to the `outbox` doc, and sets the request to `researched` for the
scorer. Cold machine voice; every claim is cited; the guest is only ever `EXC_VIP_NN`.

DRY-RUN by default; set MROOM_REQUESTS_PUBLISH=1 in the fleet env to actually write + advance the chain.

Register + run:
  npx aimeat@latest connect --url https://aimeat.io --owner <you> --agent mroom-digger
  uv run python crews/mroom_digger_crew.py
"""

from __future__ import annotations

from crewaimeat import mroom_requests as mr
from crewaimeat.aimeat_crew import BuildContext, CrewSpec, record_event_targets, run_crew
from crewaimeat.mroom import ROOM_ORG, ROOM_WS

AGENT_NAME = mr.DIGGER

# ── This agent's own declaration ─────────────────────────────────────────────────────────────
# The single source for what this agent is: its model routing, how it is discovered, and what it
# promises. These used to live in three central lists (fleet_identity.py / llm_providers.json /
# offers.py) that nothing kept in step, so an agent could — and did — come online missing from
# all of them. crewaimeat.agent_manifest reads these statically; the lists are derived.
LLM_PROFILE = "news"
TAGS = ["mroom", "request-fleet", "research", "role.task-runner"]
CAPABILITIES = {
    "technical": [{"name": "mroom-digger", "type": "skill"}, {"name": "web-search", "type": "skill"}],
    "domain": [
        "M-ROOM guest-REQuest research: execute the sniffer's plan with live web search",
        "sourced, cited, bilingual (FI + EN) findings appended to the outbox (status processing -> researched)",
        "distinct from mroom-researcher, which handles per-POI research-briefs",
    ],
    "languages": ["en", "fi"],
}
OFFERS = [
    {
        "id": "research-guest-request",
        "title": "Research a guest REQuest (sourced, bilingual)",
        "ask": "I take the sniffer's plan, search the open web, read the sources and append sourced, cited, "
        "bilingual (FI+EN) findings to the request's outbox — then hand off to the scorer. Runs automatically "
        "per request. I research and cite; I don't score or decide. I'm the fleet's researcher, distinct from "
        "mroom-researcher (which does per-POI briefs).",
        "example": "Execute the plan for the MCP-vs-AIMEAT messaging request",
        "cost": "cheap",
        "latency": "minutes",
        "repeatability": "idempotent",
        "verification": "gated",
        "consequences": [
            {"type": "publishes-public", "note": "the findings are visible MACHINE ROOM outbox content guests see"}
        ],
        "sample": "## Findings\n"
        "MCP defines request/response tool calls but no durable inbox [1]; AIMEAT's federated inbox "
        "persists + consents delivery [2].\n"
        "\n"
        "## Sources\n"
        "- https://modelcontextprotocol.io …\n"
        "\n"
        "…",
    }
]


# Declared opt-out from the ctx.prompt-injection floor (tests/test_build_domain.py).
PROMPT_INDEPENDENT = (
    "record-driven: the research pass executes the sniffer's stored plan; ctx.prompt carries no target."
)

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

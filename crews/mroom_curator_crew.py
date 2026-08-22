"""mroom-curator: the M-ROOM research curator (agentic loop, task-runner).

Every scheduled `agent_task` (≈4 h) fires one curation pass: read the public `ext:mroom` feed hits,
judge them against the operator's criteria (batch-first — one judgement over the whole list, at most
1-2 extra web lookups for ambiguous items), and write verdicts as `signal` records into the live
MACHINE ROOM. The strongest accept becomes an insight + proposal DRAFT the operator decides on.

Writing to the live room is gated: DRY-RUN by default (fetch + judge + build records, ZERO room
writes). Set MROOM_CURATOR_PUBLISH=1 in the fleet env to actually write + publish.

Register + run (owner = the MACHINE ROOM owner, so cross-organism writes pass):
  npx aimeat@latest connect --url https://aimeat.io --owner <aimeat-account> --agent mroom-curator
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

# ── This agent's own declaration ─────────────────────────────────────────────────────────────
# The single source for what this agent is: its model routing, how it is discovered, and what it
# promises. These used to live in three central lists (fleet_identity.py / llm_providers.json /
# offers.py) that nothing kept in step, so an agent could — and did — come online missing from
# all of them. crewaimeat.agent_manifest reads these statically; the lists are derived.
LLM_PROFILE = "content-free"
TAGS = ["research-radar", "mroom", "curation", "role.task-runner"]
CAPABILITIES = {
    "technical": [{"name": "mroom-curator", "type": "skill"}],
    "domain": [
        "M-ROOM research curation: judge raw feed hits into ACCEPTED/REJECTED signal verdicts",
        "AIMEAT-relevance + popularity signal scoring (competitor-compare / adopt / foundation-shift / regulation)",
        "insight + proposal drafting (drafts only — the operator decides)",
    ],
    "languages": ["en", "fi"],
}
OFFERS = [
    {
        "id": "curate-mroom",
        "title": "Curate the M-ROOM research feeds",
        "ask": "I read the raw HN / arXiv / MCP-release / EU-AI-Act feed hits, judge each against the operator's "
        "criteria, and write ACCEPTED/REJECTED signal verdicts into the MACHINE ROOM. Runs on a schedule — "
        "ask only for an extra pass. I judge and record; I don't decide — the strongest accepts become drafts "
        "the operator approves.",
        "example": "Run an M-ROOM curation pass now",
        "cost": "cheap",
        "latency": "minutes",
        "repeatability": "idempotent",
        "verification": "gated",
        "scheduleBorn": "every 4h — runs automatically",
        "consequences": [
            {
                "type": "publishes-public",
                "note": "signal verdicts are public MACHINE ROOM content guests see; insight/proposal stay drafts",
            }
        ],
        "sample": "## M-ROOM curation — 15 scanned\n"
        "\n"
        "- **ACCEPTED** sig-scopewalker (POI_006 · community-pulse) — MCP ecosystem tooling with "
        "traction.\n"
        "- **REJECTED** sig-agentrc — packaging/ops tooling; no protocol or identity angle.\n"
        "\n"
        "1 strong accept drafted as insight + proposal (operator decides).\n"
        "\n"
        "…",
    }
]


# Declared opt-out from the ctx.prompt-injection floor (tests/test_build_domain.py).
PROMPT_INDEPENDENT = (
    "record-driven: the curation pass runs deterministically in code over the raw feed; ctx.prompt carries no target."
)

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

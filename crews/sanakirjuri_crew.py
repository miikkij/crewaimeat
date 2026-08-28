"""sanakirjuri — a JSON agent. Its crew lives ON THE NODE, not in this repo.

There is no definition here and there is not meant to be one: this file only names the agent. The
crew is at `crews.registry.sanakirjuri` and you edit it in AIMEAT under
profile > agents > sanakirjuri > Crew. Publish, and the NEXT TASK uses it — no restart, because the
definition is re-read every time a task is built.

`crewaimeat try <def.json> --prompt "…"` runs a definition locally before you publish it.

Run standalone: uv run python crews/sanakirjuri_crew.py
"""

from __future__ import annotations

from crewaimeat.json_agent import Definition, load_def, run_json_agent

AGENT_NAME = "sanakirjuri"

# Read by `crewaimeat doctor` (statically, via ast). It says: the declarations doctor looks for —
# TAGS, CAPABILITIES, OFFERS, LLM_PROFILE — are deliberately absent HERE because they are on the
# node. Without it doctor would report this healthy agent as having no identity, no offer and no
# routing decision, and go red in pre-commit for every JSON agent ever added.
CREW_DEF_SOURCE = "node"

_live: Definition | None = None


def build_domain(ctx):
    """Interpret the definition the node currently holds for this agent.

    `run()` is the real path; this exists so the fleet validator and any direct caller behave exactly
    as they do for a Python crew. The `Definition` is kept so repeated builds reuse the last good one
    instead of going dark when a read fails.
    """
    global _live
    if _live is None:
        doc, revision = load_def(AGENT_NAME)
        _live = Definition(AGENT_NAME, doc, revision)
    return _live.build(ctx)


def run() -> None:
    run_json_agent(AGENT_NAME)


if __name__ == "__main__":
    run()

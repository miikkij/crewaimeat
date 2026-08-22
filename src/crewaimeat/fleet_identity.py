"""Per-agent capability identity — RESOLVED from the crew file, with the authoring helpers.

`identity_for(agent)` answers "what does this agent advertise": its tags and its capabilities. On
every start `run_crew` sets the tags (aimeat_agent_tags_set) and reports the capabilities
(aimeat_agent_capabilities_report, which OVERWRITES the set), so the ecosystem picker's matcher sees
what the agent ACTUALLY does instead of the liaison's generic Hello-Integration defaults.

WHERE THE ANSWER COMES FROM (changed 2026-08-22): the crew file itself. A crew declares `TAGS` and
`CAPABILITIES` at module level — a JSON crew declares `tags`/`capabilities` in its doc — and
crewaimeat.agent_manifest reads them statically. This module used to BE the registry: 45 hand-kept
entries that nothing required you to add to, so 13 live agents had no identity at all and one had a
malformed one that the node accepted and could not match on. Data that must exist per agent belongs
with the agent.

Conventions (unchanged, and enforced by `_validate_capabilities` at the reporting boundary): tags
charset is [a-z0-9._-] only (NO ':' or '@'); `technical` entries are {name, type} objects — use the
`_skill` / `_tool` helpers below; `domain` is a list of free strings and may carry ':'/'@'
(e.g. "consumes:ledger-request"). Background: docs/internal/agent-tags-capabilities-proposal.md.
"""

from __future__ import annotations


def _skill(name: str) -> dict:
    return {"name": name, "type": "skill"}


def _tool(name: str) -> dict:
    """A concrete tool the agent can call. `technical` entries are OBJECTS ({name, type}) — the node's
    matcher reads the pair, and a bare string is not a capability it can match on. Descriptive phrases
    ("pandas", "frictionless table schema") belong in `domain`, which IS a list of strings."""
    return {"name": name, "type": "tool"}


# EMPTY ON PURPOSE. Every agent's tags + capabilities now live in its own crew file (`TAGS` /
# `CAPABILITIES`, or `tags` / `capabilities` in a JSON crew doc) and `identity_for` reads them from
# there via crewaimeat.agent_manifest. The 45 entries that used to sit here were moved on 2026-08-22;
# the last 7 belonged to agents that had already been retired and went with them.
#
# This dict stays as the FALLBACK for an agent with no crew file reachable from the current working
# directory — crewaimeat installed as a library, or a test fixture. Adding an entry here is not wrong,
# but it is the old shape: a central list nothing forces you to keep in step, which is how this one
# ended up 13 agents short while every one of them was live.
FLEET_IDENTITY: dict[str, dict] = {}


def identity_for(agent: str) -> dict:
    """The {tags?, capabilities?} for an agent, or {} when it declares none.

    THE CREW FILE IS THE SOURCE. `FLEET_IDENTITY` below is now only a fallback for an agent with no
    crew file reachable from here (crewaimeat installed as a library, a test fixture), and for
    anything not yet migrated. Reading the crew first is the whole point of the change: a central
    dict that nothing forces you to update is a dict that ends up 13 agents short, which is exactly
    where this one was on 2026-08-22.
    """
    try:
        from crewaimeat.agent_manifest import manifest_for

        m = manifest_for(agent)
    except Exception:  # noqa: BLE001 — identity is advertising, never a reason to fail a start
        m = None
    if m is not None and (m.tags is not None or m.capabilities is not None):
        out = {}
        if m.tags is not None:
            out["tags"] = m.tags
        if m.capabilities is not None:
            out["capabilities"] = m.capabilities
        return out
    return FLEET_IDENTITY.get(agent, {})

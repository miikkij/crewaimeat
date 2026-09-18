"""What the agents spent — read from the node's ledger, never estimated here.

Every model call a runtime makes is metered to the node (`/v1/ledger/usage`, aimeat-crewai's usage
telemetry), and since 3a9b998 the single-model path asks OpenRouter for the real per-call cost. The
ledger is owner-scoped: any of the owner's agents with `wallet:read` sees the whole owner's usage, so
one call per instance gives every agent's row. A call the provider did not price is COUNTED as
unpriced, not shown as free.
"""

from __future__ import annotations

from crewaimeat.agency2 import node

DAYS = 30


def _totals(g: dict) -> dict:
    return {
        "cost_usd": round(float(g.get("cost_usd") or 0), 4),
        "calls": int(g.get("calls") or 0),
        "tokens": int(g.get("total_tokens") or 0),
        "unpriced_calls": int(g.get("unpriced_calls") or 0),
    }


def by_agent(asker: str) -> dict[str, dict]:
    """agent NAME -> totals for the last DAYS days, for every agent of the asker's owner on its node."""
    data = node.rest_get(asker, "/v1/ledger/usage", {"group_by": "agent"})
    out: dict[str, dict] = {}
    for g in (data or {}).get("groups") or []:
        name = str(g.get("key") or "").split("#")[0]
        if name:
            out[name] = _totals(g)
    return out


def for_agent(agent: str) -> dict:
    """{total, models: [{model, ...totals}]} for one agent over the last DAYS days."""
    gaii = node.served_agents().get(agent, {}).get("gaii") or agent
    data = node.rest_get(agent, "/v1/ledger/usage", {"group_by": "model", "agent": gaii})
    models = [{"model": g.get("key"), **_totals(g)} for g in (data or {}).get("groups") or []]
    return {"days": DAYS, "total": _totals((data or {}).get("totals") or {}), "models": models}

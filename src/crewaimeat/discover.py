"""Master directory (`aimeat_discover`) — deterministic helpers for no-LLM crews.

`aimeat_discover` is the node's ONE faceted query across every domain (capabilities, workflows, knowledge,
decisions, research, produced material, companies + offerings, live documents, apps, memory). Since node
**1.32.1** it is also SHELL-callable, so deterministic / no-LLM crews can call it straight over the loopback
serve daemon (`_aimeat_call`) — "map first" to see WHAT already exists before doing work, then "find" to pull
ranked entries. (LLM crews / the liaison reach the same tool through the MCP adapter — see CrewSpec.discover;
this module is the deterministic counterpart.)

Two modes:
  - map  -> a cheap catalog-of-catalogs: {total, types:[{value,count}], segments, ...} — see what exists.
  - find -> ranked entries: [{type, id, title, description, tags, score, href, ...}].
Scope: own (your owner's reachable content, default) | public (node-wide) | shared (organisms you belong to).
"""

from __future__ import annotations

from crewaimeat.aimeat_crew import _aimeat_call


def _payload(mode: str, *, q: str, type: str, tags: str, segment: str, scope: str, limit: int | None) -> dict:
    p: dict = {"mode": mode, "scope": scope}
    if q:
        p["q"] = q
    if type:
        p["type"] = type
    if tags:
        p["tags"] = tags
    if segment:
        p["segment"] = segment
    if limit is not None:
        p["limit"] = limit
    return p


def discover_map(agent: str, *, scope: str = "own", type: str = "", tags: str = "", segment: str = "") -> dict:
    """Cheap catalog: counts by type/segment/tag, no content. Returns {total, types, segments, ...} (or {}).
    Call this FIRST to see what exists before pulling entries. `type`/`tags`/`segment` are CSV filters."""
    return (
        _aimeat_call(
            agent,
            "aimeat_discover",
            _payload("map", q="", type=type, tags=tags, segment=segment, scope=scope, limit=None),
        )
        or {}
    )


def discover_find(
    agent: str,
    q: str = "",
    *,
    type: str = "",
    tags: str = "",
    segment: str = "",
    scope: str = "own",
    limit: int = 20,
) -> list[dict]:
    """Ranked, faceted entries -> [{type, id, title, description, tags, score, href, ...}] (or []). Filter
    with `q` (free text), `type` (CSV of types), `tags` (CSV — an entry must carry ALL), `segment` (CSV).
    Prefer this over the per-domain searches when you don't yet know which domain holds what you need."""
    r = _aimeat_call(
        agent, "aimeat_discover", _payload("find", q=q, type=type, tags=tags, segment=segment, scope=scope, limit=limit)
    )
    if isinstance(r, list):
        return r
    if isinstance(r, dict):
        entries = r.get("entries")
        if isinstance(entries, list):
            return entries
    return []

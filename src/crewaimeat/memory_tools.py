"""Memory tools — let a CONTENT crew read/write the owner's memory at specific keys with a chosen
visibility. The enabling toolkit for the news pipeline: a fetcher writes raw material, a writer reads
raw + writes own-words articles, an editorial agent reads articles + writes the editorial — each to
PUBLIC dated keys so an anonymous newspaper app can read them.

Why this exists: the scaffold's default deliverable-publish writes ONE derived key at `owner` visibility
(it does not let an agent target arbitrary keys or set public visibility). Content agents need exactly
that, so they get explicit tools. Backed by `_aimeat_call` (the connector's CLI path) — verified that
aimeat_memory_write/read/list ARE CLI-callable (unlike the schedule tools, which are MCP-only).

Usage (in a crew's build_domain — crew-forge wires this for content/writer/editorial crews):
    from crewaimeat.memory_tools import make_memory_tools
    mem_tools = make_memory_tools(AGENT_NAME)
    agent = Agent(..., tools=[*mem_tools], llm=ctx.llm)
"""

from __future__ import annotations

import json

from crewaimeat.aimeat_crew import _aimeat_call
from crewai.tools import tool


def make_memory_tools(agent_name: str) -> list:
    """Return content-memory crewai tools (write_memory / read_memory / list_memory) for this agent."""

    @tool("write_memory")
    def write_memory(key: str, value: str, visibility: str = "public") -> str:
        """Write a value to the owner's memory at an EXACT key. Use this to persist your deliverable to
        the agreed key (e.g. 'news.2026-06-03.morning.article.talous'). visibility:
          'public' = anyone can read it WITHOUT logging in (use for articles + editorials a public app shows),
          'owner'  = only the owner / same-owner agents can read it (use for raw material if it shouldn't be public).
        `value` is stored as given (write your finished article/editorial text here, or a JSON string for
        structured data). Returns OK or the error. Write each category to its OWN key."""
        vis = (visibility or "public").strip().lower()
        if vis not in ("public", "owner"):
            return "FAILED: visibility must be 'public' or 'owner'."
        if not key or not str(key).strip():
            return "FAILED: key is required (the EXACT memory key to write)."
        # Accept a JSON string (store the parsed object) or plain text (store as-is).
        val: object = value
        sv = value.strip() if isinstance(value, str) else value
        if isinstance(sv, str) and sv[:1] in ("{", "["):
            try:
                val = json.loads(sv)
            except Exception:  # noqa: BLE001 — not JSON, store as plain text
                val = value
        r = _aimeat_call(agent_name, "aimeat_memory_write", {"key": key, "value": val, "visibility": vis})
        if r is None:
            return f"FAILED to write '{key}' (no result from memory_write)."
        return f"OK: wrote '{key}' (visibility={vis})."

    def _owner_scope_value(key: str):
        # Cross-agent read: memory is namespaced by the WRITING agent's GAII, so a value written by a
        # sibling (e.g. the fetcher's raw keys) is NOT under this agent's own GAII. owner_scope=true lists
        # across ALL same-owner agents (the pattern workflow.py uses to collect workers' deliverables).
        r = _aimeat_call(agent_name, "aimeat_memory_list", {"owner_scope": True, "prefix": key})
        items = (r or {}).get("items") if isinstance(r, dict) else None
        for it in (items or []):
            if isinstance(it, dict) and it.get("key") == key and it.get("value") is not None:
                return it.get("value")
        return None

    @tool("read_memory")
    def read_memory(key: str) -> str:
        """Read the value at an EXACT owner memory key — INCLUDING keys written by OTHER same-owner agents
        (e.g. a writer reading the fetcher's raw keys). Tries your own memory first, then a same-owner
        cross-agent (owner-scope) lookup. Returns the value, or a clear 'not found' if the key isn't
        written yet — in that case do NOT fabricate content; report the missing upstream key and stop
        (the upstream stage may not have run yet)."""
        if not key or not str(key).strip():
            return "FAILED: key is required."
        r = _aimeat_call(agent_name, "aimeat_memory_read", {"key": key})  # own GAII first
        val = (r.get("value") if isinstance(r, dict) else r) if r is not None else None
        if val is None:
            val = _owner_scope_value(key)  # then same-owner cross-agent (sibling-written keys)
        if val is None:
            return f"NOT FOUND: '{key}' has no value yet (upstream stage may not have run). Do not fabricate — stop."
        out = val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
        return f"value of '{key}':\n{out[:8000]}"

    @tool("list_memory")
    def list_memory(prefix: str) -> str:
        """List the owner memory keys under a prefix (e.g. 'news.2026-06-03.' to see what's been written
        for today) ACROSS all same-owner agents (so you see siblings' keys too). Returns the matching
        keys. Use to discover which categories/editions exist before reading."""
        r = _aimeat_call(agent_name, "aimeat_memory_list", {"owner_scope": True, "prefix": prefix or ""})
        items = ((r or {}).get("items") if isinstance(r, dict) else None) or []
        keys = [it.get("key") for it in items if isinstance(it, dict) and it.get("key")]
        if not keys:
            return f"No memory keys found under prefix '{prefix}'."
        return f"keys under '{prefix}':\n" + "\n".join(f"- {k}" for k in keys[:60])

    tools = [write_memory, read_memory, list_memory]
    for _t in tools:  # side-effecting / live-state — never serve a cached result
        try:
            _t.cache_function = lambda *_a, **_k: False
        except Exception:  # noqa: BLE001
            pass
    return tools

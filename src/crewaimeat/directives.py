"""Owner-set directives, on every model call.

An agent's directives live on the node in three layers (the operator's principles, the owner's
defaults for all their agents, and the agent's own Directives tab) and come back merged from
GET /v1/agents/me/directives. Until 2026-09-09 the scaffold read them once per task and prepended
them to the task text of a `build_domain` crew, and NOTHING ELSE saw them: the 27 places that call
`llm.call([...])` directly (the julkaisupöytä writers, the brief, the desk, grok, the editorial
pipeline, features, mail, legal, the librarian, corrections) built their own prompt and sent it
bare. A rule the owner wrote on the agent's Directives tab therefore did not reach the prompt that
wrote an image request, and a partner found that out by writing one.

The fix is at the one seam every call passes: `get_llm(agent_name=...)` installs a wrapper on the
returned LLM's `call` that prepends the block as a system message. The block is fetched at most
once per TTL per agent, a failed fetch keeps the last known block (stale is better than none), and a
message list that already carries the block (a crew task the scaffold prepended it to) is left alone
so nothing is said twice.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from typing import Any

# The first line of a formatted block. A message that already contains it carries the directives.
DIRECTIVES_HEADER = "STANDING DIRECTIVES (owner-set policy"

# How long a fetched block is reused before the node is asked again. An owner edit lands within this.
DIRECTIVES_TTL_S = 300.0


def fetch_directives(agent_name: str, owner: str | None = None) -> dict | None:
    """Read the agent's merged directives via GET /v1/agents/me/directives.

    `me` resolves to the calling agent from its credential. Returns the inner data payload
    {purpose, rules[], memory_areas, shared_tags, shared_memory_prefixes, resources} or None. Rules
    are merged system -> owner -> agent, each tagged with its source. This is the canonical
    directives contract (also onboarding STEP 1). Best-effort: any failure returns None so a crew
    still runs without directives.

    Through the scaffold's REST dispatcher, which already knows both doors: the loopback serve
    daemon first (an Agent v2 identity holds a signing key and only the daemon can sign for it), a
    direct authed call with the stored token when no daemon runs, and the retry policy every other
    node call has. Resolved at call time: aimeat_crew imports llm, llm imports this module, so a
    module-level import would be a cycle, and a process without the scaffold (a script that only
    wants a model) has no node to ask. `owner` is kept for the cache key; the dispatcher resolves
    the credential from the agent name.
    """
    del owner  # the cache key's business, not the transport's
    try:
        from crewaimeat.aimeat_crew import _aimeat_rest  # noqa: PLC0415
    except Exception:  # noqa: BLE001 — no scaffold in this process
        return None
    try:
        data = _aimeat_rest(agent_name, "GET", "/v1/agents/me/directives")
    except Exception as exc:  # noqa: BLE001 — transient / offline: said out loud, then run without
        print(
            f"[llm] {agent_name}: directives fetch failed ({type(exc).__name__}: {exc}); running with the last known block",
            file=sys.stderr,
        )
        return None
    return data if isinstance(data, dict) else None


def format_directives(data: dict | None) -> str:
    """Render purpose + rules into a behavioral-constraints block for the prompt.

    system/owner rules are binding policy; agent rules are the agent's own standing notes. All are
    framed as directives to follow. Returns "" when there is nothing to apply."""
    if not isinstance(data, dict):
        return ""
    purpose = (data.get("purpose") or "").strip()
    rules = [r for r in (data.get("rules") or []) if isinstance(r, dict) and (r.get("description") or "").strip()]
    if not purpose and not rules:
        return ""
    lines = [
        DIRECTIVES_HEADER + " — follow these in everything you produce. They apply "
        "to YOU; if you delegate work to other crews, do NOT copy these into their instructions — "
        "each crew already applies its own directives):"
    ]
    if purpose:
        lines.append(f"- Purpose: {purpose}")
    label = {"system": "policy", "owner": "policy", "agent": "standing"}
    for r in rules:
        lines.append(f"- [{label.get(r.get('source'), 'rule')}] {r.get('description', '').strip()}")
    return "\n".join(lines)


# (agent_name, owner) -> (block, fetched_at). One process, many agent threads: a lock, not a race.
_CACHE: dict[tuple[str, str | None], tuple[str, float]] = {}
_CACHE_LOCK = threading.Lock()


def directives_block(
    agent_name: str,
    owner: str | None = None,
    *,
    ttl_s: float = DIRECTIVES_TTL_S,
    fetch: Callable[[str, str | None], dict | None] | None = None,
    now: Callable[[], float] = time.monotonic,
) -> str:
    """The formatted block for this agent, fetched at most once per `ttl_s`.

    A failed fetch (None) keeps the last known block rather than dropping to nothing: an owner's
    rule should not switch off because the node blinked. A fetch that answers "no directives" is a
    real answer and clears the block. `fetch` is resolved at call time so a test can stand in for
    the node by patching this module's fetch_directives."""
    key = (agent_name, owner)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
    t = now()
    if cached is not None and t - cached[1] < ttl_s:
        return cached[0]
    data = (fetch or fetch_directives)(agent_name, owner)
    block = cached[0] if data is None and cached is not None else format_directives(data)
    with _CACHE_LOCK:
        _CACHE[key] = (block, t)
    return block


def clear_cache() -> None:
    """Forget every fetched block (tests, and a daemon that wants an owner edit right now)."""
    with _CACHE_LOCK:
        _CACHE.clear()


def carries_directives(messages: Any) -> bool:
    """True when some message already contains the block, however it got there."""
    if isinstance(messages, str):
        return DIRECTIVES_HEADER in messages
    if isinstance(messages, list):
        for m in messages:
            content = m.get("content") if isinstance(m, dict) else None
            if isinstance(content, str) and DIRECTIVES_HEADER in content:
                return True
            if isinstance(content, list):  # multimodal parts
                for part in content:
                    if isinstance(part, dict) and DIRECTIVES_HEADER in str(part.get("text") or ""):
                        return True
    return False


def with_directives(messages: Any, block: str) -> Any:
    """`messages` with the block as the leading system text. Pure; the input is not mutated.

    A plain string becomes [system, user]. A list gets the block prepended into its first system
    message when it has one, else a new system message at the front, because some providers take
    exactly one system turn. A list that already carries the block is returned as it came."""
    if not block or carries_directives(messages):
        return messages
    if isinstance(messages, str):
        return [{"role": "system", "content": block}, {"role": "user", "content": messages}]
    if not isinstance(messages, list):
        return messages
    out = [dict(m) if isinstance(m, dict) else m for m in messages]
    first = out[0] if out else None
    if isinstance(first, dict) and first.get("role") == "system" and isinstance(first.get("content"), str):
        first["content"] = block + "\n\n" + first["content"]
        return out
    return [{"role": "system", "content": block}, *out]


def install_directives(
    llm: Any,
    agent_name: str | None,
    owner: str | None = None,
    *,
    block_for: Callable[..., str] = directives_block,
) -> Any:
    """Wrap `llm.call` so every completion for `agent_name` carries the owner's directives.

    Idempotent (a second install is a no-op), best-effort (a wrapper that cannot be installed is
    reported once and the LLM works as before), and inert when there is no agent to fetch for."""
    if not agent_name or llm is None or getattr(llm, "_aimeat_directives", None) == agent_name:
        return llm
    inner = getattr(llm, "call", None)
    if not callable(inner):
        return llm

    def call(messages, *args, **kwargs):
        try:
            block = block_for(agent_name, owner)
        except Exception as exc:  # noqa: BLE001 — a directive fetch must never break generation
            print(
                f"[llm] {agent_name}: directives unavailable ({type(exc).__name__}); calling without", file=sys.stderr
            )
            block = ""
        return inner(with_directives(messages, block) if block else messages, *args, **kwargs)

    try:
        object.__setattr__(llm, "call", call)
        object.__setattr__(llm, "_aimeat_directives", agent_name)
    except Exception as exc:  # noqa: BLE001
        print(
            f"[llm] {agent_name}: could not install directives on {type(llm).__name__} ({type(exc).__name__})",
            file=sys.stderr,
        )
    return llm

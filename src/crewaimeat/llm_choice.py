"""The owner's model choice, made on the AIMEAT node and read here.

WHY THE NODE AND NOT A FILE. A crew definition already lives on the node (`crews.registry.<agent>`),
and the person editing it is looking at a web page, not at this machine's `llm_providers.json`. Until
now the only way to say "this agent thinks with that model" was `llm_overrides.json` under
AIMEAT_HOME, reachable from the TUI on the box the fleet happens to run on. So the definition and the
model that runs it were configured in two different places by two different people.

THE KEYS, both in the OWNER's own namespace so the person's own tools can read and write them:

    crews.llm.default        the owner's default for every agent they own
    crews.llm.<agent>        one agent's own choice, when it differs

Each holds `{"kind": "profile", "profile": "<name>"}` or
`{"kind": "model", "label": "...", "provider": {<one-model provider dict>}}` — the SAME two shapes
`llm_overrides.json` already stores, so `_select_chain` resolves them with the code it already has
and nothing new had to learn what a provider is.

READS ARE CACHED AND NEVER FATAL. `get_llm` runs on every task, and a node round trip per task would
put a network hop in front of every model call. Sixty seconds is short enough that a change made in
the browser lands within a task or two, and long enough that a busy fleet does not poll. A node that
cannot be reached returns None, which means "no choice from the node" and lets the file decide —
routing must never depend on a network being up.

WHAT THIS DOES NOT DO. Store keys or secrets: a `model` choice carries `api_key_env`, the NAME of an
environment variable on this machine, exactly as the local override store does. The node never sees a
credential.
"""

from __future__ import annotations

import os
import time
from typing import Any

DEFAULT_KEY = "crews.llm.default"
CATALOG_KEY = "crews.llm.catalog"

#: How long a read is reused. Overridable for tests and for a fleet that wants it tighter.
CACHE_TTL_S = float(os.getenv("AIMEAT_LLM_CHOICE_TTL_S", "60"))

_CACHE: dict[str, tuple[float, Any]] = {}


def key_for(agent_name: str) -> str:
    """`crews.llm.<agent>` — one agent's own choice."""
    return f"crews.llm.{agent_name}"


def _read(agent_name: str, key: str) -> Any:
    """One cached owner-scope read. None on anything that is not a live answer."""
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit and now - hit[0] < CACHE_TTL_S:
        return hit[1]
    try:
        from crewaimeat.memory_tools import read_owner_key

        val = read_owner_key(agent_name, key)
    except Exception:  # noqa: BLE001 — a routing lookup must never take the fleet down
        val = None
    _CACHE[key] = (now, val)
    return val


def _valid(choice: Any) -> dict | None:
    """A choice the resolver can actually use, or None. Same two shapes as the local override store."""
    if not isinstance(choice, dict):
        return None
    kind = choice.get("kind")
    if kind == "profile" and isinstance(choice.get("profile"), str) and choice["profile"].strip():
        return choice
    if kind == "model" and isinstance(choice.get("provider"), dict):
        return choice
    return None


def node_choice(agent_name: str | None) -> tuple[dict | None, str | None]:
    """`(choice, scope)` for `agent_name`: the agent's own first, then the owner's default.

    `scope` is 'agent' or 'default' and is only for saying WHY a model was picked in the log line —
    "web-researcher -> node:agent coding" is a sentence somebody can act on, "coding" is not.
    """
    if not agent_name:
        return None, None
    own = _valid(_read(agent_name, key_for(agent_name)))
    if own:
        return own, "agent"
    shared = _valid(_read(agent_name, DEFAULT_KEY))
    if shared:
        return shared, "default"
    return None, None


def forget(agent_name: str | None = None) -> None:
    """Drop the cache, so the next read asks the node. Called after this runtime writes a choice."""
    if agent_name is None:
        _CACHE.clear()
        return
    _CACHE.pop(key_for(agent_name), None)
    _CACHE.pop(DEFAULT_KEY, None)


def publish_catalog(agent_name: str) -> bool:
    """Tell the node which profiles and models this machine can actually reach.

    The picker on the node has to offer something, and only this side knows what `llm_providers.json`
    holds. Written to `crews.llm.catalog` in the owner's namespace, best-effort: a fleet that cannot
    publish its catalogue still runs, the page just falls back to typing a profile name.

    NO SECRETS. Each model carries its provider dict as `available_models()` builds it, which names
    an `api_key_env` and never a key.
    """
    from crewaimeat.llm import available_models, known_profiles

    payload = {
        "spec": "aimeat.llm-catalog/1",
        "profiles": known_profiles(),
        "models": available_models(),
        "reportedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        from crewaimeat.aimeat_crew import _aimeat_call

        _aimeat_call(
            agent_name,
            "aimeat_memory_write",
            {"key": CATALOG_KEY, "value": payload, "visibility": "owner", "owner_scope": True, "tags": ["llm-catalog"]},
        )
        return True
    except Exception as exc:  # noqa: BLE001 — best-effort by design
        print(f"[llm] could not publish the model catalogue: {type(exc).__name__}: {exc}")
        return False

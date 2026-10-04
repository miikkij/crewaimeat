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
and nothing new had to learn what a provider is — or, since 2026-10-02, `{"kind": "node", "role"?:
"<AI role id>"}`: THE NODE ROAD. The crew's model calls go to the node's `/v1/llm` with the agent's
own credential, and the node picks the model and the key (the agent's own, the owner's own, then the
place's from the owner's allowance). It is what a hosted place writes as every new owner's default, so
it is honoured at the default scope too, and `llm.get_llm` resolves it before the providers file
(aimeat-protocol brief doc-muqud1ah2zvl, Jouni's ruling the same day).

A `model` choice is checked on read with the node's own guard (`aimeat_crewai.unsafe_choice_reason`):
it names a key variable and an address, and a crew run inherits this machine's environment, so a
record that names `AIMEAT_ENCRYPTION_KEY` or a private address would send a secret somewhere. The node
refuses to store one since 2026-10-02; a record saved before is still readable. A refused choice is no
choice, and the reason is logged once per agent.

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
import sys
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


_REFUSED_LOGGED: set[tuple[str, str]] = set()


def model_choice_refusal(choice: dict) -> str | None:
    """Why a `model` choice must not be used, in the node's words; None when it may be.

    The guard is the node's own (aimeat-crewai 0.32.0). A runtime without it cannot check the choice,
    and an unchecked choice could send this machine's secret anywhere, so it is refused with that said.
    """
    try:
        from aimeat_crewai import unsafe_choice_reason
    except ImportError:
        return "this runtime cannot check a model choice (aimeat-crewai 0.32.0 has the node's guard)"
    return unsafe_choice_reason(choice)


def _valid(choice: Any, agent_name: str | None = None) -> dict | None:
    """A choice the resolver can actually use, or None: a profile, a SAFE model, or the node road."""
    if not isinstance(choice, dict):
        return None
    kind = choice.get("kind")
    if kind == "profile" and isinstance(choice.get("profile"), str) and choice["profile"].strip():
        return choice
    if kind == "node":
        role = choice.get("role")
        return choice if role is None or (isinstance(role, str) and role.strip()) else None
    if kind == "model" and isinstance(choice.get("provider"), dict):
        reason = model_choice_refusal(choice)
        if reason is None:
            return choice
        key = (str(agent_name), reason)
        if key not in _REFUSED_LOGGED:  # once per agent and reason: get_llm runs on every task
            _REFUSED_LOGGED.add(key)
            print(f"[llm] {agent_name or '?'}: the owner's model choice is not used: {reason}", file=sys.stderr)
        return None
    return None


def is_node_road(choice: Any) -> bool:
    return isinstance(choice, dict) and choice.get("kind") == "node"


_NO_ANSWER = object()


def _effective(agent_name: str) -> Any:
    """The node's own answer to "which choice applies to this agent's crew" (aimeat-protocol 076991f0d,
    GET /v1/agents/{name}/crew/llm): `{value, scope, why, key_source?}`. `_NO_ANSWER` when the node has
    no such route or could not be asked, and then the memory keys are read as before.

    WHY THE NODE'S ANSWER AND NOT THE KEYS. The choice that applies is not only what is stored: the node
    road needs `ai:use` and something that pays, and the node knows both, the keys do not. An owner's
    `{kind:'node'}` default skips an agent without `ai:use` (it would only be refused 403 on its first
    call), and with nothing stored the node itself is the default for an agent that holds `ai:use` when a
    key pays. One rule, kept where the facts are. The agent reads its own answer with no permission word.
    """
    now = time.monotonic()
    ck = f"effective:{agent_name}"
    hit = _CACHE.get(ck)
    if hit and now - hit[0] < CACHE_TTL_S:
        return hit[1]
    try:
        from crewaimeat.agent_manifest import agent_local_name
        from crewaimeat.aimeat_crew import _aimeat_rest

        res = _aimeat_rest(
            agent_name, "GET", f"/v1/agents/{agent_local_name(agent_name)}/crew/llm", retries=1, return_error=True
        )
    except Exception:  # noqa: BLE001 — a routing lookup must never take the fleet down
        res = None
    answer = res if isinstance(res, dict) and res.get("ok") is not False and "value" in res else _NO_ANSWER
    _CACHE[ck] = (now, answer)
    return answer


def node_choice(agent_name: str | None) -> tuple[dict | None, str | None]:
    """`(choice, scope)` for `agent_name`: the node's effective answer when it gives one; otherwise the
    agent's own stored choice first, then the owner's default.

    `scope` is 'agent', 'default' or 'node'. 'agent' and 'default' are choices somebody MADE (the owner,
    for this agent or for all of theirs; a hosted place writes the default for every new owner). 'node'
    is the node's own offer when NOTHING is chosen ("the agent holds ai:use and this node has a key"):
    llm._node_road_choice lets this machine's own routing run the agent over it, because a fleet whose
    agents hold `*` would otherwise leave its llm_providers.json profiles without anyone deciding so
    (measured 2026-10-04, the Sanomat edition that failed on the node road).
    """
    if not agent_name:
        return None, None
    answer = _effective(agent_name)
    if answer is not _NO_ANSWER:
        choice = _valid(answer.get("value"), agent_name)
        if choice is None:
            return None, None
        scope = answer.get("scope")
        return choice, (scope if scope in ("agent", "node") else "default")
    own = _valid(_read(agent_name, key_for(agent_name)), agent_name)
    if own:
        return own, "agent"
    shared = _valid(_read(agent_name, DEFAULT_KEY), agent_name)
    if shared:
        return shared, "default"
    return None, None


def default_is_node_road(agent_name: str | None) -> bool:
    """True when the OWNER's default is the node road -- the road a new agent of theirs will take, which
    is what a proposer needs to know about an agent that has no choice of its own yet."""
    if not agent_name:
        return False
    return is_node_road(_valid(_read(agent_name, DEFAULT_KEY), agent_name))


def forget(agent_name: str | None = None) -> None:
    """Drop the cache, so the next read asks the node. Called after this runtime writes a choice."""
    if agent_name is None:
        _CACHE.clear()
        return
    _CACHE.pop(key_for(agent_name), None)
    _CACHE.pop(DEFAULT_KEY, None)
    _CACHE.pop(f"effective:{agent_name}", None)


def publish_catalog(agent_name: str) -> bool:
    """Tell the node which profiles and models this machine can actually reach.

    The picker on the node has to offer something, and only this side knows what `llm_providers.json`
    holds. Written to `crews.llm.catalog` in THIS AGENT's own namespace, best-effort: a fleet that
    cannot publish its catalogue still runs, the page just falls back to typing a profile name.

    NOT THE OWNER'S NAMESPACE. `crews.llm.` is reserved there, so an agent's write is refused unless it
    holds memory:write-reserved, which would also let it write the owner's AI key settings and payment
    provider. The node reads each agent's catalogue from that agent (aimeat services/crew-menu.ts),
    which also stops agents on two machines overwriting one shared list.

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
            {"key": CATALOG_KEY, "value": payload, "visibility": "owner", "tags": ["llm-catalog"]},
        )
        return True
    except Exception as exc:  # noqa: BLE001 — best-effort by design
        print(f"[llm] could not publish the model catalogue: {type(exc).__name__}: {exc}")
        return False

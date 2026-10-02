"""Tell the node which ROAD an agent's model calls take: 'node' (its /v1/llm, with the agent's own
credential) or 'machine' (a provider, with a key on the machine the crew runs on).

WHY. An owner who saves their own AI key on their node expects their agents to use it. The node can
only say "your key reaches your crews" when every one of them reported the node road
(GET /v1/chat/status `own_key.agents`, the owner's AI page). Nothing in crewaimeat reported it, so the
answer was always "unknown" (aimeat-protocol brief doc-muqud1ah2zvl, 2026-10-02).

WHEN. `llm.get_llm` resolves the road on every build; this reports it the first time and again only
when it changes (the owner switched the place to the node, or back). A report the node refuses is
logged and not repeated for the same road: since aimeat-protocol 6e999056d an agent reports ITSELF with
no permission word, and an older node that still wants agent:write will want it next time too.

WHAT. `aimeat_agent_runtime_report` replaces the agent's runtime record, so it carries everything this
side knows: the kind (a JSON crew-def, or a python crew file), the runtime and its version, the road,
and for a JSON crew the definition revision that is live.
"""

from __future__ import annotations

import sys
import threading

_LOCK = threading.Lock()
# agent -> the (road, revision) last reported, so a road is said once and a change is said at once.
_REPORTED: dict[str, tuple[str, int | None]] = {}
# agent -> the live JSON definition's revision (None: a definition without one). Absent: a python crew.
_DEFINITIONS: dict[str, int | None] = {}


def note_definition(agent_name: str, revision) -> None:
    """A JSON crew's definition was loaded: it is a crew-def, at this revision."""
    rev = revision if isinstance(revision, int) and not isinstance(revision, bool) else None
    with _LOCK:
        _DEFINITIONS[agent_name] = rev


def _runtime() -> str:
    try:
        from importlib.metadata import version

        return f"crewaimeat {version('crewaimeat')}"
    except Exception:  # noqa: BLE001 -- a checkout without metadata still runs
        return "crewaimeat unknown"


def report(agent_name: str | None, road: str) -> bool:
    """Report `road` for `agent_name` unless it already was. True when a report was sent and accepted."""
    if not agent_name or road not in ("node", "machine"):
        return False
    with _LOCK:
        is_def = agent_name in _DEFINITIONS
        revision = _DEFINITIONS.get(agent_name)
        state = (road, revision)
        if _REPORTED.get(agent_name) == state:
            return False
        _REPORTED[agent_name] = state  # before the call: a refusal is said once, not on every task
    from crewaimeat.agent_manifest import agent_local_name
    from crewaimeat.aimeat_crew import _aimeat_call

    payload = {
        "target_agent_name": agent_local_name(agent_name),
        "kind": "crew-def" if is_def else "python",
        "runtime": _runtime(),
        "llm": road,
    }
    if is_def and revision is not None:
        payload["definition_revision"] = revision
    try:
        answer = _aimeat_call(agent_name, "aimeat_agent_runtime_report", payload, return_error=True)
    except Exception as exc:  # noqa: BLE001 -- a status report must never take the agent down
        answer = {"ok": False, "error": {"code": type(exc).__name__, "message": str(exc)}}
    if isinstance(answer, dict) and answer.get("ok") is False or answer is None:
        err = (answer or {}).get("error") or {}
        print(
            f"[llm] {agent_name}: could not report its model road '{road}' to the node "
            f"({err.get('code', 'no answer')}: {err.get('message', '')})",
            file=sys.stderr,
        )
        return False
    print(f"[llm] {agent_name}: reported model road '{road}' to the node", file=sys.stderr)
    return True


def forget() -> None:
    """For tests: report everything again."""
    with _LOCK:
        _REPORTED.clear()
        _DEFINITIONS.clear()

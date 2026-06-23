"""Agent-to-agent orchestration — the delegating-router substrate.

A coordinator (the concierge) uses this to turn the fleet into a DM-callable SERVICE MESH: discover the
LIVE sibling services, DELEGATE a user's request to the right one over the federated inbox, then RELAY
that specialist's reply back to the original user.

Trust model — same-owner siblings are a CONSENTED internal mesh, not strangers. `aimeat_agents_list`
returns only the CALLER'S OWN agents (same owner, this node), so every gaii it yields is a sibling we
own. Delegating to one therefore does NOT trip the owner-gate (`dm.dm_initiate`) — that gate exists to
stop cold-DMing strangers across owners, which this never does. We still never reach beyond our own roster.

Correlation — the concierge<->specialist thread is the key. When we delegate we remember, UNDER that
thread's conversation id, who the original user was (their gaii + their thread). When the specialist
replies on that same thread, its inbound event carries the same conversation id, so we look the pending
delegation up by it and forward the reply to the user. One delegation per specialist-thread at a time.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

from crewaimeat.aimeat_crew import _aimeat_call
from crewaimeat.dm import dm_send
from crewaimeat.session_store import session_clear, session_get, session_set

_PENDING_KEY = "delegation"
_FRESH_SECONDS = 900  # a sibling whose daemon reported in within 15 min is "live" (fleet polls every 30 s)


def list_node_agents(agent: str) -> list[dict]:
    """The caller's OWN agents on the node (aimeat_agents_list, shell-callable) -> [{name, gaii, mode,
    last_seen, tags, ...}]. Same owner + node by construction, so the result is the trusted sibling set."""
    r = _aimeat_call(agent, "aimeat_agents_list", {}) or {}
    ags = r.get("agents") if isinstance(r, dict) else r
    if isinstance(ags, dict):
        ags = ags.get("agents", [])
    return ags or []


def _is_fresh(last_seen: str | None, max_age_s: int = _FRESH_SECONDS) -> bool:
    """True if `last_seen` (ISO-8601) is within max_age_s of now — i.e. the sibling's daemon is up."""
    if not last_seen:
        return False
    try:
        ts = datetime.fromisoformat(str(last_seen).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ts).total_seconds() <= max_age_s
    except Exception:  # noqa: BLE001
        return False


def services_from_roster(
    roster: list[dict], directory: dict[str, str], *, max_age_s: int = _FRESH_SECONDS
) -> list[dict]:
    """Intersect a curated `directory` ({agent_name: "use-when" description}) with an ALREADY-FETCHED roster
    so the router only ever offers specialists whose daemon is actually up. -> [{name, gaii, desc,
    last_seen}], in `directory` order. A specialist deleted server-side or with a stale daemon is dropped.
    Prefer this over live_services when you already hold the roster (one aimeat_agents_list call serves both
    the menu and the loop-guard — keeps idle/per-DM node traffic minimal)."""
    by_name = {a.get("name"): a for a in roster}
    out: list[dict] = []
    for name, desc in directory.items():
        a = by_name.get(name)
        if not a or not a.get("gaii") or not _is_fresh(a.get("last_seen"), max_age_s):
            continue
        out.append({"name": name, "gaii": a["gaii"], "desc": desc, "last_seen": a.get("last_seen")})
    return out


def live_services(agent: str, directory: dict[str, str], *, max_age_s: int = _FRESH_SECONDS) -> list[dict]:
    """Fetch the roster and return the live services (see services_from_roster)."""
    return services_from_roster(list_node_agents(agent), directory, max_age_s=max_age_s)


def in_roster(roster: list[dict], sender: str | None) -> bool:
    """True if `sender` (a gaii like 'name#owner@node', or a bare name) is one of OUR own fleet agents.
    Used to SUPPRESS chatter from a sibling agent that isn't a tracked delegation — two dm_serviceable
    crews would otherwise loop on each other (each treats the other's reply as a fresh request)."""
    if not sender:
        return False
    name = sender.split("#", 1)[0]
    return any(a.get("name") == name for a in roster)


def directory_text(services: list[dict]) -> str:
    """Render live services as a menu for the router prompt. Empty -> a clear 'none available' line."""
    if not services:
        return "(no specialists are available right now — handle the request yourself)"
    return "\n".join(f"- **{s['name']}** — {s['desc']}" for s in services)


def _conv_id(res) -> str | None:
    """Pull a conversation id out of a dm_send result, tolerant of envelope/camel/snake shapes."""
    if not isinstance(res, dict):
        return None
    for src in (res, res.get("data") if isinstance(res.get("data"), dict) else {}):
        if not isinstance(src, dict):
            continue
        cid = src.get("conversation_id") or src.get("conversationId")
        if cid:
            return cid
    return None


def delegate(agent: str, specialist_gaii: str, request: str, *, subject: str = "Request via concierge") -> str | None:
    """Open a thread to a sibling specialist and send the request (consented intra-fleet, no owner-gate).
    Returns the concierge<->specialist conversation id to correlate the reply on, or None on failure."""
    res = dm_send(agent, specialist_gaii, request, subject=subject)
    return _conv_id(res)


def record_delegation(agent: str, specialist_conv: str, *, user_to: str, user_conv: str, specialist: str, request: str):
    """Remember (under the specialist-thread conv) who to relay the eventual reply back to."""
    session_set(
        agent,
        specialist_conv,
        _PENDING_KEY,
        {
            "user_to": user_to,
            "user_conv": user_conv,
            "specialist": specialist,
            "request": request,
            "ts": int(time.time()),
        },
    )


def match_delegation(agent: str, conv: str | None, sender: str | None) -> dict | None:
    """If `conv` has a pending delegation AND `sender` is that specialist, return the pending payload and
    CLEAR it (so a duplicate reply can't double-relay). Otherwise None — the caller handles it normally."""
    if not conv or not sender:
        return None
    pending = session_get(agent, conv, _PENDING_KEY)
    if not pending:
        return None
    if sender.split("#", 1)[0] != pending.get("specialist"):
        return None
    session_clear(agent, conv, _PENDING_KEY)
    return pending

"""Per-subscriber key spaces for Aamukatsaus, on the node's share model.

THE SHAPE, in the node team's words (2026-08-11): a share is its own thing —
(owner, group, key_pattern). The record stays PRIVATE; visibility is the floor and a share is a
named exception on top. `*` matches one segment, `**` the subtree, and a `**` share covers keys
written LATER — which is the whole reason a subscription works without touching the share again.

WHO DOES WHAT, measured on the live API 2026-08-14 rather than taken from the docs:

    OWNER   creates the group and admits the subscriber's GHII.
            `POST /v1/groups` AND the `aimeat_group_create` MCP tool both answer an agent token
            with ACCESS_DENIED: Role "owner" required. MCP does not bypass it — this one is gated
            at the role, not the transport. The boundary is deliberate: an agent may hand out
            access to a key space, but it must never assemble its own audience.

    AGENT   creates ONE share of `aamukatsaus.<sub>.**` to that group  ->  provision()
            Needs the `share:manage` scope, which NO WILDCARD CARRIES. `scopes:["*"]` is not
            enough; the node keeps it out of every wildcard so that nobody ticking "full access"
            is thereby deciding an agent may publish their memory to strangers.

After that the daily write is an ORDINARY PRIVATE RECORD:

    aamukatsaus.<sub>.2026-08-14   ->   no group id, no visibility juggling, nothing to remember

The subscriber reads `GET /v1/memory/<our-ghii>/<key>` with their own credential; membership
resolves on the node. Ending it is revoking the share — reads stop at once. A copy the reader
already took stays theirs, which is true of every revocation anywhere.

Everything here goes through the connector's MCP tools (`aimeat_share_create`, `aimeat_share_list`,
`aimeat_share_revoke`) rather than REST, which is the surface the fleet already speaks.
"""

from __future__ import annotations

import datetime
import re
import sys

from crewaimeat.aimeat_crew import _aimeat_call

AGENT = "postman"  # the agent that owns the Aamukatsaus data and its shares
SPACE_ROOT = "aamukatsaus"

# A subscriber id becomes part of a memory key and of a share pattern, so it may not contain the
# separators those grammars use. Rejected at the boundary rather than sanitised: a silently
# rewritten id would produce a share that does not cover the keys we then write.
_SUB_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,38}[a-z0-9]$")


def space_of(subscriber: str) -> str:
    """The key space one subscriber owns. Validated, because it is half of a permission."""
    if not _SUB_RE.match(subscriber or ""):
        raise ValueError(
            f"invalid subscriber id {subscriber!r} — use 3-40 chars of a-z, 0-9, '-' or '_'. "
            "The id becomes part of both the memory key and the share pattern, so a rewritten one "
            "would silently produce a share that does not cover the keys we write."
        )
    return f"{SPACE_ROOT}.{subscriber}"


def pattern_of(subscriber: str) -> str:
    """`**` — the subtree, including keys written later. That is what makes it a subscription."""
    return f"{space_of(subscriber)}.**"


class OwnerActionRequired(RuntimeError):
    """The step needs the OWNER's own hands — an agent cannot do it, by design."""


def _tool(name: str, payload: dict, *, agent: str = AGENT):
    """Call a connector tool. MCP rather than REST: `POST /v1/groups` refuses an agent token with
    'Role "owner" required', and while MCP tools normally bypass that HTTP middleware, group
    creation is gated at the role itself and refuses on both surfaces (measured 2026-08-14)."""
    return _aimeat_call(agent, name, payload)


def provision(subscriber: str, group_id: str, *, agent: str = AGENT) -> dict:
    """Share this subscriber's key space with a group the OWNER has already created.

    WHY `group_id` IS A PARAMETER AND NOT SOMETHING WE CREATE. Measured 2026-08-14, on both
    surfaces: `POST /v1/groups` and the `aimeat_group_create` MCP tool both refuse an agent token
    with `ACCESS_DENIED: Role "owner" required`. Admitting a member is the same. That boundary is
    deliberate — an agent may hand out access to a key space, but it must never be able to assemble
    its own audience — so the two halves belong to different principals:

        OWNER  creates the group and admits the subscriber's GHII (browser, or an owner MCP session)
        AGENT  creates ONE share of `aamukatsaus.<sub>.**` to that group   <- this function

    The share half needs `share:manage`, which no wildcard carries: `scopes:["*"]` is NOT enough and
    the owner grants it per agent. Without it this raises with the node's own words rather than a
    generic failure."""
    pattern = pattern_of(subscriber)
    out: dict = {"subscriber": subscriber, "space": space_of(subscriber), "pattern": pattern, "group_id": group_id}

    for s in shares_out(agent=agent):
        if s.get("key_pattern") == pattern:
            out["share"] = s
            out["note"] = "share already existed — left alone"
            return out

    share = _tool("aimeat_share_create", {"group_id": group_id, "key_pattern": pattern}, agent=agent)
    if share is None:
        raise OwnerActionRequired(
            f"could not share {pattern} with group {group_id}. The two causes, in order:\n"
            f"  1. `{agent}` lacks the `share:manage` scope. No wildcard carries it — the node keeps\n"
            "     it out of every one on purpose, so that nobody ticking 'full access' is thereby\n"
            "     deciding an agent may publish their memory to strangers. Grant it per agent.\n"
            "  2. the group id does not exist, or is not one this owner owns."
        )
    out["share"] = share
    print(f"[{agent}] subscriber {subscriber}: share {pattern} -> group {group_id}", file=sys.stderr)
    return out


def publish(subscriber: str, value, date: str | None = None, *, agent: str = AGENT) -> str | None:
    """Write one day into the subscriber's space as a PLAIN PRIVATE record.

    No group id and no visibility flag: the share is the exception that makes it readable, and it
    already covers this key. Writing it `public` or `group` here would be the old model leaking
    back in — and `public` would hand it to everyone, not to the subscriber."""
    key = f"{space_of(subscriber)}.{date or datetime.date.today().isoformat()}"
    if _aimeat_call(agent, "aimeat_memory_write", {"key": key, "value": value, "visibility": "private"}) is None:
        print(f"[{agent}] subscriber {subscriber}: write {key} FAILED", file=sys.stderr)
        return None
    return key


def shares_out(*, agent: str = AGENT) -> list[dict]:
    """What we have given away."""
    d = _tool("aimeat_share_list", {"direction": "outgoing"}, agent=agent) or {}
    return d.get("shares") or d.get("items") or []


def shares_in(*, agent: str = AGENT) -> list[dict]:
    """What has been given to US — `GET /v1/shares/incoming`.

    The half whose absence made groups unusable: without it nobody could find what they had been
    handed. Worth calling even when we are only the giver — it is how an agent discovers that some
    OTHER organism has started sharing a space with us."""
    d = _tool("aimeat_share_list", {"direction": "incoming"}, agent=agent) or {}
    return d.get("shares") or d.get("items") or []


def revoke(subscriber: str, *, agent: str = AGENT) -> bool:
    """End a subscription: delete the share. Reads stop at once; copies already taken stay theirs.

    The group and its membership are left in place — re-subscribing is then one share again, and
    removing someone from an audience is a different decision from ending their subscription."""
    pattern = pattern_of(subscriber)
    for s in shares_out(agent=agent):
        if s.get("key_pattern") != pattern:
            continue
        sid = s.get("id") or s.get("share_id")
        if _tool("aimeat_share_revoke", {"share_id": sid}, agent=agent) is not None:
            print(f"[{agent}] subscriber {subscriber}: share revoked ({pattern})", file=sys.stderr)
            return True
        print(f"[{agent}] subscriber {subscriber}: revoke FAILED for share {sid}", file=sys.stderr)
        return False
    print(f"[{agent}] subscriber {subscriber}: no share matching {pattern}", file=sys.stderr)
    return False

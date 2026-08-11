"""Per-subscriber key spaces for Aamukatsaus, on the node's share model.

THE SHAPE, in the node team's words (2026-08-11): a share is its own thing —
(owner, group, key_pattern). The record stays PRIVATE; visibility is the floor and a share is a
named exception on top. `*` matches one segment, `**` the subtree, and a `**` share covers keys
written LATER — which is the whole reason a subscription works without touching the share again.

So a subscriber costs three calls ONCE:

    group   = POST /v1/groups                          (owner-role act — see the scope note)
    member  = POST /v1/groups/{group}/members          (owner-role act)
    share   = POST /v1/groups/{group}/shares           {"key_pattern": "aamukatsaus.<sub>.**"}

and after that the daily write is an ORDINARY PRIVATE RECORD:

    aamukatsaus.<sub>.2026-08-12   ->   no group id, no visibility juggling, nothing to remember

The subscriber reads `GET /v1/memory/<our-ghii>/<key>` with their own credential; membership
resolves on the node. Ending it is deleting the share or removing the member — reads stop at once.
A copy the reader already took stays theirs, which is true of every revocation anywhere.

TWO SCOPES, AND NEITHER COMES WITH `*`:
  · `share:manage`   — required to create a share. The node deliberately keeps it OUT of any
                       wildcard: nobody ticking "full access" is thereby deciding that an agent may
                       publish their memory to strangers. Our agents hold `scopes:["*"]`, which is
                       NOT enough. The owner grants it per agent.
  · `consent:groups` — required to create a group and admit members. An app-grant can never do this
                       (its roles are ["app"]): an app may hand out access to a key space, but it
                       cannot assemble its own audience. That boundary is why provisioning runs
                       here, agent-side, and only the sharing half could ever move into the app.

NOT LIVE YET at the time of writing: the share API is committed on a node branch, not merged, and
aimeat.io has not been redeployed. Every call below therefore reports a 404 as "not deployed yet"
rather than as a failure, so this module can ship, be read, and be exercised the moment it lands.
"""

from __future__ import annotations

import datetime
import re
import sys

from crewaimeat.aimeat_crew import _aimeat_call, _aimeat_rest

AGENT = "postman"  # the agent that owns the Aamukatsaus data and its shares
SPACE_ROOT = "aamukatsaus"

# A subscriber id becomes part of a memory key and of a share pattern, so it may not contain the
# separators those grammars use. Rejected at the boundary rather than sanitised: a silently
# rewritten id would produce a share that does not cover the keys we then write.
_SUB_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,38}[a-z0-9]$")


class ShareApiUnavailable(RuntimeError):
    """The share API answered 404 — the node build carrying it is not deployed here yet."""


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


def _rest(method: str, path: str, body: dict | None = None, *, agent: str = AGENT):
    data = _aimeat_rest(agent, method, path, body)
    if data is None:
        # _aimeat_rest already logged the real status. Distinguish "not deployed" from "refused"
        # by probing the collection route, so a missing build never reads as a permission problem.
        probe = _aimeat_rest(agent, "GET", "/v1/shares")
        if probe is None:
            raise ShareApiUnavailable(
                f"{method} {path} failed and GET /v1/shares is also unavailable — the share API is "
                "most likely not deployed on this node yet (it was on a branch on 2026-08-11). "
                "Nothing was changed."
            )
    return data


def provision(subscriber: str, ghii: str, agent_gaiis: list[str] | None = None, *, agent: str = AGENT) -> dict:
    """Give one subscriber their own readable space. Idempotent-ish: safe to re-run, and it reports
    what already existed rather than duplicating it.

    `ghii` is the subscriber as a PERSON. `agent_gaiis` are any agents of theirs that should read it
    too — a subscriber whose own fleet consumes the briefing needs its agents in the group, because
    membership is matched per principal, not per household."""
    space, pattern = space_of(subscriber), pattern_of(subscriber)
    out: dict = {"subscriber": subscriber, "space": space, "pattern": pattern}

    existing = _rest("GET", "/v1/shares") or {}
    for s in existing.get("shares") or existing.get("items") or []:
        if isinstance(s, dict) and s.get("key_pattern") == pattern:
            out["share"] = s
            out["note"] = "share already existed — left alone"
            return out

    group = _rest(
        "POST",
        "/v1/groups",
        {"name": f"aamukatsaus-{subscriber}", "description": f"Aamukatsaus subscriber {subscriber}"},
    )
    gid = (group or {}).get("id") or ((group or {}).get("group") or {}).get("id")
    if not gid:
        raise RuntimeError(f"group creation returned no id for {subscriber!r}: {group!r}")
    out["group_id"] = gid

    members = [{"identifier": ghii, "identifier_type": "ghii"}] + [
        {"identifier": g, "identifier_type": "gaii"} for g in (agent_gaiis or [])
    ]
    out["members"] = []
    for m in members:
        _rest("POST", f"/v1/groups/{gid}/members", {**m, "permissions": {"read": True, "write": False}})
        out["members"].append(m["identifier"])

    # ONE share for the whole subtree. Not one per day: `**` covers keys written later, so the daily
    # write stays an ordinary private record and this is never touched again.
    out["share"] = _rest("POST", f"/v1/groups/{gid}/shares", {"key_pattern": pattern})
    print(
        f"[{agent}] subscriber {subscriber}: group {gid}, share {pattern}, {len(out['members'])} member(s)",
        file=sys.stderr,
    )
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
    """What we have given away — `GET /v1/shares`."""
    d = _rest("GET", "/v1/shares") or {}
    return d.get("shares") or d.get("items") or []


def shares_in(*, agent: str = AGENT) -> list[dict]:
    """What has been given to US — `GET /v1/shares/incoming`.

    The half whose absence made groups unusable: without it nobody could find what they had been
    handed. Worth calling even when we are only the giver — it is how an agent discovers that some
    OTHER organism has started sharing a space with us."""
    d = _rest("GET", "/v1/shares/incoming") or {}
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
        gid = s.get("group_id") or s.get("group")
        path = f"/v1/groups/{gid}/shares/{sid}" if gid else f"/v1/shares/{sid}"
        if _aimeat_rest(agent, "DELETE", path) is not None:
            print(f"[{agent}] subscriber {subscriber}: share revoked ({pattern})", file=sys.stderr)
            return True
        print(f"[{agent}] subscriber {subscriber}: revoke FAILED for {path}", file=sys.stderr)
        return False
    print(f"[{agent}] subscriber {subscriber}: no share matching {pattern}", file=sys.stderr)
    return False

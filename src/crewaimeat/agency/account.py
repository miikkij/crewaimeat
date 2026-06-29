"""account — the operator's identity context for the agency: which OWNER + home NODE their agents
live under. This is the answer to "where does my agent go, and does the app actually have access?"

There is no account password in the app. AIMEAT identity is per-agent and consent-based: the owner
approves each agent via device-auth in their own aimeat.io dashboard, and the connector stores a scoped
token at `<AIMEAT_HOME>/tokens/{agent}@{owner}.token`. So "the app has access to happydude500001 @
aimeat.io" means: the owner has approved at least one agent there, and its token works. The app must NOT
act against the node until that is true — nothing runs without the owner's explicit per-agent approval.

This module persists the chosen owner + node (so new agents register under them) and reports each
agent's authorization, reusing the connector's own token/auth primitives — it invents no new paths.
"""

from __future__ import annotations

import json
import os

from crewaimeat._home import aimeat_home

DEFAULT_NODE = "https://aimeat.io"


def _path() -> str:
    return str(aimeat_home() / "agency_account.json")


def load() -> dict:
    """The operator context {owner, node, owner_set}. The owner comes ONLY from what the user connected in
    this app (the saved agency_account.json) — NOT from an ambient AIMEAT_OWNER env, so a stray shell/system
    var on a dev machine can never silently skip onboarding. owner_set is False on a fresh install — the cue
    to show the first-run wizard. node defaults to aimeat.io."""
    owner = node = None
    try:
        with open(_path(), encoding="utf-8") as fh:
            doc = json.load(fh)
        owner = (doc.get("owner") or "").strip() or None
        node = (doc.get("node") or "").strip() or None
    except (OSError, ValueError):
        pass
    node = node or os.environ.get("AIMEAT_NODE_URL", "").strip() or DEFAULT_NODE
    return {"owner": owner, "node": node, "owner_set": bool(owner)}


def save(owner: str, node: str | None = None) -> dict:
    """Persist the operator's owner + home node and set AIMEAT_OWNER for this process (so the connector
    registers new agents under it). Returns the saved context."""
    owner = (owner or "").strip()
    if not owner:
        raise ValueError("owner is required")
    node = (node or "").strip() or DEFAULT_NODE
    home = aimeat_home()
    os.makedirs(home, exist_ok=True)
    with open(_path(), "w", encoding="utf-8") as fh:
        json.dump({"owner": owner, "node": node}, fh, indent=2, ensure_ascii=False)
    os.environ["AIMEAT_OWNER"] = owner
    return {"owner": owner, "node": node, "owner_set": True}


def apply_env() -> None:
    """At cockpit startup: if an owner was saved, export it as AIMEAT_OWNER so registration/REST calls
    in this process use the right identity even when the env wasn't pre-set."""
    doc = load()
    if doc["owner"] and not os.environ.get("AIMEAT_OWNER"):
        os.environ["AIMEAT_OWNER"] = doc["owner"]


def agent_auth(agent_name: str, owner: str | None) -> dict:
    """Whether the owner has approved this agent and the token works — the access check that gates
    running it. Reuses the connector's own token/auth primitives. `has_token` = device-auth completed;
    `authorized` = token present AND not actively rejected (a missing live-probe doesn't count as a
    rejection, so this still answers True offline once a token exists)."""
    from crewaimeat.aimeat_crew import _auth_alive, _token_exists

    has_token = _token_exists(agent_name, owner)
    if not has_token:
        return {"agent": agent_name, "has_token": False, "authorized": False}
    try:
        alive = _auth_alive(agent_name, owner)
    except Exception:  # noqa: BLE001 — no probe available offline; don't treat as rejected
        alive = None
    return {"agent": agent_name, "has_token": True, "authorized": alive is not False}

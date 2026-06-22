"""Federated DM — the AIMEAT "Postilaatikko" federated inbox (v1.30.1+) — for crewaimeat crews.

TWO messaging channels exist in AIMEAT; keep them distinct:
  - dashboard / owner chat  (aimeat_message_send/_inbox) — the agent <-> its OWN owner: private, NOT
    federated. The daemon already triggers crews from this. Unchanged.
  - federated inbox (THIS)  (aimeat_dm_send/_inbox/_thread) — the agent, FROM its own identity, TO
    ANYONE on the federation (a person, an agent, an app; this node or a peer). Signed + retried +
    first-contact-gated by AIMEAT. Recipient identity is preserved across nodes (since v1.30.1).

This module is the crewaimeat SEND side: deterministic helpers + CrewAI tools, with a safety gate.
The tools are shell-callable (v1.30.1), so everything rides the existing tunnel via `_aimeat_call`
(no REST glue) — except a file upload, which must stay binary (presigned PUT, never base64 over MCP).

INBOUND (a DM -> a crew) is the daemon's `dm.inbound` tunnel-push drain (Phase 2, in aimeat-crewai):
the node pushes a `{type:"deliver", kind:"dm.inbound", payload:{message_id, conversation_id, from, ...}}`
frame on the SAME channel as `task_assigned`/`workspace.record`; the daemon builds a crew from the DM
and hands the result back through `dm_reply` here. No poller — idle-quiet is preserved.

SAFETY — the first-contact gate (AIMEAT gates strangers into "requests"; we add an owner gate on top):
  - A reply IN a thread / to a requester is already consented -> `dm_reply` auto-sends.
  - A NEW outbound DM to a stranger -> `dm_initiate` is OWNER-GATED: it asks the agent's owner (a
    dashboard message) to approve and only sends on approve=True. crewaimeat never cold-DMs on its own.

Scopes: the agent identity needs `messages:send` + `messages:read` (both in the `coordinator` profile;
task-runner agents need an explicit grant at device-auth). See CLAUDE.md "Federated DM".
"""

from __future__ import annotations

import mimetypes
import os
import sys

import requests

from crewaimeat.aimeat_crew import _aimeat_call, _serve_api
from crewaimeat.generator_tool import _discover_owner, _token

# How AIMEAT renders an attachment by media kind (the recipient's inbox: image thumbnails, a/v players,
# a doc viewer, or a plain download). Map a MIME prefix -> the `kind` field the send contract wants.
_KIND_BY_PREFIX = {"image/": "image", "audio/": "audio", "video/": "video"}


def _kind_for(mime: str) -> str:
    for prefix, kind in _KIND_BY_PREFIX.items():
        if mime.startswith(prefix):
            return kind
    return "file"


# ── low-level: the three federated-inbox tools (shell-callable since v1.30.1) ──
def dm_inbox(agent: str, *, per_page: int = 20, page: int = 1) -> dict:
    """DMs addressed to THIS agent, newest first (aimeat_dm_inbox)."""
    return _aimeat_call(agent, "aimeat_dm_inbox", {"per_page": per_page, "page": page}) or {}


def dm_thread(agent: str, conversation_id: str, *, per_page: int = 50, page: int = 1) -> dict:
    """Full thread (this agent's sent + received), oldest-first (aimeat_dm_thread)."""
    payload = {"conversation_id": conversation_id, "per_page": per_page, "page": page}
    return _aimeat_call(agent, "aimeat_dm_thread", payload) or {}


def dm_send(
    agent: str,
    to: str,
    body: str | None = None,
    *,
    reply_to: str | None = None,
    subject: str | None = None,
    conversation_id: str | None = None,
    attachments: list[dict] | None = None,
) -> dict | None:
    """Low-level send (aimeat_dm_send). Prefer `dm_reply` (consented) / `dm_initiate` (owner-gated) —
    they carry the safety policy. `to` = "owner@node" | "agent#owner@node" | "eco:app#owner@node"."""
    if not body and not attachments:
        raise ValueError("dm_send needs a body or attachments")
    payload: dict = {"to": to}
    if body:
        payload["body"] = body
    if reply_to:
        payload["reply_to"] = reply_to
    if subject:
        payload["subject"] = subject
    if conversation_id:
        payload["conversation_id"] = conversation_id
    if attachments:
        payload["attachments"] = attachments
    return _aimeat_call(agent, "aimeat_dm_send", payload)


# ── the SAFE send API ──
def dm_reply(
    agent: str,
    to: str,
    body: str,
    *,
    conversation_id: str | None = None,
    reply_to: str | None = None,
    attachments: list[dict] | None = None,
) -> dict | None:
    """Reply WITHIN an existing thread / to a requester — already consented, so it auto-sends. Requires
    thread context (conversation_id or reply_to) so it can't be repurposed to cold-DM a stranger. This is
    the deliverable hand-back path: reply to the DM that triggered a crew with the result + attachments."""
    if not (conversation_id or reply_to):
        raise ValueError(
            "dm_reply requires conversation_id or reply_to (in-thread only). "
            "Use dm_initiate for a NEW contact (owner-gated)."
        )
    return dm_send(agent, to, body, conversation_id=conversation_id, reply_to=reply_to, attachments=attachments)


def dm_initiate(
    agent: str,
    to: str,
    body: str,
    *,
    subject: str,
    approve: bool = False,
    attachments: list[dict] | None = None,
) -> dict:
    """Open a NEW topic thread to someone the agent hasn't talked to. AIMEAT already gates first contact
    (it lands in the recipient's REQUESTS until they accept); crewaimeat ADDS an owner gate: unless
    approve=True this does NOT send — it asks the agent's OWNER to approve (a dashboard message) and
    returns {"status":"gated"}. Pass approve=True (after a human says yes) to actually send."""
    if not approve:
        owner = _discover_owner(agent)
        prompt = (
            f"[dm-approval] {agent} wants to OPEN a new federated DM to '{to}'.\n"
            f"subject: {subject}\n\n{body[:500]}\n\n"
            f"Approve? Re-run with approve=True, or reply here to decline."
        )
        _aimeat_call(agent, "aimeat_message_send", {"content": prompt})  # -> the agent's OWN owner inbox
        return {"status": "gated", "to": to, "subject": subject, "owner": owner}
    res = dm_send(agent, to, body, subject=subject, attachments=attachments)
    return {"status": "sent" if res else "failed", "to": to, "subject": subject, "result": res}


# ── attachments: presigned upload -> the attachment dict the send contract wants ──
def dm_attach(agent: str, path: str, *, name: str | None = None, mime: str | None = None) -> dict | None:
    """Upload a local file the PRESIGNED way (binary stays binary — never base64 over MCP/the tunnel) and
    return the attachment dict for dm_send/dm_reply: {storage_key, mime, kind, size, name}. Up to 20 per
    message. NB visibility=public (unguessable key) so a cross-owner/cross-node recipient can fetch the
    deliverable — open question with the AIMEAT dev whether a message-scoped visibility should replace it.

    Mirrors image_contract._upload_public: POST /v1/storage {mode:'presigned'} -> PUT raw bytes."""
    if not os.path.isfile(path):
        print(f"[{agent}] dm_attach: no such file: {path}", file=sys.stderr)
        return None
    fname = name or os.path.basename(path)
    ctype = mime or mimetypes.guess_type(fname)[0] or "application/octet-stream"
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        print(f"[{agent}] dm_attach read {path} failed: {exc!r}", file=sys.stderr)
        return None
    key = f"dm/{agent}/{fname}"
    presign = {"key": key, "mime_type": ctype, "visibility": "public", "mode": "presigned"}
    try:
        api = _serve_api()
        if api is not None:
            base, session = api
            r = session.post(f"{base}/v1/storage", json=presign, headers={"X-Aimeat-Agent": agent}, timeout=60)
        else:
            tok, url = _token(agent, _discover_owner(agent))
            if not tok or not url:
                return None
            r = requests.post(
                f"{url.rstrip('/')}/v1/storage", json=presign, headers={"Authorization": f"Bearer {tok}"}, timeout=60
            )
        upload_url = ((r.json() or {}).get("data") or {}).get("upload_url") if r.status_code == 200 else None
        if not upload_url:
            print(f"[{agent}] dm_attach presign {key} failed: HTTP {r.status_code} {r.text[:160]}", file=sys.stderr)
            return None
        put = requests.put(upload_url, data=data, headers={"Content-Type": ctype}, timeout=300)
        if put.status_code not in (200, 201):
            print(f"[{agent}] dm_attach PUT {key} failed: HTTP {put.status_code}", file=sys.stderr)
            return None
    except Exception as exc:  # noqa: BLE001
        print(f"[{agent}] dm_attach {key} failed: {exc!r}", file=sys.stderr)
        return None
    return {"storage_key": key, "mime": ctype, "kind": _kind_for(ctype), "size": len(data), "name": fname}


# ── CrewAI tools: let an LLM crew SEND a reply / CHECK its inbox during a run ──
def make_dm_tools(agent: str) -> list:
    """Tools an LLM crew can use mid-run. Only the SAFE surface is exposed: reply-in-thread + read.
    Opening a NEW contact stays the deterministic owner-gated `dm_initiate` (not a crew tool), so an
    LLM can never decide to cold-DM a stranger on its own."""
    from crewai.tools import tool

    @tool("check_federated_inbox")
    def check_federated_inbox() -> str:
        """List recent federated DMs addressed to me (sender, subject, preview, conversation_id)."""
        data = dm_inbox(agent, per_page=20)
        msgs = (data.get("messages") if isinstance(data, dict) else None) or []
        if not msgs:
            return "No federated DMs."
        lines = [
            f"- [{m.get('conversation_id', '?')}] from {m.get('from', '?')}: "
            f"{(m.get('subject') or '')} — {str(m.get('preview') or m.get('body') or '')[:120]}"
            for m in msgs
        ]
        return "\n".join(lines)

    @tool("reply_federated_dm")
    def reply_federated_dm(conversation_id: str, to: str, body: str) -> str:
        """Reply to a federated DM IN its thread (conversation_id + the original sender `to`). Consented;
        sends immediately. Use this to hand a deliverable or answer back. NOT for new contacts."""
        res = dm_reply(agent, to, body, conversation_id=conversation_id)
        return "sent" if res else "send failed"

    return [check_federated_inbox, reply_federated_dm]

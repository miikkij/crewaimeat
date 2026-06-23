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

INBOUND (a DM -> a crew): when a DM arrives the node pushes a lightweight `dm.inbound` wake over the
connect tunnel, which the connector surfaces on its loopback `/local/dm/next` long-poll (v1.30.2, a
mirror of `/local/records/next`). `run_dm_listener(agent, responder)` drains it (event-driven — it parks
on the wake, zero node calls while idle), runs the responder, and hands the result back via `dm_reply`.
Run it standalone (`scripts/dm_listener.py`) or in a background thread next to a coordinator agent; when
aimeat-crewai's `run_crew_daemon` grows an `on_dm` drain this moves into the daemon. The wake carries a
PREVIEW (aligned shape {id, conversationId, subject, senderGhii, preview, attachments, createdAt}); a
responder needing the full body/attachments fetches them with `dm_thread(agent, conversationId)`.

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


def _inbound_fields(m: dict) -> tuple:
    """Tolerant extraction from a received DM -> (id, conversation_id, sender, body, subject). Handles
    BOTH shapes: the inbox-list response (camelCase + *Ghii: id/conversationId/senderGhii/body) and the
    `dm.inbound` push payload (snake_case: message_id/conversation_id/from/preview)."""
    mid = m.get("id") or m.get("message_id")
    conv = m.get("conversationId") or m.get("conversation_id")
    sender = m.get("senderGhii") or m.get("from") or m.get("sender")
    body = m.get("body") or m.get("preview") or ""
    subject = m.get("subject")
    return mid, conv, sender, body, subject


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


# ── interactive: federated AskUserQuestion (aimeat-crewai>=0.9.0, node>=1.31) ──
# NB aimeat_dm_ask is NOT shell-callable — the deterministic path is the aimeat_crewai helpers
# (serve_client + ask + read_answers), the same loopback path as `aimeat connect call`. The node schema
# (aimeat/src/models/message-schemas.ts) is authoritative, so we delegate question-building to them too.
def build_question(
    qid: str,
    header: str,
    prompt: str,
    options,
    *,
    multi_select: bool = False,
    allow_other: bool = True,
    required: bool = True,
) -> dict:
    """One structured question for dm_ask. `options` = (id, label) tuples OR plain strings. Renders as
    radios (single-select) / checkboxes (multiSelect) + an 'Other' freeform. Delegates to the package
    builder so the shape always matches the node schema."""
    from aimeat_crewai import build_question as _bq

    return _bq(
        qid, prompt, list(options), header=header, multi_select=multi_select, allow_other=allow_other, required=required
    )


def dm_ask(
    agent: str,
    to: str,
    questions: list[dict],
    *,
    body: str | None = None,
    subject: str | None = None,
    conversation_id: str | None = None,
    submit_label: str | None = None,
) -> dict | None:
    """Send a STRUCTURED question (renders as a form in the recipient's inbox) to map intent / clarify
    BEFORE acting. The answer returns as a normal DM whose on_dm event has interactive=="answers"; read
    the picks with dm_read_answers(agent, conversation_id). Returns {message_id, conversation_id} or None."""
    from aimeat_crewai import ask as _ask
    from aimeat_crewai import serve_client as _serve_client

    try:
        api = _serve_client(agent)
        return _ask(
            api, to, questions, body=body, subject=subject, conversation_id=conversation_id, submit_label=submit_label
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[{agent}] dm_ask failed: {exc!r}", file=sys.stderr)
        return None


def dm_read_answers(agent: str, conversation_id: str) -> dict:
    """The latest structured ANSWERS in a thread -> {qId: {"selected":[ids], "other": str|None}} (or {})."""
    from aimeat_crewai import read_answers as _read_answers
    from aimeat_crewai import serve_client as _serve_client

    try:
        api = _serve_client(agent)
        r = _read_answers(api, conversation_id)
        return (r.get("answers") or {}) if isinstance(r, dict) else {}
    except Exception as exc:  # noqa: BLE001
        print(f"[{agent}] dm_read_answers failed: {exc!r}", file=sys.stderr)
        return {}


# ── attachments: presigned upload -> the attachment dict the send contract wants ──
def dm_attach(agent: str, path: str, *, name: str | None = None, mime: str | None = None) -> dict | None:
    """Upload a local file the PRESIGNED way (binary stays binary — never base64 over MCP/the tunnel) and
    return the attachment dict for dm_send/dm_reply: {storage_key, mime, kind, size, name}. Up to 20 per
    message. Uploads PRIVATE (v1.30.2): the node grants the recipient read per-message via a signed
    federation storage grant on accept (in-thread hand-back to an accepted contact = automatic) — no
    public exposure, no "message" visibility value needed.

    Mirrors image_contract._upload_public's flow: POST /v1/storage {mode:'presigned'} -> PUT raw bytes."""
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
    return dm_attach_bytes(agent, data, name=fname, mime=ctype)


def dm_attach_bytes(agent: str, data: bytes, *, name: str, mime: str) -> dict | None:
    """Attach IN-MEMORY bytes (a found image, a fetched file, a generated image) the presigned PRIVATE way —
    binary never rides MCP. Returns the {storage_key, mime, kind, size, name} attachment dict, or None."""
    if not data:
        return None
    key = f"dm/{agent}/{name}"
    presign = {"key": key, "mime_type": mime, "visibility": "private", "mode": "presigned"}
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
        put = requests.put(upload_url, data=data, headers={"Content-Type": mime}, timeout=300)
        if put.status_code not in (200, 201):
            print(f"[{agent}] dm_attach PUT {key} failed: HTTP {put.status_code}", file=sys.stderr)
            return None
    except Exception as exc:  # noqa: BLE001
        print(f"[{agent}] dm_attach {key} failed: {exc!r}", file=sys.stderr)
        return None
    return {"storage_key": key, "mime": mime, "kind": _kind_for(mime), "size": len(data), "name": name}


# ── INBOUND: a DM -> a crew -> hand back (Phase 2 handler) ──
def process_dm_inbox(agent: str, responder, *, seen: set | None = None, max_items: int = 5) -> dict:
    """Read this agent's federated inbox and, for each NEW DM, run `responder(dm) -> reply_text` and hand
    the result back IN-THREAD via `dm_reply`. This is the reusable Phase-2 handler:

      - The daemon's future `dm.inbound` PUSH drain calls this on a wake (no poll) — `on_dm` in
        aimeat-crewai mirrors the `on_record` drain; pass `seen` (a persistent set) for cross-call dedup.
      - The test harness (`scripts/dm_inbound_test.py`) calls it directly so the read->crew->handback loop
        is exercisable today, before the package push lands.

    `responder(dm)` is where a real crew runs (e.g. build a crew from `dm['preview']`/the thread and return
    its deliverable text); return "" to stay silent. Dedups on message id via `seen`. Returns counts."""
    seen = seen if seen is not None else set()
    data = dm_inbox(agent, per_page=max_items)
    msgs = (data.get("messages") if isinstance(data, dict) else None) or []
    replied = skipped = failed = 0
    for m in msgs:
        mid, conv, sender, _body, _subject = _inbound_fields(m)
        if not mid or mid in seen or not sender or not conv:
            skipped += 1
            continue
        seen.add(mid)  # mark BEFORE running so a crash can't re-trigger the same DM (runaway-safe)
        try:
            reply_text = responder(m)
        except Exception as exc:  # noqa: BLE001
            print(f"[{agent}] dm responder failed for {mid}: {exc!r}", file=sys.stderr)
            failed += 1
            continue
        if not reply_text:
            continue
        res = dm_reply(agent, sender, reply_text, conversation_id=conv)
        if res:
            replied += 1
        else:
            failed += 1
    return {"seen": len(msgs), "replied": replied, "skipped": skipped, "failed": failed}


# ── INBOUND trigger: drain the loopback DM queue (v1.30.2) — event-driven, no poller ──
def dm_drain_next(agent: str, *, wait_ms: int = 5000) -> dict | None:
    """Long-poll the serve daemon's loopback DM queue (GET /local/dm/next — the mirror of
    /local/records/next, v1.30.2+). Blocks up to wait_ms for a pushed `dm.inbound` wake, returns the event
    {id, conversationId, subject, senderGhii, preview, attachments, createdAt} or None. Loopback only — NOT
    a node call — so an idle listener parks here and makes zero node traffic until the node pushes a DM."""
    api = _serve_api()
    if api is None:
        return None
    base, session = api
    try:
        r = session.get(f"{base}/local/dm/next", params={"wait": wait_ms, "agent": agent}, timeout=wait_ms / 1000 + 10)
        if r.status_code != 200:
            return None
        return (r.json() or {}).get("data", {}).get("event")
    except Exception:  # noqa: BLE001
        return None


def handle_dm_event(agent: str, event: dict, responder, *, seen: set | None = None) -> bool:
    """on_dm handler — process ONE pushed `dm.inbound` wake: dedup, run `responder(event) -> reply_text`,
    hand back in-thread via dm_reply. This is what you pass to `run_crew_daemon(on_dm=...)` (aimeat-crewai
    >=0.8.0) — the daemon parks its idle wait on /local/dm/next and calls this on each wake (event-based,
    idle-quiet). Marks `seen` BEFORE running (runaway-safe). Returns True if it replied. The wake carries a
    PREVIEW; a responder needing the full body/attachments fetches them with dm_thread(agent, conversationId)."""
    seen = seen if seen is not None else set()
    mid, conv, sender, _body, _subject = _inbound_fields(event)
    if not mid or mid in seen or not sender or not conv:
        return False
    if sender.startswith(f"{agent}#"):
        return False  # never reply to our OWN message (self-DM) — the reply would re-trigger -> loop
    seen.add(mid)  # mark BEFORE running (runaway-safe)
    try:
        result = responder(event)
    except Exception as exc:  # noqa: BLE001
        print(f"[{agent}] on_dm responder failed for {mid}: {exc!r}", file=sys.stderr)
        return False
    # The responder may return plain reply text, or {"text": ..., "attachments": [...]} to hand back files.
    if isinstance(result, dict):
        reply_text, attachments = result.get("text") or "", result.get("attachments")
    else:
        reply_text, attachments = result or "", None
    if not reply_text and not attachments:
        return False
    return bool(dm_reply(agent, sender, reply_text, conversation_id=conv, attachments=attachments))


def run_dm_listener(agent: str, responder, *, stop=None, seen: set | None = None, wait_ms: int = 5000) -> None:
    """Standalone version of the inbound trigger (when not using the daemon's on_dm): drain
    `/local/dm/next` forever (long-poll) and hand each NEW DM through `handle_dm_event`. Event-driven (parks
    on the loopback wake), so idle = zero node calls. Run it in a background thread next to a coordinator
    agent, or standalone (scripts/dm_listener.py). `stop` is an optional threading.Event to end the loop."""
    seen = seen if seen is not None else set()
    while not (stop is not None and stop.is_set()):
        event = dm_drain_next(agent, wait_ms=wait_ms)
        if event:
            handle_dm_event(agent, event, responder, seen=seen)


def start_dm_listener_thread(agent: str, responder, *, wait_ms: int = 5000):
    """Start run_dm_listener in a background DAEMON thread and return it. A STANDALONE alternative to the
    daemon's native listen_for="dms"/on_dm (the production path since aimeat-crewai>=0.8.1) — e.g. to serve
    DMs for an agent that isn't a run_crew_daemon crew, or to keep DM handling out of the daemon loop. The
    listener OWNS the /local/dm/next queue, so DON'T also put "dms" in that agent's listen_for (the daemon
    would race it for the same queue). For a normal crew, prefer native on_dm=dm.handle_dm_event(...)."""
    import threading

    t = threading.Thread(
        target=run_dm_listener,
        args=(agent, responder),
        kwargs={"wait_ms": wait_ms},
        daemon=True,
        name=f"dm-listener-{agent}",
    )
    t.start()
    return t


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
        lines = []
        for m in msgs:
            _id, conv, sender, body, subject = _inbound_fields(m)
            lines.append(f"- [{conv or '?'}] from {sender or '?'}: {subject or ''} — {str(body)[:120]}")
        return "\n".join(lines)

    @tool("reply_federated_dm")
    def reply_federated_dm(conversation_id: str, to: str, body: str) -> str:
        """Reply to a federated DM IN its thread (conversation_id + the original sender `to`). Consented;
        sends immediately. Use this to hand a deliverable or answer back. NOT for new contacts."""
        res = dm_reply(agent, to, body, conversation_id=conversation_id)
        return "sent" if res else "send failed"

    return [check_federated_inbox, reply_federated_dm]

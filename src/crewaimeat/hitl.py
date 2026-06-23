"""Human-in-the-loop gates — reusable approval / choice / escalation over the federated inbox.

A gate is a STRUCTURED question (AIMEAT interactive `dm_ask`) plus a remembered PENDING action: the agent
asks the owner before an outward/irreversible step, parks what it intended to do, and resumes only when the
answer arrives (a separate DM whose on_dm event has interactive=="answers"). Same async-correlation shape
as the orchestrator's delegation relay — the pending lives in session_store keyed by the conversation, and
`resolve(event)` matches the answer back to it.

Three gates, one mechanism:
  - ask_approval : a yes/no before acting (publish, send, delegate, save) -> resolve -> {approved: bool}.
  - ask_choice   : present N options, the human picks one/some -> resolve -> {picked: [option dicts]}.
  - escalate     : ask the owner to DECIDE when confidence is low (ask_choice with decision options).

One pending gate per conversation (the latest question wins). `resolve` only fires for an answer that
actually carries this gate's question id, so an unrelated answer in the thread leaves the gate intact.
"""

from __future__ import annotations

from crewaimeat import dm, session_store

_PENDING_KEY = "hitl"


def _set_pending(agent: str, conv: str, pending: dict) -> None:
    session_store.session_set(agent, conv, _PENDING_KEY, pending)


def _get_pending(agent: str, conv: str) -> dict | None:
    return session_store.session_get(agent, conv, _PENDING_KEY)


def _clear_pending(agent: str, conv: str) -> None:
    session_store.session_clear(agent, conv, _PENDING_KEY)


def ask_approval(
    agent: str,
    to: str,
    conv: str,
    *,
    summary: str,
    action_id: str = "",
    payload=None,
    yes: str = "Yes, go ahead",
    no: str = "No, cancel",
    qid: str = "hitl_approve",
    body: str | None = None,
) -> bool:
    """Ask the owner to APPROVE an action before doing it. `summary` describes the action; `payload` is
    whatever the caller needs to carry out the action on approval (returned by resolve). Returns True if
    the question was sent. Resolve the answer in on_dm with resolve(agent, event)."""
    q = dm.build_question(qid, "Approve?", summary, [("yes", yes), ("no", no)], multi_select=False, allow_other=False)
    res = dm.dm_ask(agent, to, [q], body=body or summary, conversation_id=conv)
    _set_pending(agent, conv, {"kind": "approval", "qid": qid, "action_id": action_id, "payload": payload})
    return bool(res)


def ask_choice(
    agent: str,
    to: str,
    conv: str,
    *,
    prompt: str,
    options: list[dict],
    action_id: str = "",
    multi: bool = False,
    payload=None,
    qid: str = "hitl_choice",
    allow_other: bool = False,
    body: str | None = None,
) -> bool:
    """Present N OPTIONS and let the owner pick. `options` = [{"id","label", ...any data}]; the picked
    option dicts come back from resolve. `multi`=True for checkboxes. Generalises the offer->pick pattern."""
    opts = [(o["id"], o.get("label", o["id"])) for o in options if o.get("id")]
    q = dm.build_question(qid, "Pick", prompt, opts, multi_select=multi, allow_other=allow_other)
    res = dm.dm_ask(agent, to, [q], body=body or prompt, conversation_id=conv)
    _set_pending(
        agent,
        conv,
        {
            "kind": "choice",
            "qid": qid,
            "action_id": action_id,
            "options": {o["id"]: o for o in options if o.get("id")},
            "multi": multi,
            "payload": payload,
        },
    )
    return bool(res)


def escalate(
    agent: str,
    to: str,
    conv: str,
    *,
    question: str,
    options: list[dict],
    action_id: str = "",
    payload=None,
    qid: str = "hitl_escalate",
) -> bool:
    """Escalate a low-confidence decision to the owner: ask them to choose how to proceed. Thin wrapper
    over ask_choice with a decision framing."""
    return ask_choice(agent, to, conv, prompt=question, options=options, action_id=action_id, payload=payload, qid=qid)


def resolve(agent: str, event: dict) -> dict | None:
    """Match an on_dm 'answers' event to this conversation's pending gate and return the resolution,
    clearing the gate. Returns None when there is no pending gate OR the answer doesn't carry this gate's
    question (so an unrelated answer leaves the gate intact). Resolution shapes:
      approval -> {"kind":"approval","action_id","approved":bool,"payload"}
      choice   -> {"kind":"choice","action_id","picked":[option dicts],"other":str|None,"payload"}"""
    _mid, conv, _sender, _preview, _subject = dm._inbound_fields(event)
    if not conv:
        return None
    pending = _get_pending(agent, conv)
    if not pending:
        return None
    answers = dm.dm_answers_from_event(agent, event) or {}
    if pending["qid"] not in answers:
        return None  # this answer is for a different question — don't consume our gate
    ans = answers.get(pending["qid"]) or {}
    selected = ans.get("selected") or []
    other = ans.get("other")
    _clear_pending(agent, conv)
    if pending["kind"] == "approval":
        return {
            "kind": "approval",
            "action_id": pending.get("action_id", ""),
            "approved": "yes" in selected,
            "payload": pending.get("payload"),
        }
    picked = [pending["options"][sid] for sid in selected if sid in pending.get("options", {})]
    return {
        "kind": "choice",
        "action_id": pending.get("action_id", ""),
        "picked": picked,
        "other": other,
        "payload": pending.get("payload"),
    }

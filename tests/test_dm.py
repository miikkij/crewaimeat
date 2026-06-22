"""dm.py — the federated-inbox send helpers + the first-contact safety gate. Pure logic, mocked node."""

from __future__ import annotations

import pytest

from crewaimeat import dm


@pytest.fixture
def calls(monkeypatch):
    """Record every _aimeat_call (agent, tool, payload) and stub a success envelope."""
    log: list[tuple] = []

    def fake_call(agent, tool, payload, *a, **k):
        log.append((agent, tool, payload))
        return {"ok": True, "id": "m1"}

    monkeypatch.setattr(dm, "_aimeat_call", fake_call)
    monkeypatch.setattr(dm, "_discover_owner", lambda agent: "tester")
    return log


def test_dm_send_requires_body_or_attachment(calls):
    with pytest.raises(ValueError):
        dm.dm_send("a", "x@n")  # neither body nor attachments


def test_dm_reply_requires_thread_context(calls):
    # No conversation_id / reply_to -> refuse (can't be used to cold-DM a stranger).
    with pytest.raises(ValueError):
        dm.dm_reply("a", "x@n", "hello")
    assert calls == []  # nothing sent


def test_dm_reply_in_thread_sends(calls):
    dm.dm_reply("img", "alice@n", "here is your moodboard", conversation_id="c1")
    assert len(calls) == 1
    agent, tool, payload = calls[0]
    assert tool == "aimeat_dm_send"
    assert payload["conversation_id"] == "c1"
    assert payload["to"] == "alice@n"
    assert payload["body"] == "here is your moodboard"


def test_dm_initiate_gated_by_default(calls):
    # A NEW contact must NOT send a DM; it asks the OWNER to approve (a dashboard message).
    res = dm.dm_initiate("a", "stranger@n", "let's collaborate", subject="Project X")
    assert res["status"] == "gated"
    assert len(calls) == 1
    _agent, tool, payload = calls[0]
    assert tool == "aimeat_message_send"  # owner approval prompt, NOT aimeat_dm_send
    assert "stranger@n" in payload["content"]


def test_dm_initiate_approved_sends_dm(calls):
    res = dm.dm_initiate("a", "stranger@n", "hi", subject="Project X", approve=True)
    assert res["status"] == "sent"
    assert len(calls) == 1
    _agent, tool, payload = calls[0]
    assert tool == "aimeat_dm_send"
    assert payload["subject"] == "Project X"


def test_process_dm_inbox_replies_and_dedups(monkeypatch):
    """Inbound handler: replies in-thread to a new DM, dedups via `seen`, skips DMs missing coords."""
    inbox = {
        "messages": [
            {"message_id": "m1", "conversation_id": "c1", "from": "alice@n", "preview": "make a logo"},
            {"message_id": "m2", "conversation_id": "c2", "from": "bob@n", "preview": "status?"},
            {"message_id": "m3", "from": "carol@n", "preview": "no conversation_id -> skip"},
        ]
    }
    sent: list[tuple] = []
    monkeypatch.setattr(dm, "dm_inbox", lambda agent, **k: inbox)
    monkeypatch.setattr(dm, "dm_reply", lambda agent, to, body, **k: sent.append((to, body, k)) or {"ok": True})

    seen: set = set()
    r1 = dm.process_dm_inbox("a", lambda m: f"re: {m['preview']}", seen=seen)
    assert r1["replied"] == 2 and r1["skipped"] == 1  # m1+m2 replied, m3 (no conv) skipped
    assert {to for to, _b, _k in sent} == {"alice@n", "bob@n"}
    assert all(k.get("conversation_id") for _to, _b, k in sent)  # threaded

    # Re-run: m1/m2 already in `seen` -> no new replies (runaway-safe).
    r2 = dm.process_dm_inbox("a", lambda m: "again", seen=seen)
    assert r2["replied"] == 0 and len(sent) == 2


def test_process_dm_inbox_silent_responder_sends_nothing(monkeypatch):
    monkeypatch.setattr(
        dm,
        "dm_inbox",
        lambda agent, **k: {
            "messages": [{"message_id": "m1", "conversation_id": "c1", "from": "x@n", "preview": "hi"}]
        },
    )
    sent = []
    monkeypatch.setattr(dm, "dm_reply", lambda *a, **k: sent.append(a) or {"ok": True})
    r = dm.process_dm_inbox("a", lambda m: "")  # responder returns "" -> stay silent
    assert r["replied"] == 0 and sent == []


def test_handle_dm_event_replies_and_dedups(monkeypatch):
    """on_dm handler: reply in-thread to a new event, dedup via seen, skip events missing coords."""
    sent: list[tuple] = []
    monkeypatch.setattr(dm, "dm_reply", lambda agent, to, body, **k: sent.append((to, body, k)) or {"ok": True})
    ev = {"id": "m1", "conversationId": "c1", "senderGhii": "alice@n", "preview": "hi"}
    seen: set = set()
    assert dm.handle_dm_event("x", ev, lambda e: "yo", seen=seen) is True
    assert sent[0][0] == "alice@n" and sent[0][2].get("conversation_id") == "c1"
    assert dm.handle_dm_event("x", ev, lambda e: "yo", seen=seen) is False  # dedup
    assert len(sent) == 1
    # missing conversationId -> skip
    assert dm.handle_dm_event("x", {"id": "m2", "senderGhii": "b@n", "preview": "x"}, lambda e: "yo") is False
    # self-DM (sender is THIS agent) -> never reply (loop guard)
    self_ev = {"id": "m3", "conversationId": "c3", "senderGhii": "wm#owner@n", "preview": "x"}
    assert dm.handle_dm_event("wm", self_ev, lambda e: "yo") is False


def test_run_dm_listener_processes_then_stops(monkeypatch):
    """The production drain loop: one pushed event -> reply in-thread -> stop when the queue drains."""
    import threading

    stop = threading.Event()
    queue = [{"id": "m1", "conversationId": "c1", "senderGhii": "alice@n", "preview": "make a logo"}]

    def fake_drain(agent, **k):
        if queue:
            return queue.pop(0)
        stop.set()  # nothing left -> end the loop on the next top-of-loop check
        return None

    sent: list[tuple] = []
    monkeypatch.setattr(dm, "dm_drain_next", fake_drain)
    monkeypatch.setattr(dm, "dm_reply", lambda agent, to, body, **k: sent.append((to, body, k)) or {"ok": True})

    dm.run_dm_listener("x", lambda e: f"re: {dm._inbound_fields(e)[3]}", stop=stop)
    assert len(sent) == 1
    to, body, kw = sent[0]
    assert to == "alice@n" and kw.get("conversation_id") == "c1" and "make a logo" in body


@pytest.mark.parametrize(
    "mime,kind",
    [
        ("image/png", "image"),
        ("audio/mpeg", "audio"),
        ("video/mp4", "video"),
        ("application/pdf", "file"),
        ("text/markdown", "file"),
    ],
)
def test_kind_for(mime, kind):
    assert dm._kind_for(mime) == kind

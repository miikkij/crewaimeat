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

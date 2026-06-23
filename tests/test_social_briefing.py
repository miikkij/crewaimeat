"""social_briefing.py — deterministic helpers (config, owner addressing, kickoff, digest). Node mocked."""

from __future__ import annotations

import pytest

from crewaimeat import social_briefing as sb


@pytest.fixture(autouse=True)
def _isolate_store(monkeypatch):
    """In-memory session store so config tests don't touch the real WAL file."""
    store: dict = {}
    monkeypatch.setattr(sb.session_store, "session_get", lambda a, c, k: store.get((a, c, k)))
    monkeypatch.setattr(sb.session_store, "session_set", lambda a, c, k, v: store.__setitem__((a, c, k), v))
    return store


def test_default_topics_when_unset():
    assert sb.get_config("x")["topics"] == sb.DEFAULT_TOPICS


def test_set_topics_cleans_and_caps():
    saved = sb.set_topics("x", ["  AI agents ", "", "CrewAI", *[f"t{i}" for i in range(20)]])
    assert saved[0] == "AI agents" and "" not in saved
    assert len(saved) == 12  # capped


def test_set_topics_empty_falls_back_to_default():
    assert sb.set_topics("x", ["  ", ""]) == sb.DEFAULT_TOPICS


def test_build_kickoff_has_every_topic_and_both_sources():
    msg = sb.build_kickoff(["AI agents", "CrewAI"], "2026-06-23")
    assert "AI agents" in msg and "CrewAI" in msg
    assert "Grok / X" in msg and "Reddit" in msg
    assert "2026-06-23" in msg


def test_owner_gaii_derived_from_own(monkeypatch):
    monkeypatch.setattr(sb, "own_gaii", lambda agent=sb.AGENT_NAME: "social-briefing#happydude500001@node-1")
    assert sb.owner_gaii("social-briefing") == "happydude500001@node-1"


def test_owner_gaii_none_when_no_gaii(monkeypatch):
    monkeypatch.setattr(sb, "own_gaii", lambda agent=sb.AGENT_NAME: None)
    assert sb.owner_gaii("social-briefing") is None


def test_send_kickoff_opens_thread_and_stores_conv(monkeypatch):
    monkeypatch.setattr(sb, "owner_gaii", lambda agent=sb.AGENT_NAME: "happydude@node")
    sent: dict = {}
    monkeypatch.setattr(
        sb.dm,
        "dm_send",
        lambda a, to, body, **k: sent.update(to=to, body=body) or {"message": {"conversationId": "cv-1"}},
    )
    monkeypatch.setattr(sb.dm, "dm_reply", lambda *a, **k: pytest.fail("should open a new thread, not reply"))
    ok = sb.send_kickoff("social-briefing", "2026-06-23")
    assert ok and sent["to"] == "happydude@node"
    assert sb.get_config("social-briefing")["conversation_id"] == "cv-1"  # remembered the standing thread


def test_send_kickoff_replies_in_existing_thread(monkeypatch):
    monkeypatch.setattr(sb, "owner_gaii", lambda agent=sb.AGENT_NAME: "happydude@node")
    sb.set_config("social-briefing", conversation_id="cv-existing")
    used: dict = {}
    monkeypatch.setattr(
        sb.dm, "dm_reply", lambda a, to, body, **k: used.update(conv=k.get("conversation_id")) or {"ok": True}
    )
    monkeypatch.setattr(sb.dm, "dm_send", lambda *a, **k: pytest.fail("should reply in the standing thread"))
    assert sb.send_kickoff("social-briefing", "2026-06-23")
    assert used["conv"] == "cv-existing"


def test_write_digest_writes_dated_and_latest(monkeypatch):
    writes: list = []
    monkeypatch.setattr(sb, "_aimeat_call", lambda agent, tool, payload: writes.append(payload["key"]) or {"ok": True})
    assert sb.write_digest("social-briefing", "2026-06-23", "digest body", ["AI agents"])
    assert "social.briefing.digest.2026-06-23" in writes and "social.briefing.latest" in writes

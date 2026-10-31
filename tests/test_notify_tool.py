"""`notify`: one aimeat_notify call with email:true, and the outcome said plainly (crewaimeat.notify_tool)."""

from __future__ import annotations

import crewaimeat.notify_tool as nt


def _stub(monkeypatch, answer):
    calls = []

    def fake(agent, tool, payload, **kw):
        calls.append((agent, tool, payload, kw))
        return answer

    monkeypatch.setattr(nt, "_aimeat_call", fake)
    return calls


def test_sends_with_email_and_says_it_went(monkeypatch):
    calls = _stub(monkeypatch, {"created": True, "muted": False, "emailed": "sent"})
    out = nt.send_notification("news-agent", "Aamun katsaus", "Kolme uutista …", "/v1/home")
    assert calls[0][1] == "aimeat_notify"
    assert calls[0][2] == {"title": "Aamun katsaus", "body": "Kolme uutista …", "email": True, "link": "/v1/home"}
    assert calls[0][3].get("return_error") is True
    assert "emailed to the owner" in out


def test_a_link_off_the_node_is_dropped(monkeypatch):
    calls = _stub(monkeypatch, {"created": True, "emailed": "sent"})
    nt.send_notification("a", "t", "b", "https://example.com/x")
    assert "link" not in calls[0][2]


def test_every_mail_outcome_is_said_and_never_claims_a_mail_that_did_not_go(monkeypatch):
    for emailed in ("no_address", "mail_off", "limit", "failed"):
        _stub(monkeypatch, {"created": True, "emailed": emailed})
        out = nt.send_notification("a", "t", "b")
        assert "NOT emailed" in out or "could NOT be sent" in out, emailed


def test_muted_refused_and_silent_node(monkeypatch):
    _stub(monkeypatch, {"created": False, "muted": True, "emailed": "muted"})
    assert "muted" in nt.send_notification("a", "t", "b")
    _stub(monkeypatch, {"ok": False, "error": {"code": "SCOPE_REQUIRED", "message": "notifications:send"}})
    assert "refused" in nt.send_notification("a", "t", "b")
    _stub(monkeypatch, None)
    assert "did not answer" in nt.send_notification("a", "t", "b")


def test_empty_and_long_texts(monkeypatch):
    calls = _stub(monkeypatch, {"created": True, "emailed": "sent"})
    assert "needs a title" in nt.send_notification("a", "  ", "b")
    assert not calls
    nt.send_notification("a", "x" * 500, "y" * 20000)
    assert len(calls[0][2]["title"]) == nt.TITLE_MAX and len(calls[0][2]["body"]) == nt.BODY_MAX


def test_registered_and_proposable():
    from crewaimeat.concierge_propose import tool_scopes
    from crewaimeat.crew_def import TOOL_PURPOSES, TOOL_REGISTRY, resolve_tool

    assert "notify" in TOOL_REGISTRY and "notify" in TOOL_PURPOSES and resolve_tool("notify") is not None
    assert tool_scopes("notify") == ("notifications:send",)

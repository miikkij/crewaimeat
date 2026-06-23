"""hitl.py — approval / choice / escalation gates. Node + store mocked (pure logic)."""

from __future__ import annotations

import pytest

from crewaimeat import hitl


@pytest.fixture(autouse=True)
def _store(monkeypatch):
    store: dict = {}
    monkeypatch.setattr(hitl.session_store, "session_set", lambda a, c, k, v: store.__setitem__((a, c, k), v))
    monkeypatch.setattr(hitl.session_store, "session_get", lambda a, c, k: store.get((a, c, k)))
    monkeypatch.setattr(hitl.session_store, "session_clear", lambda a, c, k: store.pop((a, c, k), None))
    return store


@pytest.fixture
def asked(monkeypatch):
    """Capture dm_ask and stub a success; build_question passthrough."""
    sent: list = []
    monkeypatch.setattr(hitl.dm, "build_question", lambda qid, *a, **k: {"id": qid})
    monkeypatch.setattr(hitl.dm, "dm_ask", lambda agent, to, qs, **k: sent.append((to, qs, k)) or {"ok": True})
    return sent


def _event(conv="c1", mid="m1"):
    return {"id": mid, "conversationId": conv, "senderGhii": "owner@n", "interactive": "answers"}


def test_ask_approval_sets_pending(asked, _store):
    ok = hitl.ask_approval("a", "owner@n", "c1", summary="Publish?", action_id="pub", payload={"x": 1})
    assert ok and ("a", "c1", "hitl") in _store
    assert _store[("a", "c1", "hitl")]["kind"] == "approval"


def test_resolve_approval_yes(monkeypatch, asked, _store):
    hitl.ask_approval("a", "owner@n", "c1", summary="Publish?", action_id="pub", payload={"x": 1})
    monkeypatch.setattr(hitl.dm, "dm_answers_from_event", lambda a, e: {"hitl_approve": {"selected": ["yes"]}})
    res = hitl.resolve("a", _event())
    assert res["kind"] == "approval" and res["approved"] is True and res["payload"] == {"x": 1}
    assert ("a", "c1", "hitl") not in _store  # cleared


def test_resolve_approval_no(monkeypatch, asked):
    hitl.ask_approval("a", "owner@n", "c1", summary="Publish?", action_id="pub")
    monkeypatch.setattr(hitl.dm, "dm_answers_from_event", lambda a, e: {"hitl_approve": {"selected": ["no"]}})
    res = hitl.resolve("a", _event())
    assert res["approved"] is False


def test_resolve_choice_returns_picked_option_dicts(monkeypatch, asked):
    opts = [{"id": "o1", "label": "One", "data": 1}, {"id": "o2", "label": "Two", "data": 2}]
    hitl.ask_choice("a", "owner@n", "c1", prompt="Pick", options=opts, action_id="ch", multi=True)
    monkeypatch.setattr(hitl.dm, "dm_answers_from_event", lambda a, e: {"hitl_choice": {"selected": ["o2"]}})
    res = hitl.resolve("a", _event())
    assert res["kind"] == "choice" and res["picked"] == [{"id": "o2", "label": "Two", "data": 2}]


def test_resolve_ignores_unrelated_answer(monkeypatch, asked, _store):
    hitl.ask_approval("a", "owner@n", "c1", summary="Publish?")
    # an answer for a DIFFERENT question (e.g. a doc pick) must not consume the gate
    monkeypatch.setattr(hitl.dm, "dm_answers_from_event", lambda a, e: {"pick_docs": {"selected": ["d1"]}})
    assert hitl.resolve("a", _event()) is None
    assert ("a", "c1", "hitl") in _store  # gate left intact


def test_resolve_none_when_no_pending(monkeypatch):
    monkeypatch.setattr(hitl.dm, "dm_answers_from_event", lambda a, e: {"hitl_approve": {"selected": ["yes"]}})
    assert hitl.resolve("a", _event()) is None


def test_escalate_is_a_choice_gate(asked, _store):
    hitl.escalate("a", "owner@n", "c1", question="Which way?", options=[{"id": "x", "label": "X"}], action_id="esc")
    assert _store[("a", "c1", "hitl")]["kind"] == "choice"
    assert _store[("a", "c1", "hitl")]["qid"] == "hitl_escalate"

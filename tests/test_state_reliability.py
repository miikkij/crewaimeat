"""Behavior across delayed replies, restarts, expiry, races and database failures."""

from __future__ import annotations

import importlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from crewaimeat import hitl
from crewaimeat import session_store as state
from crewaimeat._sqlite import database
from crewaimeat.agency import chat_store


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))


@pytest.fixture
def questions(monkeypatch):
    sent = []

    def send(agent, to, questions, **kwargs):
        sent.extend(questions)
        return {"ok": True}

    monkeypatch.setattr(hitl.dm, "dm_ask", send)
    return sent


def answer(question, selected=None):
    return {
        "id": "reply",
        "conversationId": "conversation",
        "senderGhii": "owner@n",
        "interactive": {"answers": {question["id"]: {"selected": selected or ["yes"]}}},
    }


def ask(action):
    return hitl.ask_approval("agent", "owner@n", "conversation", summary=f"Approve {action}?", action_id=action)


def test_delayed_approval_cannot_authorize_new_action(questions):
    ask("A")
    ask("B")
    assert questions[0]["id"] != questions[1]["id"]
    assert hitl.resolve("agent", answer(questions[0])) is None
    result = hitl.resolve("agent", answer(questions[1]))
    assert result["action_id"] == "B" and result["approved"]


def test_approval_survives_reload_and_duplicate_is_consumed_once(questions):
    ask("A")
    importlib.reload(hitl)
    event = answer(questions[0])
    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(lambda _: hitl.resolve("agent", event), range(4)))
    assert sum(r is not None for r in results) == 1
    assert next(r for r in results if r)["action_id"] == "A"


def test_replacement_between_read_and_consume_is_preserved(questions, monkeypatch):
    ask("A")
    original = state.session_consume

    def raced(agent, conv, key, expected):
        ask("B")
        return original(agent, conv, key, expected)

    monkeypatch.setattr(state, "session_consume", raced)
    assert hitl.resolve("agent", answer(questions[0])) is None
    assert hitl._get_pending("agent", "conversation")["action_id"] == "B"


@pytest.mark.parametrize("result", [None, {"ok": False}])
def test_failed_question_is_not_pending(result, monkeypatch):
    monkeypatch.setattr(hitl.dm, "dm_ask", lambda *a, **kw: result)
    assert ask("A") is False
    assert hitl._get_pending("agent", "conversation") is None


def test_fast_answer_during_send_can_resolve(monkeypatch):
    resolved = []

    def send(agent, to, questions, **kwargs):
        resolved.append(hitl.resolve(agent, answer(questions[0])))
        return {"ok": True}

    monkeypatch.setattr(hitl.dm, "dm_ask", send)
    assert ask("A")
    assert resolved[0]["action_id"] == "A"
    assert hitl._get_pending("agent", "conversation") is None


def test_expired_approval_does_not_resume_without_any_later_write(questions, monkeypatch):
    monkeypatch.setattr(state.time, "time", lambda: 1_000_000)
    ask("A")
    monkeypatch.setattr(state.time, "time", lambda: 1_000_000 + state._TTL_SECONDS)
    assert hitl.resolve("agent", answer(questions[0])) is None


def test_legacy_uncorrelated_approval_requires_new_question(capsys):
    state.session_set("agent", "conversation", "hitl", {"kind": "approval", "qid": "hitl_approve"})
    assert hitl.resolve("agent", answer({"id": "hitl_approve"})) is None
    assert "ask again" in capsys.readouterr().err


def test_recent_chat_window_and_reload_with_timestamp_ties(monkeypatch):
    monkeypatch.setattr(chat_store.time, "time", lambda: 100)
    for n in range(10):
        chat_store.append("s", "user", str(n))
    importlib.reload(chat_store)
    assert [r["text"] for r in chat_store.window("s", 3)] == ["7", "8", "9"]
    assert [r["text"] for r in chat_store.history("s", 2)] == ["8", "9"]
    assert chat_store.window("s", 0) == []
    assert chat_store.history("other") == []


def test_expiry_does_not_delete_durable_or_legacy_preferences(monkeypatch):
    monkeypatch.setattr(state.time, "time", lambda: 1_000_000)
    state.session_set("agent", "c", "pending", "temporary")
    state.session_set("agent", "_briefing", "config", {"topics": ["old preference"]})
    state.preference_set("agent", "_sanomat_desk", "config", {"conversation_id": "saved"})
    monkeypatch.setattr(state.time, "time", lambda: 1_000_000 + state._TTL_SECONDS + 1)
    assert state.session_get("agent", "c", "pending") is None
    state.session_set("other", "c", "new", "write triggers pruning")
    assert state.preference_get("agent", "_briefing", "config") == {"topics": ["old preference"]}
    assert state.preference_get("agent", "_sanomat_desk", "config") == {"conversation_id": "saved"}
    assert state.preference_get("other", "_briefing", "config") is None
    with state._conn() as conn:
        assert conn.execute("SELECT count(*) FROM sessions WHERE conv='_briefing'").fetchone()[0] == 0


def test_database_rolls_back_and_closes_on_failure(tmp_path):
    path = str(tmp_path / "failure.db")

    def schema(conn):
        conn.execute("CREATE TABLE IF NOT EXISTS example(value TEXT)")

    with pytest.raises(ValueError, match="fail transaction"), database(path, schema) as conn:
        conn.execute("INSERT INTO example VALUES('uncommitted')")
        raise ValueError("fail transaction")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")
    with database(path, schema) as conn:
        assert conn.execute("SELECT count(*) FROM example").fetchone()[0] == 0
    (tmp_path / "failure.db").unlink()  # Windows catches leaked handles here.


def test_json_doctor_verdict_matches_exit_for_stale_baseline(tmp_path, capsys, monkeypatch):
    from crewaimeat.doctor import cli
    from crewaimeat.doctor.model import Report

    (tmp_path / "crews").mkdir()
    (tmp_path / "doctor-baseline.json").write_text(json.dumps({"accepted": ["fixed.rule::old"]}))
    monkeypatch.setattr(cli, "run", lambda *a, **kw: Report())
    assert cli.main(["--root", str(tmp_path), "--strict", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["verdict"] == "fail"

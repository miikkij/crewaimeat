"""Finalize marks todos done DETERMINISTICALLY (not via the LLM).

Field bug 2026-07-07 (image-maker): the deterministic completion callback always completed the task,
but todo-marking was left to the LLM finalize step, which on a slow/weak model replied 'done' without
calling aimeat_task_todo — so the task landed Done with its todo still pending (0/1). These tests pin
the fix: _make_complete_cb flips every open todo to done, BEFORE completing, while the task is active.
All deterministic, no LLM, no network.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import crewaimeat.aimeat_crew as ac


def _capture(monkeypatch, task_todos):
    """Mock _aimeat_call: aimeat_task_get returns a task with `task_todos`; everything else -> ok."""
    calls = []

    def fake(agent, tool, payload):
        calls.append((tool, payload))
        if tool == "aimeat_task_get":
            return {"task": {"id": payload["task_id"], "todos": task_todos}}
        return {"ok": True}

    monkeypatch.setattr(ac, "_aimeat_call", fake)
    return calls


def test_open_todos_are_marked_done_before_completion(monkeypatch):
    todos = [
        {"id": "todo-1", "status": "pending"},
        {"id": "todo-2", "status": "active"},
    ]
    calls = _capture(monkeypatch, todos)
    ac._make_complete_cb("image-maker", "t-1", mem_key="crews.image-maker.x.latest_output")(MagicMock())
    tools = [c[0] for c in calls]
    # both open todos flipped to done, each via its own call...
    todo_calls = [c for c in calls if c[0] == "aimeat_task_todo"]
    assert {c[1]["todo_id"] for c in todo_calls} == {"todo-1", "todo-2"}
    assert all(c[1]["status"] == "done" for c in todo_calls)
    # ...and the todo writes happen BEFORE the task is completed (todo works only on active tasks)
    assert tools.index("aimeat_task_todo") < tools.index("aimeat_task_complete")
    assert tools[-1] == "aimeat_task_complete"


def test_terminal_and_done_todos_are_left_alone(monkeypatch):
    todos = [
        {"id": "todo-1", "status": "done"},
        {"id": "todo-2", "status": "failed"},
        {"id": "todo-3", "status": "skipped"},
        {"id": "todo-4", "status": "pending"},
    ]
    calls = _capture(monkeypatch, todos)
    ac._make_complete_cb("image-maker", "t-2")(MagicMock())
    flipped = {c[1]["todo_id"] for c in calls if c[0] == "aimeat_task_todo"}
    assert flipped == {"todo-4"}  # only the still-open one


def test_no_todos_still_completes(monkeypatch):
    calls = _capture(monkeypatch, [])
    ac._make_complete_cb("image-maker", "t-3")(MagicMock())
    tools = [c[0] for c in calls]
    assert "aimeat_task_todo" not in tools
    assert "aimeat_task_complete" in tools


def test_failed_gate_does_not_mark_todos_done(monkeypatch):
    """A verify-gate FAIL fails the task — it must NOT flip todos to done."""
    import crewaimeat.author_tool as at

    at._VERIFY_VERDICTS["t-fail"] = {"verify_render": {"ok": False}}
    calls = _capture(monkeypatch, [{"id": "todo-1", "status": "pending"}])
    ac._make_complete_cb("image-maker", "t-fail", require_verify=True)(MagicMock())
    tools = [c[0] for c in calls]
    assert "aimeat_task_fail" in tools
    assert "aimeat_task_todo" not in tools and "aimeat_task_complete" not in tools
    at.reset_verify_verdicts("t-fail")

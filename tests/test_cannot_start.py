"""A runtime that cannot start fails the task it was started for, and a refused read is called refused.

Hosted place, 2026-10-02: a chat-proposed agent had only memory:write. Reading its own definition was
refused (SCOPE_DENIED memory:read), the runtime exited 1 on each wake (two runs, 11 s each), and the
customer's task stayed "active" for good with no word on it. The log said "crews.registry.<name> holds
no crew definition (got NoneType) ... Publish one" -- the wrong defect, sending the person to publish a
definition that was there. These tests hold the two places that decide it:

  the registry read tells REFUSED (the node said no) from EMPTY (the node said nothing is there) from
    UNANSWERED (nothing came back), instead of folding all three into None;
  the worker fails every open task with the reason and what the owner does, on the node-backed path
    and on the crew-file path, and a refused start exits 3 like a refused run.

No node and no model: `_aimeat_call` is replaced at the seam the code calls it through.
"""

from __future__ import annotations

import pytest

from crewaimeat import lifecycle, memory_tools, run_once
from crewaimeat.crew_def import CrewDocError
from crewaimeat.memory_tools import MemoryReadFailed, MemoryReadRefused

DENIED = {"ok": False, "error": {"code": "SCOPE_DENIED", "message": "This needs memory:read."}, "http_status": 403}


@pytest.fixture(autouse=True)
def _clean_module_state():
    lifecycle._REFUSED.clear()
    lifecycle._WORKER_RUN_START["at"] = None
    yield
    lifecycle._REFUSED.clear()
    lifecycle._WORKER_RUN_START["at"] = None


class _Recorder:
    """Stands in for `_aimeat_call`: records every tool call and answers what it is told to."""

    def __init__(self, answers: dict | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.answers = answers or {}

    def __call__(self, agent_name, tool, payload, **_kw):
        self.calls.append((tool, payload))
        return self.answers.get(tool, {"ok": True})

    def tools(self) -> list[str]:
        return [t for t, _ in self.calls]


def _raise(exc):
    raise exc


# ── the strict read ──────────────────────────────────────────────────────────────────────────


def test_a_refused_read_is_refused_and_names_the_scope(monkeypatch):
    monkeypatch.setattr(memory_tools, "_aimeat_call", lambda *a, **k: DENIED)
    with pytest.raises(MemoryReadRefused) as e:
        memory_tools.read_owner_key_strict("x", "crews.registry.x", needs="memory:read")
    exc = e.value
    assert exc.code == "SCOPE_DENIED" and exc.needed == ["memory:read"]
    assert "refused" in str(exc) and "memory:read" in str(exc)
    assert "Profile > Agents > Manage access rights" in str(exc), "the owner's fix is part of the sentence"


def test_the_scope_the_caller_knows_stands_in_when_the_node_names_none(monkeypatch):
    bare = {"ok": False, "error": {"code": "SCOPE_DENIED", "message": "Forbidden."}, "http_status": 403}
    monkeypatch.setattr(memory_tools, "_aimeat_call", lambda *a, **k: bare)
    with pytest.raises(MemoryReadRefused) as e:
        memory_tools.read_owner_key_strict("x", "k", needs="memory:read")
    assert e.value.needed == ["memory:read"]


def test_a_403_without_a_known_code_is_still_a_refusal(monkeypatch):
    other = {"ok": False, "error": {"code": "NOPE", "message": "no"}, "http_status": 403}
    monkeypatch.setattr(memory_tools, "_aimeat_call", lambda *a, **k: other)
    with pytest.raises(MemoryReadRefused):
        memory_tools.read_owner_key_strict("x", "k")


def test_an_empty_key_is_none_and_nothing_else(monkeypatch):
    absent = {"ok": False, "error": {"code": "NOT_FOUND", "message": "no such key"}, "http_status": 404}
    monkeypatch.setattr(memory_tools, "_aimeat_call", lambda *a, **k: absent)
    assert memory_tools.read_owner_key_strict("x", "k") is None


def test_a_value_comes_back_as_the_value(monkeypatch):
    seen: dict = {}

    def fake(agent, tool, payload, **kw):
        seen.update(tool=tool, payload=payload, kw=kw)
        return {"key": "k", "value": {"doc": 1}}

    monkeypatch.setattr(memory_tools, "_aimeat_call", fake)
    assert memory_tools.read_owner_key_strict("x", "k") == {"doc": 1}
    assert seen["tool"] == "aimeat_memory_read" and seen["payload"]["owner_scope"] is True
    assert seen["kw"].get("return_error") is True, "without the envelope a refusal reads as absent"


def test_no_answer_at_all_is_neither_empty_nor_refused(monkeypatch):
    monkeypatch.setattr(memory_tools, "_aimeat_call", lambda *a, **k: None)
    with pytest.raises(MemoryReadFailed):
        memory_tools.read_owner_key_strict("x", "k")


# ── failing the open tasks ───────────────────────────────────────────────────────────────────

TASKS = {
    "tasks": [
        {"id": "t-active", "status": "active", "title": "Myyntiraportti"},
        {"id": "t-done", "status": "done", "title": "Earlier"},
        {"id": "t-queued", "status": "queued", "title": "Owner starts this one"},
        {"id": "t-stalled", "status": "stalled", "title": "Stuck"},
    ]
}


def test_every_open_task_is_failed_with_the_reason_and_the_fix():
    call = _Recorder({"aimeat_task_list": TASKS})
    failed = lifecycle.fail_open_tasks(call, "myyntiraportti", "the node refused the read; needs memory:read")
    assert failed == ["t-active", "t-stalled"], "done is done, and a queued task is the owner's to start"
    fails = [p for t, p in call.calls if t == "aimeat_task_fail"]
    assert [p["task_id"] for p in fails] == ["t-active", "t-stalled"]
    msg = fails[0]["message"]
    assert "could not start" in msg and "needs memory:read" in msg, msg
    assert "can run again" in msg, "the customer must read what happens next"
    assert lifecycle.refused_runs() == {}, "not a refusal unless the caller says so"


def test_a_refused_start_is_recorded_per_task_so_the_worker_exits_3():
    call = _Recorder({"aimeat_task_list": TASKS})
    lifecycle.fail_open_tasks(call, "a", "refused", refused=True)
    assert set(lifecycle.refused_runs()) == {"t-active", "t-stalled"}


def test_the_record_survives_a_fail_the_node_would_not_take(capsys):
    # An agent without task:write cannot fail its own task either; then the exit code is all that is left.
    call = _Recorder({"aimeat_task_list": TASKS, "aimeat_task_fail": None})
    assert lifecycle.fail_open_tasks(call, "a", "refused", refused=True) == []
    assert "t-active" in lifecycle.refused_runs()
    assert "NOT accepted" in capsys.readouterr().err


def test_when_the_tasks_cannot_be_listed_nothing_is_failed_and_it_is_said(capsys):
    call = _Recorder({"aimeat_task_list": None})
    assert lifecycle.fail_open_tasks(call, "a", "why", refused=True) == []
    assert "aimeat_task_fail" not in call.tools()
    assert "could not list its tasks" in capsys.readouterr().err
    assert "(start-up)" in lifecycle.refused_runs(), "still a refused start, still exit 3"


def test_no_open_task_is_a_plain_statement_not_a_failure(capsys):
    call = _Recorder({"aimeat_task_list": {"tasks": [{"id": "t-done", "status": "done"}]}})
    assert lifecycle.fail_open_tasks(call, "a", "why") == []
    assert "no open task" in capsys.readouterr().err


# ── the worker ───────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def node_calls(monkeypatch):
    call = _Recorder({"aimeat_task_list": TASKS})
    monkeypatch.setattr("crewaimeat.aimeat_crew._aimeat_call", call)
    return call


def test_a_refused_definition_fails_the_task_and_exits_3(node_calls, monkeypatch, capsys):
    from crewaimeat import json_agent

    refused = MemoryReadRefused(
        "crews.registry.a", code="SCOPE_DENIED", message="needs memory:read", needed=["memory:read"]
    )
    monkeypatch.setattr(json_agent, "run_json_agent", lambda agent, **kw: _raise(refused))
    code = run_once._refused_exit("a", run_once._run_node_backed("a", quiet=True))
    assert code == run_once.EXIT_REFUSED, "a start the owner has to unblock is not re-run by the spawner"
    fails = [p for t, p in node_calls.calls if t == "aimeat_task_fail"]
    assert [p["task_id"] for p in fails] == ["t-active", "t-stalled"]
    assert "memory:read" in fails[0]["message"] and "Manage access rights" in fails[0]["message"]
    err = capsys.readouterr().err
    assert "CANNOT START" in err and "refused" in err
    assert "Publish one" not in err and "holds no crew definition" not in err, "refused is not empty"


def test_an_empty_definition_fails_the_task_and_says_describe_it(node_calls, monkeypatch):
    from crewaimeat import json_agent

    empty = CrewDocError(["crews.registry.a holds no crew definition — the key is empty."], missing=True)
    monkeypatch.setattr(json_agent, "run_json_agent", lambda agent, **kw: _raise(empty))
    assert run_once._refused_exit("a", run_once._run_node_backed("a", quiet=True)) == 1
    fails = [p for t, p in node_calls.calls if t == "aimeat_task_fail"]
    assert fails and "not yet defined" in fails[0]["message"] and "agent's page" in fails[0]["message"]
    assert lifecycle.refused_runs() == {}, "an empty key is not a permission problem"


def test_an_invalid_definition_names_every_error_on_the_task(node_calls, monkeypatch):
    from crewaimeat import json_agent

    invalid = CrewDocError(["temperature must be a number", "agents[0].role is required"])
    monkeypatch.setattr(json_agent, "run_json_agent", lambda agent, **kw: _raise(invalid))
    run_once._run_node_backed("a", quiet=True)
    msg = [p for t, p in node_calls.calls if t == "aimeat_task_fail"][0]["message"]
    assert "temperature must be a number" in msg and "agents[0].role is required" in msg


def test_the_lock_guard_and_the_auth_guard_fail_nothing(node_calls, monkeypatch):
    # Another daemon holding the lock (exit 0) and a rejected token (exit 2) are not a runtime that
    # cannot start: the first one IS running the task, the second needs re-approval before anything.
    from crewaimeat import json_agent

    for exit_code in (0, 2):
        monkeypatch.setattr(json_agent, "run_json_agent", lambda agent, _c=exit_code, **kw: _raise(SystemExit(_c)))
        assert run_once._run_node_backed("a", quiet=True) == exit_code
    assert "aimeat_task_fail" not in node_calls.tools()


BROKEN_CREW = """AGENT_NAME = "broken"

raise ImportError("no such module: thing")


def build_domain(ctx):
    return [], []


def run():
    pass
"""

NO_RUN_CREW = """AGENT_NAME = "norun"


def build_domain(ctx):
    return [], []
"""


def test_a_crew_file_that_cannot_load_fails_the_task(node_calls, monkeypatch, tmp_path):
    monkeypatch.setenv("AIMEAT_LOG_TIMESTAMPS", "0")
    monkeypatch.delenv("AIMEAT_SPAWN_RUN_ID", raising=False)
    (tmp_path / "crews").mkdir()
    (tmp_path / "crews" / "broken_crew.py").write_text(BROKEN_CREW, encoding="utf-8")
    assert run_once.run_once("broken", root=tmp_path, quiet=True) == 1
    fails = [p for t, p in node_calls.calls if t == "aimeat_task_fail"]
    assert fails and "broken_crew.py" in fails[0]["message"] and "no such module" in fails[0]["message"]


def test_a_crew_file_with_no_run_fails_the_task(node_calls, monkeypatch, tmp_path):
    monkeypatch.setenv("AIMEAT_LOG_TIMESTAMPS", "0")
    monkeypatch.delenv("AIMEAT_SPAWN_RUN_ID", raising=False)
    (tmp_path / "crews").mkdir()
    (tmp_path / "crews" / "norun_crew.py").write_text(NO_RUN_CREW, encoding="utf-8")
    assert run_once.run_once("norun", root=tmp_path, quiet=True) == 1
    fails = [p for t, p in node_calls.calls if t == "aimeat_task_fail"]
    assert fails and "has no run()" in fails[0]["message"]


def test_failing_the_tasks_never_hides_the_exit_code(monkeypatch, capsys):
    from crewaimeat import json_agent

    monkeypatch.setattr("crewaimeat.aimeat_crew._aimeat_call", lambda *a, **k: _raise(RuntimeError("no daemon")))
    monkeypatch.setattr(json_agent, "run_json_agent", lambda agent, **kw: _raise(RuntimeError("boom")))
    assert run_once._run_node_backed("a", quiet=True) == 1
    assert "could not fail its open tasks" in capsys.readouterr().err

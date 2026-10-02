"""The onboarding step runs once.

Sold place, 2026-10-02: at run start the deterministic driver repeated accept_test_task ->
aimeat_task_propose_todos thirteen times in twelve seconds (rounds [6]..[18], each "ok") before going on,
and the run took 267 s against 118 s on the earlier image. The node passes that step the moment the test
task it WATCHES carries todos; a plan proposed on another task answers ok and advances nothing, and the
package driver repeats the node's next step every round until its cap. Two things hold here:

  the id the scaffold proposes on is the one the node's status names (hints.test_task_id, else the
    step's details.testTaskId), and the scan of the task list is only the fallback;
  a task tool that already answered ok for identical arguments is not sent the same call again: the
    second repeat is answered with STEP_NOT_ADVANCING, which the driver stops on.
"""

from __future__ import annotations

import json

from crewaimeat import aimeat_crew


class _Tool:
    def __init__(self, name, answer=None, raise_exc=None):
        self.name = name
        self.answer = answer if answer is not None else {"ok": True}
        self.raise_exc = raise_exc
        self.calls: list[dict] = []

    def run(self, **kw):
        self.calls.append(dict(kw))
        if self.raise_exc:
            raise self.raise_exc
        return json.dumps(self.answer) if isinstance(self.answer, (dict, list)) else self.answer


def _status(hint=None, details=None):
    data = {"onboarding": {"steps": [{"id": "accept_test_task", "details": details or {}}]}, "hints": {}}
    if hint:
        data["hints"]["test_task_id"] = hint
    return _Tool("aimeat_onboarding_status", data)


# ── the id the node watches ──────────────────────────────────────────────────────────────────


def test_the_hinted_id_wins():
    assert (
        aimeat_crew._test_task_id_from_status([_status(hint="t-hint", details={"testTaskId": "t-det"})], "a")
        == "t-hint"
    )


def test_the_steps_own_details_are_next():
    assert aimeat_crew._test_task_id_from_status([_status(details={"testTaskId": "t-det"})], "a") == "t-det"


def test_a_placeholder_hint_is_no_id():
    assert aimeat_crew._test_task_id_from_status([_status(hint="{test_task_id}")], "a") is None


def test_no_status_tool_or_a_failed_read_means_none(capsys):
    assert aimeat_crew._test_task_id_from_status([], "a") is None
    broken = _Tool("aimeat_onboarding_status", raise_exc=RuntimeError("tunnel"))
    assert aimeat_crew._test_task_id_from_status([broken], "a") is None
    assert "status read for the test task id failed" in capsys.readouterr().err


# ── once per identical call ──────────────────────────────────────────────────────────────────


def test_an_identical_repeat_after_ok_is_answered_with_a_failure_the_driver_stops_on(capsys):
    inner = _Tool("aimeat_task_propose_todos", {"ok": True, "todo_count": 2})
    [tool] = aimeat_crew._once_per_run([inner], "concierge")
    args = {"task_id": "t-1", "todos": [{"title": "x"}]}

    first = json.loads(tool.run(**args))
    assert first["ok"] is True and inner.calls == [args]
    second = json.loads(tool.run(**args))
    assert second["code"] == "STEP_NOT_ADVANCING" and "t-1" in second["message"]
    assert inner.calls == [args], "the node was not asked twice"
    assert "not sending it again" in capsys.readouterr().err
    from aimeat_crewai.onboarding import failure_of

    assert failure_of(second), "the package driver reads it as a failure, and stops on the second one"


def test_different_arguments_are_a_different_call():
    inner = _Tool("aimeat_task_propose_todos")
    [tool] = aimeat_crew._once_per_run([inner], "concierge")
    tool.run(task_id="t-1", todos=[])
    tool.run(task_id="t-2", todos=[])
    assert len(inner.calls) == 2


def test_a_call_the_node_refused_may_be_sent_again():
    # A refusal is the node's to repeat or not (the driver stops on the same code twice); only an OK that
    # advanced nothing is held back here.
    inner = _Tool("aimeat_task_propose_todos", {"code": "TASK_NOT_FOUND", "message": "no such task"})
    [tool] = aimeat_crew._once_per_run([inner], "concierge")
    tool.run(task_id="t-1")
    tool.run(task_id="t-1")
    assert len(inner.calls) == 2


def test_only_the_task_tools_are_wrapped_and_the_rest_pass_through():
    status = _Tool("aimeat_onboarding_status")
    complete = _Tool("aimeat_task_complete")
    tools = aimeat_crew._once_per_run([status, complete], "a")
    assert tools[0] is status
    assert isinstance(tools[1], aimeat_crew._OnceTool) and tools[1].name == "aimeat_task_complete"
    assert tools[1].calls is complete.calls, "other attributes reach the real tool"


# ── a refusal is read as a refusal, in every shape that reaches the driver ───────────────────

MCP_ERROR = (
    "MCP error -32602: Input validation error: Invalid arguments for tool aimeat_task_propose_todos: "
    "Invalid input: expected string, received null at todos[0].description"
)
ENVELOPE = {"ok": False, "protocol": "aimeat", "error": {"code": "PLAN_REQUIRED", "message": "it has no plan yet"}}


def test_the_mcp_adapters_own_refusal_is_a_failure_the_driver_sees(capsys):
    """Measured against a local node 2026-10-02: this text was logged 'ok' by both drivers, the plan
    never landed, and accept_test_task was repeated until the cap."""
    from aimeat_crewai.onboarding import failure_of

    inner = _Tool("aimeat_task_propose_todos", MCP_ERROR)
    [tool] = aimeat_crew._once_per_run([inner], "a")
    out = json.loads(tool.run(task_id="t-1", todos=[]))
    assert out["code"] == "MCP_ERROR" and "received null at todos[0].description" in out["message"]
    assert failure_of(out), "the package driver stops on it instead of logging ok"
    assert "answered: MCP error -32602" in capsys.readouterr().err, "the node's words are in the log"
    tool.run(task_id="t-1", todos=[])
    assert len(inner.calls) == 2, "a refusal is not held back; only an ok that advanced nothing is"


def test_the_nodes_envelope_is_a_failure_too():
    inner = _Tool("aimeat_task_complete", ENVELOPE)
    [tool] = aimeat_crew._once_per_run([inner], "a")
    out = json.loads(tool.run(task_id="t-1", message="done"))
    assert out == {"code": "PLAN_REQUIRED", "message": "it has no plan yet"}


def test_the_safety_net_reads_the_envelope_as_rejected(capsys):
    tools = [
        _Tool(
            "aimeat_onboarding_status",
            {
                "onboarding": {
                    "steps": [
                        {
                            "id": "complete_test_task",
                            "required": True,
                            "status": "pending",
                            "details": {"testTaskId": "t-1"},
                        }
                    ]
                },
                "step_guide": {
                    "complete_test_task": {"tool": "aimeat_task_complete", "args": {"task_id": "t-1", "message": "x"}}
                },
            },
        ),
        _Tool("aimeat_task_complete", ENVELOPE),
    ]
    aimeat_crew._finish_pending_onboarding(tools, "a", {}, attempts=1)
    err = capsys.readouterr().err
    assert "REJECTED by node: PLAN_REQUIRED" in err and ": ok" not in err


def test_the_test_task_plan_fills_every_field_of_the_tools_schema():
    """The liaison's MCP adapter sends a left-out field as null, and the connector's schema takes an
    optional string, not null -- so a todo without `description` is refused before the node sees it."""
    for todo in aimeat_crew._TEST_TASK_TODOS:
        assert set(todo) >= {"title", "description", "verification", "estimate_minutes", "effects"}
        assert all(isinstance(todo[k], str) and todo[k] for k in ("title", "description", "verification"))
        assert isinstance(todo["estimate_minutes"], int) and isinstance(todo["effects"], list)

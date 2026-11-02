"""A run that did not write what its definition requires does not end done.

varoituskartta-vahti ended done three times on 2026-11-01 with nothing written to `varoituskartta.c.*`.
These hold the validator, the check, and the wiring into the task's close. No node: the read is replaced
at `_aimeat_rest`. The real node is in test_run_refusals_live.py.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from crewaimeat import lifecycle
from crewaimeat import required_outcome as ro
from crewaimeat.crew_def import validate_crew_doc

REQUIRED = {"memory_prefix": "kartta.c.", "namespace": "agent", "none_when": "NOTHING QUEUED"}


@pytest.fixture(autouse=True)
def _clean():
    lifecycle._OUTCOMES.clear()
    yield
    lifecycle._OUTCOMES.clear()


@pytest.fixture
def node(monkeypatch):
    """The agent's records as the node lists them; `answer` replaces the whole reply when set."""
    state = SimpleNamespace(records={}, answer=None, asked=[])

    def rest(agent, method, path, body=None, **_kw):
        state.asked.append((agent, method, path))
        if state.answer is not None:
            return state.answer
        return {"items": [{"key": k, "version": v} for k, v in state.records.items()], "total": len(state.records)}

    monkeypatch.setattr("crewaimeat.aimeat_crew._aimeat_rest", rest)
    return state


def _doc(**fields) -> dict:
    return {
        "agent_name": "kartta",
        "agents": [{"name": "E", "role": "r", "goal": "g", "backstory": "b"}],
        "tasks": [{"id": "t", "agent": "E", "description": "{{ctx.prompt}}", "expected_output": "x"}],
        **fields,
    }


# ── the definition ───────────────────────────────────────────────────────────────────────────


def test_a_definition_with_the_field_is_valid_and_one_without_stays_valid():
    assert validate_crew_doc(_doc(required_outcome=REQUIRED)) == []
    assert validate_crew_doc(_doc()) == []


@pytest.mark.parametrize(
    ("value", "says"),
    [
        ("kartta.c.", "must be an object"),
        ({"namespace": "agent"}, "memory_prefix"),
        ({"memory_prefix": "a b.", "namespace": "agent"}, "memory_prefix"),
        ({"memory_prefix": "a.{x}.", "namespace": "agent"}, "memory_prefix"),
        ({"memory_prefix": "kartta."}, "whose memory"),
        ({"memory_prefix": "kartta.", "namespace": "owner"}, "whose memory"),
        ({"memory_prefix": "crews.kartta.", "namespace": "agent"}, "every run would pass"),
        ({"memory_prefix": "agents.kartta.", "namespace": "agent"}, "every run would pass"),
        ({"memory_prefix": "kartta.", "namespace": "agent", "none_when": " "}, "none_when"),
        ({"memory_prefix": "kartta.", "namespace": "agent", "must": 1}, "unknown field"),
    ],
)
def test_a_wrong_field_is_refused_when_the_definition_is_read(value, says):
    errors = validate_crew_doc(_doc(required_outcome=value))
    assert any(says in e for e in errors), errors


def test_the_definition_hands_the_field_to_the_crew():
    from crewaimeat.crew_def import crewspec_from_json

    assert crewspec_from_json(_doc(required_outcome=REQUIRED)).required_outcome == REQUIRED
    assert crewspec_from_json(_doc()).required_outcome is None


def test_the_runtime_says_it_honours_the_field():
    from crewaimeat.runtime_features import FEATURES

    assert "required_outcome" in FEATURES


# ── the check ────────────────────────────────────────────────────────────────────────────────


def test_nothing_written_fails(node):
    node.records = {"kartta.c.RU": 3}
    ro.check("kartta", "t1", REQUIRED, {"kartta.c.RU": 3}, "I wrote the record for RU.")
    failed, message = lifecycle.take_outcome("t1")
    assert failed is True
    assert "kartta.c." in message and "NOTHING QUEUED" in message


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ({"kartta.c.RU": 3}, {"kartta.c.RU": 4}),  # the same record again: a write adds one to its version
        ({}, {"kartta.c.RU": 1}),  # a new record
        ({"kartta.c.RU": 3}, {"kartta.c.RU": 3, "kartta.c.SE": 1}),  # one more beside an old one
    ],
)
def test_a_write_is_any_changed_version(node, before, after):
    node.records = after
    ro.check("kartta", "t1", REQUIRED, before, "done")
    assert lifecycle.take_outcome("t1") is None


def test_the_words_of_nothing_to_do_pass_exactly_and_ask_nothing(node):
    ro.check("kartta", "t1", REQUIRED, {}, "NOTHING QUEUED")
    assert lifecycle.take_outcome("t1") is None
    assert node.asked == []


@pytest.mark.parametrize("answer", ["Nothing queued.", "NOTHING QUEUED today", "I found NOTHING QUEUED"])
def test_other_words_are_not_nothing_to_do(node, answer):
    ro.check("kartta", "t1", REQUIRED, {}, answer)
    assert lifecycle.take_outcome("t1")[0] is True


def test_without_none_when_only_a_write_passes(node):
    required = {"memory_prefix": "kartta.c.", "namespace": "agent"}
    ro.check("kartta", "t1", required, {}, "NOTHING QUEUED")
    failed, message = lifecycle.take_outcome("t1")
    assert failed is True and "its answer was not" not in message


@pytest.mark.parametrize(
    ("before", "answer"),
    [
        (None, {"items": []}),  # the read before the run got no answer
        ({}, {"ok": False, "error": {"code": "UNAVAILABLE"}, "http_status": 503}),  # nor the one after
        ({}, {"total": 0}),  # an answer with no list
    ],
)
def test_when_it_cannot_be_read_the_task_completes_and_says_so(node, before, answer):
    node.answer = answer
    ro.check("kartta", "t1", REQUIRED, before, "done")
    failed, message = lifecycle.take_outcome("t1")
    assert failed is False and "could not be checked" in message


def test_a_run_already_failed_keeps_its_own_reason(node):
    lifecycle.note_outcome("t1", "the workflow's key is refused", failed=True)
    ro.check("kartta", "t1", REQUIRED, None, "done")
    assert lifecycle.take_outcome("t1") == (True, "the workflow's key is refused")


def test_the_read_is_the_agents_own_memory_with_no_values(node):
    assert ro.record_versions("kartta#o@n", "kartta.c.") == {}
    assert node.asked == [("kartta#o@n", "GET", "/v1/memory?prefix=kartta.c.&include=meta")]


# ── the close ────────────────────────────────────────────────────────────────────────────────


def test_the_close_runs_after_the_check_and_acts_on_it(node):
    closed = []

    def close(out):
        closed.append((out, lifecycle.has_failed_outcome("t1")))

    last = SimpleNamespace(output=SimpleNamespace(raw=" I wrote it. "))
    ro.checked_close("kartta", "t1", REQUIRED, {}, last, close)("finalize output")
    assert closed == [("finalize output", True)]


def test_a_check_that_raises_still_closes_the_task(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("tunnel dropped")

    monkeypatch.setattr(ro, "check", boom)
    closed = []
    ro.checked_close("kartta", "t1", REQUIRED, {}, SimpleNamespace(output=None), closed.append)("x")
    assert closed == ["x"]
    assert "could not be checked" in lifecycle.take_outcome("t1")[1]


# ── a whole run: the scaffold's builder, real CrewAI, a stub model ───────────────────────────


class _Run:
    """One task through `run_crew`'s real builder and a real kickoff; only the model and the node are stubs."""

    def __init__(self, monkeypatch, answer: str, records: dict, *, source: str | None = None):
        from crewai import Agent, BaseLLM

        from crewaimeat import aimeat_crew as ac
        from crewaimeat.crew_def import crewspec_from_json

        class _Model(BaseLLM):
            def call(self, messages, **_kw):  # noqa: ARG002 - one answer, whatever is asked
                return "Thought: done" + chr(10) + f"Final Answer: {answer}"

            def supports_function_calling(self):
                return False

            def supports_stop_words(self):
                return False

        self.calls: list[tuple[str, dict]] = []
        model = _Model(model="stub")

        def call(agent, tool, payload, **_kw):
            self.calls.append((tool, payload))
            return {"ok": True}

        def rest(agent, method, path, body=None, **_kw):
            if path.startswith("/v1/memory?prefix="):
                return {"items": [{"key": k, "version": v} for k, v in records.items()]}
            return {"refusals": []} if "/refusals" in path else {}

        captured: dict = {}
        monkeypatch.setattr(ac, "_aimeat_call", call)
        monkeypatch.setattr(ac, "_aimeat_rest", rest)
        monkeypatch.setattr(ac, "get_llm", lambda *a, **k: model)
        monkeypatch.setattr(ac, "run_crew_daemon", lambda *a, **k: captured.update(k))
        # The start-up before the daemon: it waits for the owner's approval and onboards, and is not this test's.
        monkeypatch.setattr(ac, "_acquire_single_instance", lambda *_a: True)
        monkeypatch.setattr(ac, "_wait_for_auth", lambda *_a: None)
        monkeypatch.setattr(ac, "_serve_attach_bridge", lambda *_a: None)
        monkeypatch.setattr(ac, "ensure_serve", lambda **_k: {"port": 1})
        monkeypatch.setattr(ac, "_onboarding_state", lambda *_a: (True, 0))
        ac.run_crew(crewspec_from_json(_doc(required_outcome=REQUIRED), owner="o"))
        task = {"id": "t1", "description": "do it", "title": "do it", "status": "active"}
        if source:
            task["_source"] = source
        liaison = Agent(role="l", goal="g", backstory="b", llm=model)
        captured["build_crew"](task, liaison).kickoff()

    def tools(self) -> list[str]:
        return [t for t, _ in self.calls if t in ("aimeat_task_fail", "aimeat_task_complete")]


def test_a_real_run_that_only_said_it_wrote_ends_failed(monkeypatch):
    run = _Run(monkeypatch, "I wrote the record for RU.", {"kartta.c.RU": 3})
    assert run.tools() == ["aimeat_task_fail"]
    assert "kartta.c." in dict(run.calls)["aimeat_task_fail"]["message"]


def test_a_real_run_with_nothing_to_do_completes(monkeypatch):
    assert _Run(monkeypatch, "NOTHING QUEUED", {}).tools() == ["aimeat_task_complete"]

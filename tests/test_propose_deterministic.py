"""PROPOSE-vaihe: task-runner ei saa kutsua mallia, muut saavat."""

from __future__ import annotations

import pytest

from crewaimeat import aimeat_crew as ac


def test_plan_names_the_task_and_needs_no_model():
    todos = ac._task_runner_plan({"title": "Ilta-Sanomat 2026-09-06"})
    assert len(todos) == 3
    assert "Ilta-Sanomat 2026-09-06" in todos[0]["title"]
    assert all(t.get("verification") for t in todos), "jokainen todo tarvitsee tarkistuskriteerin"


def test_plan_survives_a_task_with_no_title():
    todos = ac._task_runner_plan({})
    assert "this task" in todos[0]["title"]


def test_deterministic_phase_kickoff_calls_nothing():
    ph = ac._DeterministicPhase("proposed 3 todo(s) deterministically")
    assert ph.kickoff() == "proposed 3 todo(s) deterministically"
    assert ph.kickoff(anything=1) == "proposed 3 todo(s) deterministically"


def test_a_failed_proposal_raises_instead_of_looking_done(monkeypatch):
    """A None from the node means no plan landed. `_dispatch` treats a PROPOSE crash as recoverable
    and leaves the task queued for the next poll; returning quietly would record the phase as done
    and the task would sit forever with no plan and nothing to notice it."""
    monkeypatch.setattr(ac, "_aimeat_call", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="no plan for it"):
        ac._propose_deterministically("joker", {"id": "t-1"}, [{"title": "x"}], "task-runner task")


def test_a_landed_proposal_returns_a_phase_that_calls_no_model(monkeypatch):
    sent = {}

    def _fake(agent, tool, payload, **kw):
        sent.update(tool=tool, payload=payload)
        return {"ok": True}

    monkeypatch.setattr(ac, "_aimeat_call", _fake)
    todos = ac._task_runner_plan({"title": "Ilta-Sanomat"})
    phase = ac._propose_deterministically("joker", {"id": "t-2"}, todos, "task-runner task")
    assert sent["tool"] == "aimeat_task_propose_todos"
    assert sent["payload"]["task_id"] == "t-2" and len(sent["payload"]["todos"]) == 3
    assert "3 todo(s)" in phase.kickoff()

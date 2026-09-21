"""feedback-wisdom pays the model only for NEW statistics (owner, 2026-09-21).

Measured before: an hourly schedule woke the agent 24 times a day, every wake ran a DeepSeek crew to
re-polish advisories that had not changed (~$0.20/day on the ledger), and the idle hook — whose
"unchanged" check lived in process memory, empty in every spawned worker — first rewrote the polished
wording back to the rules' raw text.
"""

from __future__ import annotations

import importlib.util
import inspect
from pathlib import Path

import pytest

from crewaimeat import aimeat_crew as ac
from crewaimeat import feedback_wisdom_contract as fw
from crewaimeat import local_marks

_ROOT = Path(__file__).resolve().parent.parent
SNAP = [("AIMEAT", {"env": 1}, {"range": {"from": "2026-09-20", "to": "2026-09-21"}, "tickets": 12})]


@pytest.fixture
def marks(tmp_path, monkeypatch):
    monkeypatch.setattr(local_marks, "_path", lambda name: tmp_path / f"{name}.json")
    monkeypatch.setitem(fw._LAST_SIG, "v", None)
    return tmp_path


@pytest.fixture
def crew_mod():
    spec = importlib.util.spec_from_file_location("feedback_wisdom_crew", _ROOT / "crews" / "feedback_wisdom_crew.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_on_task_none_means_run_the_crew():
    """The seam: a handler answering None hands the task to the crew; any string is the deliverable.
    `_build` is a closure inside run_crew, so the contract is pinned on its source."""
    src = inspect.getsource(ac.run_crew)
    assert "answer = spec.on_task(task) if spec.on_task is not None else None" in src
    assert "if answer is not None:" in src


def test_unchanged_statistics_survive_a_new_process(marks, monkeypatch):
    """Every spawned worker is a fresh process; the second one must still know nothing changed."""
    writes: list[str] = []
    monkeypatch.setattr(fw, "discover_stats", lambda: SNAP)
    monkeypatch.setattr(fw, "mirror_targets", lambda: [])
    monkeypatch.setattr(fw, "_prior_stats", lambda org, window: None)
    monkeypatch.setattr(fw, "derive_advisories", lambda org, stats, prior=None: [{"id": "a1"}])
    monkeypatch.setattr(fw, "write_advisory_outbox", lambda adv: writes.append(adv["id"]) or "written")

    first = fw.process_feedback_stats()
    fw._LAST_SIG["v"] = None  # what a new process looks like
    second = fw.process_feedback_stats()

    assert first["advisories_written"] == 1 and writes == ["a1"]
    assert second.get("unchanged") is True and writes == ["a1"], "the rules' text must not overwrite the analyst's"


def test_the_model_runs_once_per_new_statistics(marks, monkeypatch, crew_mod):
    monkeypatch.setattr(fw, "discover_stats", lambda: SNAP)
    passes: list[int] = []
    monkeypatch.setattr(fw, "process_feedback_stats", lambda: passes.append(1) or {"unchanged": True})
    task = {"id": "t1", "title": "Palautetilastot toimintaohjeiksi"}

    assert crew_mod.handle_task(task) is None  # new statistics: the analyst crew runs
    again = crew_mod.handle_task(task)  # the next daily wake, nothing new
    assert isinstance(again, str) and "nothing for the model" in again
    assert passes == [1]  # the deterministic pass still ran, for free

    monkeypatch.setattr(fw, "discover_stats", lambda: [(SNAP[0][0], SNAP[0][1], {**SNAP[0][2], "tickets": 13})])
    assert crew_mod.handle_task(task) is None  # the desk published something new: the model is paid again


def test_no_statistics_is_answered_without_a_model(marks, monkeypatch, crew_mod):
    monkeypatch.setattr(fw, "discover_stats", lambda: [])
    out = crew_mod.handle_task({"id": "t2", "title": "Palautetilastot toimintaohjeiksi"})
    assert isinstance(out, str) and "no model asked" in out


def test_the_crew_hands_its_tasks_to_the_handler():
    import ast

    tree = ast.parse((_ROOT / "crews" / "feedback_wisdom_crew.py").read_text(encoding="utf-8"))
    spec = next(n for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "CrewSpec")
    assert any(k.arg == "on_task" for k in spec.keywords)

"""DETERMINISTIC EXECUTE (CrewSpec.on_task) — the model is never asked, the deliverable path is.

The value of this path is entirely in what it does NOT skip: `_make_publish_cb` and
`_make_complete_cb` are what write the deliverable key, mark the todos and gate on verify, and a
handler that ran around them would look identical from the outside while losing all of it.
"""

from __future__ import annotations

import datetime
import importlib.util
from pathlib import Path

import pytest

from crewaimeat import aimeat_crew as ac

_ROOT = Path(__file__).resolve().parent.parent


def _crew_module(name: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / "crews" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── the phase object the daemon dispatches ──────────────────────────────────────────────────────
def test_the_phase_needs_no_agent_and_no_crew():
    """`_dispatch` only ever does builder(task, liaison).kickoff() — that is the whole contract."""
    ph = ac._DeterministicPhase("done")
    assert callable(ph.kickoff) and ph.kickoff() == "done"
    assert not hasattr(ph, "agents"), "a Crew would drag in a model; this must not be one"


# ── the params a model used to guess ────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "text,expect_date,expect_edition",
    [
        ("Tarkasta 2026-09-04 ilta", "2026-09-04", "evening"),
        ("tarkasta tämän aamun ajo", None, "morning"),
        ("", None, "evening"),
    ],
)
def test_params_are_read_not_guessed(text, expect_date, expect_edition):
    wi = _crew_module("workflow_inspector_crew")
    got = wi._params_from(text)
    assert got["edition"] == expect_edition
    if expect_date:
        assert got["date"] == expect_date
    else:
        # no date in the request -> today, never an invented one
        assert got["date"] == datetime.datetime.now(wi._TZ).date().isoformat()


def test_handle_task_returns_the_inspection_report(monkeypatch):
    wi = _crew_module("workflow_inspector_crew")
    published = {}
    monkeypatch.setattr(
        wi,
        "inspect",
        lambda wf, params: {
            "workflow": wf,
            "date": params["date"],
            "edition": params["edition"],
            "overall": "GREEN",
            "actions": [],
            "fixed": [],
            "still_red": [],
            "report_md": "# Inspection\n\nAll six steps green.",
        },
    )
    monkeypatch.setattr(wi, "publish_inspection", lambda res: published.update(res))
    out = wi.handle_task({"title": "Tarkasta 2026-09-04", "description": ""})
    assert "All six steps green" in out
    assert published["date"] == "2026-09-04", "the inspection must be PUBLISHED, not only returned"


def test_a_report_less_result_still_says_something(monkeypatch):
    """Never return an empty deliverable: an empty string publishes as an empty key."""
    wi = _crew_module("workflow_inspector_crew")
    monkeypatch.setattr(
        wi,
        "inspect",
        lambda wf, params: {
            "workflow": wf,
            "date": params["date"],
            "edition": params["edition"],
            "overall": "RED",
            "actions": ["a"],
            "fixed": [],
            "still_red": ["editorial"],
            "report_md": "",
        },
    )
    monkeypatch.setattr(wi, "publish_inspection", lambda res: None)
    out = wi.handle_task({"title": "tarkasta", "description": ""})
    assert "RED" in out and "editorial" in out

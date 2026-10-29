"""The Sanomat workflow inspector: what it repairs, and what a repair is allowed to cost."""

from __future__ import annotations

# A run that started: its first step has produced the day's raw material.
_FETCHED = {"id": "fetch", "state": "GREEN", "input": {"observed": ""}, "output": {"observed": "17 nonempty"}}


def test_a_red_step_is_re_run_once_per_cooldown(tmp_path, monkeypatch):
    """A re-run of a write step writes the articles again — 20-25 minutes of a paid model. Worth it for
    a transient miss, worth nothing while the step stays red: 15 inspections of the same red step cost
    $5.12 and 1 149 model calls on 2026-09-20."""
    from crewaimeat import local_marks
    from crewaimeat import workflow_inspector as wi

    monkeypatch.setattr(local_marks, "_path", lambda name: tmp_path / f"{name}.json")

    runs: list[str] = []
    monkeypatch.setattr(wi, "_rerun_step", lambda sid, date, edition: runs.append(sid) or "re-ran")
    monkeypatch.setattr(wi, "_agent_task_state", lambda a: f"{a}: parked")

    wf_id = "laimeat-sanomat-evening"
    red_step = {
        "id": "write-a",
        "state": "output-RED",
        "input": {"observed": "raw present"},
        "output": {"observed": "no articles"},
    }
    monkeypatch.setattr(wi, "check_workflow", lambda *a, **k: {"steps": [_FETCHED, red_step]})

    params = {"date": "2026-09-20", "edition": "evening"}
    first = wi.inspect(wf_id, params, lister=lambda *a, **k: [])
    second = wi.inspect(wf_id, params, lister=lambda *a, **k: [])

    assert runs == ["write-a"], runs  # the second inspection did not pay for the articles again
    assert any("SKIPPED" in a for a in second["actions"]), second["actions"]
    assert first["overall"] == second["overall"] == "RED"  # it still reports the truth


def test_a_different_day_is_not_blocked_by_yesterdays_re_run(tmp_path, monkeypatch):
    """The cooldown is per (date, edition, step) — tonight's edition may always be repaired once."""
    from crewaimeat import local_marks
    from crewaimeat import workflow_inspector as wi

    monkeypatch.setattr(local_marks, "_path", lambda name: tmp_path / f"{name}.json")
    runs: list[str] = []
    monkeypatch.setattr(wi, "_rerun_step", lambda sid, date, edition: runs.append(date) or "re-ran")
    monkeypatch.setattr(wi, "_agent_task_state", lambda a: "parked")
    monkeypatch.setattr(
        wi,
        "check_workflow",
        lambda *a, **k: {
            "steps": [
                _FETCHED,
                {
                    "id": "write-a",
                    "state": "output-RED",
                    "input": {"observed": "raw"},
                    "output": {"observed": "none"},
                },
            ]
        },
    )
    wi.inspect("laimeat-sanomat-evening", {"date": "2026-09-19", "edition": "evening"}, lister=lambda *a, **k: [])
    wi.inspect("laimeat-sanomat-evening", {"date": "2026-09-20", "edition": "evening"}, lister=lambda *a, **k: [])
    assert runs == ["2026-09-19", "2026-09-20"], runs


def test_a_red_step_is_not_re_run_again_on_a_later_day_of_the_same_edition(tmp_path, monkeypatch):
    """Once per edition, not once per six hours: the six-hour cooldown let four re-writes a day through
    (2026-10-20 to 10-29, $6.63 and 583 model calls) for an edition that stayed red each time."""
    import datetime
    import json

    from crewaimeat import local_marks
    from crewaimeat import workflow_inspector as wi

    monkeypatch.setattr(local_marks, "_path", lambda name: tmp_path / f"{name}.json")
    long_ago = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=30)).isoformat()
    (tmp_path / f"{wi._RERUN_MARK}.json").write_text(
        json.dumps({"2026-10-28:evening:write-a": long_ago}), encoding="utf-8"
    )
    runs: list[str] = []
    monkeypatch.setattr(wi, "_rerun_step", lambda sid, date, edition: runs.append(sid) or "re-ran")
    monkeypatch.setattr(wi, "_agent_task_state", lambda a: "parked")
    red = {"id": "write-a", "state": "output-RED", "input": {"observed": "raw"}, "output": {"observed": "9 of 12"}}
    monkeypatch.setattr(wi, "check_workflow", lambda *a, **k: {"steps": [_FETCHED, red]})

    res = wi.inspect("laimeat-sanomat-evening", {"date": "2026-10-28", "edition": "evening"}, lister=lambda *a, **k: [])

    assert runs == [], runs
    assert any("SKIPPED" in a for a in res["actions"]), res["actions"]


def test_a_run_that_never_started_is_reported_not_written_by_the_inspector(tmp_path, monkeypatch):
    """From 2026-10-21 the node refused every run of the Sanomat workflow, and the hourly inspection
    fetched and wrote the edition itself four times a day. No step with output = the run did not start;
    a re-run cannot start it, so nothing is re-run and the report says where the cause is."""
    from crewaimeat import local_marks
    from crewaimeat import workflow_inspector as wi

    monkeypatch.setattr(local_marks, "_path", lambda name: tmp_path / f"{name}.json")
    runs: list[str] = []
    monkeypatch.setattr(wi, "_rerun_step", lambda sid, date, edition: runs.append(sid) or "re-ran")
    asked: list[str] = []
    monkeypatch.setattr(wi, "_agent_task_state", lambda a: asked.append(a) or "parked")
    nothing = {"observed": "empty/missing"}
    steps = [
        {"id": "fetch", "state": "output-RED", "input": {"observed": ""}, "output": nothing},
        {"id": "write-a", "state": "input-RED", "input": nothing, "output": nothing},
        {"id": "space-weather", "state": "output-RED", "input": {"observed": ""}, "output": nothing},
    ]
    monkeypatch.setattr(wi, "check_workflow", lambda *a, **k: {"steps": steps})

    res = wi.inspect("laimeat-sanomat-evening", {"date": "2026-10-29", "edition": "evening"}, lister=lambda *a, **k: [])

    assert runs == [], "a run that did not start must not be written by the inspector"
    assert asked == [], "every step is red for one reason; the per-step diagnosis would blame each agent"
    assert res["overall"] == "RED"
    assert "did not run for 2026-10-29 evening" in res["report_md"], res["report_md"]
    assert not (tmp_path / f"{wi._RERUN_MARK}.json").exists(), "no repair was attempted, so none is marked"

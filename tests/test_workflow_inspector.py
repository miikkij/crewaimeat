"""The Sanomat workflow inspector: what it repairs, and what a repair is allowed to cost."""

from __future__ import annotations


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
    monkeypatch.setattr(wi, "check_workflow", lambda *a, **k: {"steps": [red_step]})

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
                {
                    "id": "write-a",
                    "state": "output-RED",
                    "input": {"observed": "raw"},
                    "output": {"observed": "none"},
                }
            ]
        },
    )
    wi.inspect("laimeat-sanomat-evening", {"date": "2026-09-19", "edition": "evening"}, lister=lambda *a, **k: [])
    wi.inspect("laimeat-sanomat-evening", {"date": "2026-09-20", "edition": "evening"}, lister=lambda *a, **k: [])
    assert runs == ["2026-09-19", "2026-09-20"], runs

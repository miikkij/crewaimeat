"""Lens 3 — what the NODE believes, versus what the repo declares.

The rule this lens exists to enforce is **never report "fine" when you could not look**. Connector
tools answer *empty, not error*, off-fleet, which is indistinguishable from "looked and found nothing"
— and that is exactly how this lens shipped its first version: it asked the MCP `aimeat_schedule_list`,
whose rows carry no target agent, so every comparison matched nothing and it printed a clean report.

The schedule half answers the question nobody could answer before: which trigger drives the evening
paper? Six disabled crons sat beside a workflow for months, and nobody deleted them because nobody
could say what deleting one would stop.

Everything here is stubbed; no network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crewaimeat.doctor import liveness
from crewaimeat.doctor.inventory import gather
from crewaimeat.doctor.model import ERROR, WARN, Report

CREW = 'AGENT_NAME = "{agent}"\nLLM_PROFILE = "coding"\n{extra}\n\ndef build_domain(ctx):\n    return ([], [])\n\n\ndef run():\n    pass\n'


def _repo(tmp_path: Path, crews: dict[str, str], served: list[str]) -> Path:
    root = tmp_path / "repo"
    (root / "crews").mkdir(parents=True)
    for name, body in crews.items():
        (root / "crews" / name).write_text(body, encoding="utf-8")
    (root / ".aimeat").mkdir()
    (root / ".aimeat" / "serve.json").write_text(
        json.dumps({"agents": [{"agent": a, "owner": "o"} for a in served]}), encoding="utf-8"
    )
    return root


def _node(monkeypatch, roster: list[dict], schedules: list[dict]):
    """Stub BOTH node reads: the roster (MCP) and the schedule list (REST)."""
    monkeypatch.setattr(
        "crewaimeat.aimeat_crew._aimeat_call",
        lambda *_a, **_k: {"agents": roster},
    )
    monkeypatch.setattr(
        "crewaimeat.aimeat_crew._aimeat_rest",
        lambda *_a, **_k: {"managed": schedules},
    )


def _sched(agent: str, cron: str = "0 8 * * *", enabled: bool = True, name: str = "S", last: str | None = None) -> dict:
    return {
        "displayName": name,
        "type": "agent_task",
        "agentName": agent,
        "cron": cron,
        "timezone": "Europe/Helsinki",
        "enabled": enabled,
        "lastRunAt": last,
    }


def _run(root: Path) -> Report:
    report = Report()
    liveness.check(gather(root), report)
    return report


def _rules(report: Report) -> set[str]:
    return {f.rule for f in report.findings}


# ── the hard rule: never a false green ──────────────────────────────────────────────────────────
def test_an_unreadable_roster_is_a_finding_not_a_pass(tmp_path, monkeypatch):
    root = _repo(tmp_path, {"a_crew.py": CREW.format(agent="a", extra="")}, served=["a"])
    _node(monkeypatch, roster=[], schedules=[])
    report = _run(root)
    assert "live.unreachable" in _rules(report)
    assert "live" not in report.lenses_run
    assert "live" in report.lenses_skipped


def test_an_unreadable_schedule_list_is_a_finding_too(tmp_path, monkeypatch):
    """The roster read can succeed while the schedule read fails. Reporting only the first would leave
    the schedule half silently unchecked — which is how the first version of this shipped."""
    root = _repo(tmp_path, {"a_crew.py": CREW.format(agent="a", extra="")}, served=["a"])
    monkeypatch.setattr(
        "crewaimeat.aimeat_crew._aimeat_call", lambda *_a, **_k: {"agents": [{"name": "a", "tags": ["x"]}]}
    )
    # None is what a FAILED read looks like — `_aimeat_rest` returns None and logs on an HTTP error.
    # An empty dict/list is a real answer ("no schedules"), which must NOT read as unreachable.
    monkeypatch.setattr("crewaimeat.aimeat_crew._aimeat_rest", lambda *_a, **_k: None)
    report = _run(root)
    assert any(f.rule == "live.unreachable" and "schedules" in f.subject for f in report.findings)


def test_a_node_with_genuinely_no_schedules_is_not_called_unreachable(tmp_path, monkeypatch):
    """The distinction that matters: `live.schedule.missing` is an ERROR, so treating "nothing
    scheduled" as "could not look" would invent an outage on a node that is simply idle."""
    root = _repo(tmp_path, {"a_crew.py": CREW.format(agent="a", extra="")}, served=["a"])
    _node(monkeypatch, [{"name": "a", "tags": ["x"]}], [])
    assert "live.unreachable" not in _rules(_run(root))


# ── schedules: declared vs live ─────────────────────────────────────────────────────────────────
def test_a_schedule_the_repo_never_declared_is_reported(tmp_path, monkeypatch):
    """The node fires an agent and nothing in the code says so — a reader cannot tell what drives it."""
    root = _repo(tmp_path, {"a_crew.py": CREW.format(agent="a", extra="")}, served=["a"])
    _node(monkeypatch, [{"name": "a", "tags": ["x"]}], [_sched("a")])
    assert "live.schedule.undeclared" in _rules(_run(root))


def test_a_matching_declaration_is_silent(tmp_path, monkeypatch):
    decl = 'SCHEDULE = {"cron": "0 8 * * *", "timezone": "Europe/Helsinki", "purpose": "the daily scan"}'
    root = _repo(tmp_path, {"a_crew.py": CREW.format(agent="a", extra=decl)}, served=["a"])
    _node(monkeypatch, [{"name": "a", "tags": ["x"]}], [_sched("a", "0 8 * * *")])
    assert not _rules(_run(root)) - {"live.agent.untagged"}


def test_a_cron_that_drifted_from_its_declaration_is_reported(tmp_path, monkeypatch):
    """The declaration is what a reader will believe. If the node runs something else, one of the two
    is wrong and the difference has to be a decision, not a surprise."""
    decl = 'SCHEDULE = {"cron": "0 17 * * *", "timezone": "Europe/Helsinki"}'
    root = _repo(tmp_path, {"a_crew.py": CREW.format(agent="a", extra=decl)}, served=["a"])
    _node(monkeypatch, [{"name": "a", "tags": ["x"]}], [_sched("a", "0 3 * * *")])
    hits = [f for f in _run(root).findings if f.rule == "live.schedule.drifted"]
    assert hits and "0 17 * * *" in hits[0].message and "0 3 * * *" in hits[0].message


def test_a_declared_trigger_the_node_does_not_have_is_an_ERROR(tmp_path, monkeypatch):
    """The worst case of the three: the crew says it runs every morning and nothing fires it, so the
    thing it produces is simply not being produced — and no error is raised anywhere."""
    decl = 'SCHEDULE = {"cron": "0 8 * * *", "timezone": "Europe/Helsinki", "purpose": "the morning brief"}'
    root = _repo(tmp_path, {"a_crew.py": CREW.format(agent="a", extra=decl)}, served=["a"])
    _node(monkeypatch, [{"name": "a", "tags": ["x"]}], [])
    hits = [f for f in _run(root).findings if f.rule == "live.schedule.missing"]
    assert hits and hits[0].severity == ERROR
    assert "the morning brief" in hits[0].message


def test_a_schedule_firing_at_an_agent_with_no_crew_file_is_reported(tmp_path, monkeypatch):
    root = _repo(tmp_path, {"a_crew.py": CREW.format(agent="a", extra="")}, served=["a"])
    _node(monkeypatch, [{"name": "a", "tags": ["x"]}], [_sched("long-gone", name="Ghost job")])
    hits = [f for f in _run(root).findings if f.rule == "live.schedule.orphan"]
    assert hits and hits[0].severity == WARN and "crewaimeat retire long-gone" in hits[0].fix


def test_an_owner_reminder_is_noted_not_accused(tmp_path, monkeypatch):
    """Four schedules fire at `claude-desktop-home-mcp` — the owner's own Claude Desktop session,
    carrying real procurement-law dates. They are not fleet automation and must not be reported as
    orphaned agents; a check that scolds you for your own calendar gets ignored."""
    root = _repo(tmp_path, {"a_crew.py": CREW.format(agent="a", extra="")}, served=["a"])
    _node(monkeypatch, [{"name": "a", "tags": ["x"]}], [_sched("claude-desktop-home-mcp", name="EU watch")])
    report = _run(root)
    assert "live.schedule.orphan" not in _rules(report)
    assert any("owner reminder" in n for n in report.notes)


def test_a_long_disabled_schedule_is_reported_as_a_leftover(tmp_path, monkeypatch):
    """Six of these sat beside the Sanomat workflow since June because nobody could say what deleting
    one would stop."""
    root = _repo(tmp_path, {"a_crew.py": CREW.format(agent="a", extra="")}, served=["a"])
    _node(
        monkeypatch,
        [{"name": "a", "tags": ["x"]}],
        [_sched("a", enabled=False, name="Old cron", last="2026-01-01T00:00:00Z")],
    )
    assert "live.schedule.abandoned" in _rules(_run(root))


# ── workflows: reported, never demanded ─────────────────────────────────────────────────────────
def test_a_workflow_trigger_is_reported_but_not_demanded_as_a_cron(tmp_path, monkeypatch):
    """A workflow's trigger lives in the node's workflow engine, not in /v1/schedules. Demanding an
    agent_task row for it would manufacture a failure — but saying nothing is what left "which trigger
    drives the paper" unanswerable."""
    root = _repo(tmp_path, {"a_crew.py": CREW.format(agent="a", extra="")}, served=["a"])
    _node(monkeypatch, [{"name": "a", "tags": ["x"]}], [])
    report = _run(root)
    assert "live.schedule.missing" not in _rules(report)
    assert any("workflow" in n and "declares its trigger" in n for n in report.notes), report.notes


# ── the real repo ───────────────────────────────────────────────────────────────────────────────
def test_the_sanomat_workflow_declares_a_trigger():
    """The concrete question this feature was built for. If this ever fails, the evening paper's
    trigger has stopped being written down anywhere."""
    from crewaimeat.workflow_spec import WORKFLOWS

    wf = WORKFLOWS["laimeat-sanomat-evening"]
    assert wf["schedule"]["cron"] and wf["schedule"]["timezone"]
    assert wf["steps"], "a trigger with no steps drives nothing"


@pytest.mark.parametrize("agent", ["some-analyst", "some-listener"])
def test_the_scheduled_crews_declare_what_fires_them(agent):
    from crewaimeat.agent_manifest import manifest_for

    m = manifest_for(agent, Path(__file__).resolve().parent.parent)
    assert m and m.schedule and m.schedule.get("cron"), f"{agent} is fired on a schedule it does not declare"

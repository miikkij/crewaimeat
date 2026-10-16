"""web-researcher: a recurring contract request fires on its period from a task wake, not only on a record event.

What went wrong. The weekly market scan `aimeat-weekly` (status active, period_hours 168) last ran on
2026-09-06. A recurring request becomes due when its period passes, and nothing is written at that
moment, so no record event comes; the crew checked its contracts only on a record event or the catch-up
at connect. A node schedule's task is the clock now: every wake runs the checks (idle_hook, once per
spawned run), and a task marked as a contract check is answered in code.
"""

from __future__ import annotations

import datetime

import pytest

from crewaimeat import market_contract as mc

NOW = datetime.datetime.now(datetime.timezone.utc)


def _request(days_ago: float | None) -> dict:
    rec = {"id": "aimeat-weekly", "segment": "AI-agenttialustat", "status": "active", "period_hours": 168}
    if days_ago is not None:
        rec["last_run"] = (NOW - datetime.timedelta(days=days_ago)).isoformat()
    return rec


class World:
    """One workspace with one market-scan-request; records every node call and every scan run."""

    def __init__(self, req: dict):
        self.req = req
        self.calls: list[tuple[str, dict]] = []
        self.scans: list[str] = []

    def read(self, _call, oid, wid, spaces=()):
        return {"manifest": {"objectTypes": []}, "objects": {mc.IN_SPACE: [dict(self.req)], mc.OUT_SPACE: []}}

    def call(self, tool, payload):
        self.calls.append((tool, payload))
        return {"ok": True}

    def scan(self, segment, *_a, **_k):
        self.scans.append(segment)
        return "## Ketkä täällä pelaavat\nAnalyysi.", ""

    def written(self, space: str) -> list[dict]:
        return [p for t, p in self.calls if t == "aimeat_workspace_write" and p.get("space") == space]


@pytest.fixture
def wr(tmp_path, monkeypatch):
    """The crew module, in a directory of its own (the run marks live under ./logs)."""
    monkeypatch.chdir(tmp_path)
    import crews.web_researcher_crew as mod

    mod._LAST_PASS.clear()
    mc._PROCESSED.clear()
    monkeypatch.setattr("crewaimeat.research_contract.process_research_requests", lambda **kw: {"processed": 0})
    monkeypatch.setattr("crewaimeat.company_contract.process_company_research", lambda **kw: {"processed": 0})
    monkeypatch.setattr("crewaimeat.engagements.engaged_pairs", lambda agent, pairs, contract: pairs)
    monkeypatch.setattr(mc, "member_workspaces", lambda agent: [("org-1", "ws-1")])
    return mod


@pytest.fixture
def world(monkeypatch):
    def make(req: dict) -> World:
        w = World(req)
        monkeypatch.setattr(mc, "read_workspace_objects", w.read)
        monkeypatch.setattr(mc, "_call", w.call)
        monkeypatch.setattr(mc, "run_market_scan", w.scan)
        return w

    return make


SCHEDULED = {"id": "t1", "title": "Weekly contract check", "scope": [{"name": "kind", "value": "contract-check"}]}


def test_a_due_recurring_request_runs_on_a_task_wake(wr, world):
    w = world(_request(days_ago=8))
    out = wr.on_task(SCHEDULED)
    assert w.scans == ["AI-agenttialustat"]
    assert "market-scan: 1 done, 0 failed" in out
    [doc] = w.written(mc.OUT_SPACE)
    assert doc["id"] == f"scan-aimeat-weekly-{mc.today_local()}"
    last = w.written(mc.IN_SPACE)[-1]["value"]
    assert last["status"] == "active" and last["last_run"] > _request(8)["last_run"], "it stays active, run bumped"


def test_a_request_that_is_not_due_does_not_run(wr, world):
    w = world(_request(days_ago=1))
    out = wr.on_task(SCHEDULED)
    assert w.scans == [] and w.written(mc.OUT_SPACE) == []
    assert "market-scan: 0 done, 0 failed" in out


def test_the_machines_own_run_mark_stops_a_double_run_after_a_stale_read(wr, world):
    from crewaimeat.local_marks import mark_local_run

    mark_local_run("market_scan", "aimeat-weekly")  # this machine ran it a moment ago
    w = world(_request(days_ago=8))  # ...and the read still shows the old last_run
    wr.on_task(SCHEDULED)
    assert w.scans == []


def test_a_task_wake_after_the_workers_own_check_does_not_scan_twice(wr, world):
    w = world(_request(days_ago=8))
    wr.clock_pass()  # the idle_hook: run_once.py calls it once before the task
    out = wr.on_task(SCHEDULED)
    assert w.scans == ["AI-agenttialustat"], "one scan for one wake"
    assert "market-scan: 1 done" in out, "the task reports the check that ran for it"


@pytest.mark.parametrize(
    "task",
    [
        {"id": "t2", "title": "contract-check"},
        {"id": "t3", "title": "x", "scope": {"kind": "contract-check"}},
    ],
)
def test_a_contract_check_is_known_by_its_scope_or_its_title(wr, task):
    assert wr.is_contract_check(task)


def test_an_ordinary_research_task_is_left_to_the_research_crew(wr, world):
    w = world(_request(days_ago=8))
    assert wr.on_task({"id": "t4", "title": "Research the Finnish AI agent market"}) is None
    assert w.scans == [], "on_task does nothing for it; the worker's idle_hook is the check"
    adopt = {"id": "t5", "title": "Adopt", "scope": [{"name": "kind", "value": "adopt-contract"}]}
    assert wr.on_task(adopt) is None


def test_the_crew_wires_the_checks_to_every_wake(wr, monkeypatch):
    captured = {}
    monkeypatch.setattr(wr, "run_crew", lambda spec: captured.setdefault("spec", spec))
    wr.run()
    spec = captured["spec"]
    assert spec.on_task is wr.on_task
    assert spec.idle_hook is wr.clock_pass
    assert "records" in tuple(spec.listen_for) and spec.on_record is not None, "a record still wakes it"

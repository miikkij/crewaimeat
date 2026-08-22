"""`crewaimeat costs` — the report exists to answer ONE question, so that is what these pin.

Not "does it total correctly" (the node does the arithmetic) but: **does it correctly identify an
agent that spends without producing anything?** That verdict is the whole product. Getting it wrong in
either direction destroys the report — a false accusation makes people stop reading it, and a missed
one is how crypto-weekly-reporter went on burning calls until it became the node's largest traffic
source.

The node read is stubbed; nothing here touches the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crewaimeat import fleet_economics as fe

CREW = 'AGENT_NAME = "{agent}"\nLLM_PROFILE = "coding"\n\n\ndef build_domain(ctx):\n    return ([], [])\n\n\ndef run():\n    pass\n'


def _repo(tmp_path: Path, crews: dict[str, str], served: list[str]) -> Path:
    root = tmp_path / "repo"
    (root / "crews").mkdir(parents=True)
    for name, body in crews.items():
        (root / "crews" / name).write_text(body, encoding="utf-8")
    (root / ".aimeat").mkdir()
    (root / ".aimeat" / "serve.json").write_text(
        json.dumps({"agents": [{"agent": a, "owner": "o", "token": "SECRET"} for a in served]}), encoding="utf-8"
    )
    return root


def _ledger(monkeypatch, groups: list[dict], totals: dict | None = None):
    """Stub the one node read. Keyed by GAII, exactly as /v1/ledger/usage returns it."""

    def fake_rest(_agent, _method, _path, *a, **k):
        return {"groups": groups, "totals": totals or {}}

    monkeypatch.setattr("crewaimeat.aimeat_crew._aimeat_rest", fake_rest)


def _row(name: str, cost: float, calls: int = 10) -> dict:
    return {"key": f"{name}#owner@node", "calls": calls, "total_tokens": 1000, "cost_usd": cost}


def test_an_agent_with_no_crew_file_is_the_headline(tmp_path, monkeypatch):
    """The crypto-weekly-reporter case: it spends, and there is no code here that could be producing
    what it pays for."""
    root = _repo(tmp_path, {"live_crew.py": CREW.format(agent="live")}, served=["live", "long-gone"])
    _ledger(monkeypatch, [_row("live", 1.0), _row("long-gone", 0.5)])
    rows, _totals, skipped = fe.collect(root, days=30)
    assert skipped is None
    by = {r.agent: r for r in rows}
    assert by["live"].verdict == "ok"
    assert by["long-gone"].wasted and "NO CODE" in by["long-gone"].verdict


def test_a_parked_crew_that_still_spends_is_flagged(tmp_path, monkeypatch):
    """Parking stops the FLEET running it. It does not stop a node-side schedule, so a parked crew can
    keep spending — and that is invisible without this."""
    root = _repo(tmp_path, {"_dormant_crew.py": CREW.format(agent="dormant")}, served=["dormant"])
    _ledger(monkeypatch, [_row("dormant", 0.5)])
    rows, _t, _s = fe.collect(root, days=30)
    assert rows[0].wasted and "PARKED" in rows[0].verdict


def test_an_unregistered_spender_is_flagged(tmp_path, monkeypatch):
    """`probe` has to be a REGISTERED agent — its token is what authorises the ledger read — so the
    unregistered spender is a second crew, which is also the real-world shape of this case."""
    crews = {"probe_crew.py": CREW.format(agent="probe"), "a_crew.py": CREW.format(agent="a")}
    root = _repo(tmp_path, crews, served=["probe"])
    _ledger(monkeypatch, [_row("a", 0.5)])
    rows, _t, _s = fe.collect(root, days=30)
    assert rows[0].agent == "a"
    assert rows[0].wasted and "UNREGISTERED" in rows[0].verdict


def test_the_owner_is_not_accused_of_being_a_broken_agent(tmp_path, monkeypatch):
    """The owner's own GHII appears in the ledger beside the agents — their chat and tool calls. It has
    no '#agent' part and no crew file, so a naive check calls the human "NO CODE", which is nonsense
    and exactly the kind of false positive that gets a report ignored."""
    root = _repo(tmp_path, {"a_crew.py": CREW.format(agent="a")}, served=["a"])
    _ledger(monkeypatch, [_row("a", 1.0), {"key": "owner@node", "calls": 5, "total_tokens": 10, "cost_usd": 0.5}])
    rows, _t, _s = fe.collect(root, days=30)
    owner = next(r for r in rows if r.agent == "owner@node")
    assert owner.is_owner and not owner.wasted


def test_rows_are_ordered_by_cost(tmp_path, monkeypatch):
    root = _repo(tmp_path, {f"{n}_crew.py": CREW.format(agent=n) for n in ("a", "b", "c")}, served=["a", "b", "c"])
    _ledger(monkeypatch, [_row("a", 0.1), _row("c", 5.0), _row("b", 1.0)])
    rows, _t, _s = fe.collect(root, days=30)
    assert [r.agent for r in rows] == ["c", "b", "a"]


def test_the_floor_hides_noise_but_never_a_real_spender(tmp_path, monkeypatch):
    """Two dozen agents that made a couple of onboarding calls bury the six that cost money. The floor
    moves them out of the way — but a WASTING agent with real traffic stays visible regardless."""
    root = _repo(tmp_path, {"a_crew.py": CREW.format(agent="a")}, served=["a", "busy-ghost"])
    _ledger(
        monkeypatch,
        [_row("a", 1.0), _row("trivial", 0.0001, calls=2), _row("busy-ghost", 0.0001, calls=400)],
    )
    rows, _t, _s = fe.collect(root, days=30, min_cost=0.01)
    names = [r.agent for r in rows]
    assert "trivial" not in names, "a near-zero, low-traffic row should be filtered"
    assert "busy-ghost" in names, "a ghost with real traffic must stay visible however little it costs"


def test_an_unreadable_ledger_says_which_read_failed(tmp_path, monkeypatch):
    """Never a confident zero. The first draft reported EVERY empty answer as "the fleet is not
    attached" while the real cause was an unknown tool name — which sends you hunting in the wrong
    place."""
    root = _repo(tmp_path, {"a_crew.py": CREW.format(agent="a")}, served=["a"])
    _ledger(monkeypatch, [])
    rows, _t, skipped = fe.collect(root, days=30)
    assert rows == []
    assert skipped and "/v1/ledger/usage" in skipped


def test_no_registered_agent_means_no_identity_to_ask_with(tmp_path):
    root = _repo(tmp_path, {"a_crew.py": CREW.format(agent="a")}, served=[])
    rows, _t, skipped = fe.collect(root, days=30)
    assert rows == [] and skipped and "serve.json" in skipped


def test_the_summary_names_the_waste_and_what_to_do(tmp_path, monkeypatch):
    root = _repo(tmp_path, {"a_crew.py": CREW.format(agent="a")}, served=["a", "ghost"])
    _ledger(monkeypatch, [_row("a", 3.0), _row("ghost", 1.0)], totals={"cost_usd": 4.0, "calls": 20})
    rows, totals, _s = fe.collect(root, days=30)
    text = fe.render(rows, totals, 30)
    assert "$1.00 (25%)" in text
    assert "ghost" in text and "crewaimeat retire" in text


@pytest.mark.parametrize("days", [7, 30, 90])
def test_the_window_reaches_the_query(tmp_path, monkeypatch, days):
    root = _repo(tmp_path, {"a_crew.py": CREW.format(agent="a")}, served=["a"])
    seen: dict = {}

    def fake_rest(_agent, _method, path, *a, **k):
        seen["path"] = path
        return {"groups": [_row("a", 1.0)]}

    monkeypatch.setattr("crewaimeat.aimeat_crew._aimeat_rest", fake_rest)
    fe.collect(root, days=days)
    assert "group_by=agent" in seen["path"] and "from=" in seen["path"]

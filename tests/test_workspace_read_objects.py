"""The contract modules read a workspace through one reader that opens the records by id
(workspace_tools.read_workspace_objects).

The double answers in the shape MEASURED through the connector's /local/call on 2026-10-12 and
2026-10-13 (npm aimeat 3.26.1; aimeat-protocol 6fc3ef4d5 and later): the read without ids is the
manifest and an INDEX, per object type NAME, of {id, title, updated, version, bytes, published,
has_draft}; the read with ids is `items: [{space, id, value, published, _version, ...}]`. There is no
`objects` in either. Offline: no node, no mail, no model.
"""

from __future__ import annotations

import datetime

import pytest

from crewaimeat import mail_contract as mc
from crewaimeat import workspace_tools as wt


class Workspace:
    """One workspace as the connector answers for it."""

    def __init__(self, records: dict[str, dict[str, dict]], *, rows_space: str | None = None) -> None:
        self.records = records  # {space name: {id: {"value": {...}, "published": bool}}}
        self.rows_space = rows_space
        self.calls: list[dict] = []
        self.fail_ids = False
        self.fail_index = False

    def __call__(self, tool: str, payload: dict, **_kw):
        if tool != "aimeat_workspace_read":
            return {"ok": True}  # a write or a publish
        self.calls.append(payload)
        if "ids" not in payload:
            if self.fail_index:
                return None
            types = [{"name": name, "namespace": f"shared.{name}", "mode": "records"} for name in self.records]
            if self.rows_space:
                types.append({"name": self.rows_space, "namespace": f"rows.{self.rows_space}", "backing": "rows"})
            index = {
                name: [
                    {"id": rid, "title": rid, "updated": "2026-10-13T04:00:00.000Z", "version": 1, "bytes": 80,
                     "published": rec.get("published", True), "has_draft": not rec.get("published", True)}
                    for rid, rec in recs.items()
                ]
                for name, recs in self.records.items()
            }  # fmt: skip
            return {"mode": "index", "manifest": {"name": "W", "objectTypes": types}, "index": index, "counts": {}}
        if self.fail_ids:
            return None
        space = payload["space"]
        items = [
            {"space": space, "id": rid, "value": self.records[space][rid]["value"],
             "published": self.records[space][rid].get("published", True), "_version": 1}
            for rid in payload["ids"]
            if rid in self.records.get(space, {})
        ]  # fmt: skip
        return {"mode": "content", "items": items}


def _ws(**spaces) -> Workspace:
    return Workspace(
        {name.replace("_", "-"): {rid: {"value": v} for rid, v in recs.items()} for name, recs in spaces.items()}
    )


# ── the reader ───────────────────────────────────────────────────────────────
def test_the_records_come_back_as_objects_each_with_its_id():
    ws = _ws(mail_request={"m-1": {"subject": "A", "status": "requested"}, "m-2": {"id": "m-2", "subject": "B"}})

    data = wt.read_workspace_objects(ws, "org", "w")

    assert data["objects"] == {
        "mail-request": [{"subject": "A", "status": "requested", "id": "m-1"}, {"id": "m-2", "subject": "B"}]
    }
    assert data["manifest"]["name"] == "W" and "index" in data, "the node's own answer is still there"
    assert [("ids" in c, c.get("space")) for c in ws.calls] == [(False, None), (True, "mail-request")]


def test_only_the_asked_spaces_are_opened_and_every_declared_space_still_has_a_key():
    ws = _ws(request={"r-1": {"q": "x"}}, result={"r-0": {"a": "y"}}, note={"n-1": {"t": "z"}})

    data = wt.read_workspace_objects(ws, "org", "w", spaces=("request",))

    assert data["objects"] == {"request": [{"q": "x", "id": "r-1"}], "result": [], "note": []}
    assert [c["space"] for c in ws.calls if "ids" in c] == ["request"]


def test_asking_for_no_space_opens_no_record():
    """Enough for "does the workspace declare this space" and for ids, which the index has."""
    ws = _ws(request={"r-1": {"q": "x"}})
    data = wt.read_workspace_objects(ws, "org", "w", spaces=())
    assert data["objects"] == {"request": []} and len(ws.calls) == 1
    assert wt.published_ids(data, "request") == {"r-1"}


def test_a_draft_that_was_never_published_is_not_a_record_yet():
    ws = Workspace({"request": {"r-1": {"value": {"q": "x"}}, "r-2": {"value": {"q": "draft"}, "published": False}}})
    data = wt.read_workspace_objects(ws, "org", "w")
    assert [r["id"] for r in data["objects"]["request"]] == ["r-1"]
    assert wt.published_ids(data, "request") == {"r-1"}
    assert ws.calls[1]["ids"] == ["r-1"], "an unpublished record is not asked for"


def test_many_records_are_opened_in_batches():
    ws = _ws(request={f"r-{i:03d}": {"n": i} for i in range(120)})
    data = wt.read_workspace_objects(ws, "org", "w")
    assert len(data["objects"]["request"]) == 120
    assert [len(c["ids"]) for c in ws.calls if "ids" in c] == [50, 50, 20]


def test_a_row_space_is_not_opened_as_records():
    ws = Workspace({"request": {"r-1": {"value": {"q": "x"}}}}, rows_space="mail")
    data = wt.read_workspace_objects(ws, "org", "w")
    assert data["objects"] == {"request": [{"q": "x", "id": "r-1"}], "mail": []}


def test_records_that_could_not_be_read_make_the_whole_workspace_not_read(capsys):
    """Half a list taken for the whole is how a re-scan writes again what it could not see."""
    ws = _ws(request={"r-1": {"q": "x"}})
    ws.fail_ids = True
    assert wt.read_workspace_objects(ws, "org", "w") is None
    assert "could not be read" in capsys.readouterr().err


def test_a_workspace_that_could_not_be_read_is_still_none():
    ws = _ws(request={})
    ws.fail_index = True
    assert wt.read_workspace_objects(ws, "org", "w") is None


def test_an_answer_that_still_carries_objects_is_returned_as_it_came():
    """A connector older than the change, and every test double written for it."""
    old = {"manifest": {}, "objects": {"request": [{"id": "r-1"}]}}
    assert wt.read_workspace_objects(lambda tool, payload: old, "org", "w") is old
    assert wt.published_ids(old, "request") == {"r-1"}


# ── the morning report is due once ───────────────────────────────────────────
SEVEN = datetime.datetime(2026, 10, 13, 7, 4, tzinfo=mc._TZ)


def _due(monkeypatch, ws) -> bool:
    monkeypatch.setattr(mc, "_call", ws)
    return mc.morning_report_due(SEVEN)


def test_the_report_is_due_when_today_s_record_is_not_there(monkeypatch):
    assert _due(monkeypatch, _ws(mail_request={"morning-2026-10-12": {"subject": "y"}})) is True


def test_the_report_is_not_due_a_second_time(monkeypatch):
    """Measured on production: from 2026-10-10 the report went out twice in every run, because this
    check looked for today's record in a field the read no longer had."""
    ws = _ws(mail_request={"morning-2026-10-13": {"subject": "today"}, "morning-2026-10-12": {"subject": "y"}})
    assert _due(monkeypatch, ws) is False
    assert all("ids" not in c for c in ws.calls), "its id in the index is enough; no record is opened"


def test_a_report_that_was_written_and_never_published_is_still_due(monkeypatch):
    ws = Workspace({"mail-request": {"morning-2026-10-13": {"value": {"subject": "today"}, "published": False}}})
    assert _due(monkeypatch, ws) is True


def test_a_read_that_fails_costs_a_second_mail_at_worst_never_the_first(monkeypatch):
    ws = _ws(mail_request={"morning-2026-10-13": {"subject": "today"}})
    ws.fail_index = True
    assert _due(monkeypatch, ws) is True


def test_outside_the_morning_window_nothing_is_read(monkeypatch):
    ws = _ws(mail_request={})
    monkeypatch.setattr(mc, "_call", ws)
    assert mc.morning_report_due(SEVEN.replace(hour=9)) is False and ws.calls == []


# ── a contract scan sees its requests again ──────────────────────────────────
def test_the_mail_sweep_sends_a_request_it_can_see_again(monkeypatch):
    """By the code, no mail request published since the read changed was seen. This is the sweep on the
    measured shape: one requested record, one mail, and a record already sent is left alone."""
    ws = _ws(mail_request={
        "m-1": {"subject": "Tarjous", "body_md": "Hei", "status": "requested"},
        "m-0": {"subject": "Vanha", "body_md": "x", "status": "sent"},
    })  # fmt: skip
    sends: list = []
    monkeypatch.setattr(mc, "_call", ws)
    monkeypatch.setattr(mc, "send_mail", lambda *a, **k: sends.append(a) or None)
    monkeypatch.setattr(mc, "last_local_run", lambda name, rid: None)
    monkeypatch.setattr(mc, "mark_local_run", lambda name, rid: None)
    mc._PROCESSED.clear()

    result = mc.process_mail(targets=[("org", "w")])

    assert result["sent"] == 1 and len(sends) == 1
    assert [c["space"] for c in ws.calls if "ids" in c] == ["mail-request"]


@pytest.mark.parametrize(
    "module, space",
    [("research_contract", "IN_SPACE"), ("market_contract", "IN_SPACE"), ("company_contract", "IN_SPACE")],
)
def test_each_contract_module_reads_through_the_one_reader(module, space):
    """No module reads `objects` off the read without ids any more: each goes through the reader."""
    import importlib
    import inspect

    src = inspect.getsource(importlib.import_module(f"crewaimeat.{module}"))
    assert '_call("aimeat_workspace_read", {"organism_id"' not in src and "read_workspace_objects(_call" in src

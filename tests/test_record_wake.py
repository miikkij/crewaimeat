"""A published record becomes a task (crewaimeat.record_wake).

The node double answers in the shapes MEASURED on a local node on 2026-10-12 (aimeat-protocol
dbd1ad16a, connector 3.26.1): the workspace read without ids is the index (`manifest`, and `index`
per object type NAME with {id, title, updated, version, bytes, published, has_draft}); with ids it is
`items: [{space, id, value, published, _version, _createdAt, _updatedAt, ai_provenance?}]`; a task
created by the agent for itself answers `{task: {id, status}, ...}` and a repeat of a live one adds
`deduplicated`. Offline: no node, no model.
"""

from __future__ import annotations

import pytest

from crewaimeat import record_wake as rw

ME = "support-drafts#owner@node"
SPACE = {"organism_id": "org-1", "ws": "ws-p7", "space": "shared.support-requests"}
KEY = "org-1/ws-p7/shared.support-requests"


def _row(rid, updated, version=1, published=True):
    return {
        "id": rid,
        "title": rid,
        "updated": updated,
        "version": version,
        "bytes": 90,
        "published": published,
        "has_draft": False,
    }


class Node:
    """One workspace with a `requests` space, this agent's memo key, and its task list."""

    def __init__(self):
        self.records: dict[str, dict] = {}  # id -> {value, version, updated, published, writer}
        self.memo = None
        self.tasks: list[dict] = []
        self.refuse: dict[str, dict] = {}  # tool or "tasks" -> the node's error
        self.calls: list[str] = []

    def put(self, rid, updated, *, version=1, writer=None, published=True, value=None):
        self.records[rid] = {
            "created": self.records.get(rid, {}).get("created", updated),
            "value": value or {"id": rid, "subject": f"about {rid}"},
            "version": version,
            "updated": updated,
            "published": published,
            "writer": writer,
        }

    def _err(self, what):
        return {"ok": False, "error": self.refuse[what]}

    def call(self, agent, tool, payload, **_kw):
        self.calls.append(tool)
        if tool in self.refuse:
            return self._err(tool)
        if tool == "aimeat_memory_read":
            assert payload == {"key": "crews.records.support-drafts", "owner_scope": True}
            if self.memo is None:
                return {"ok": False, "error": {"code": "NOT_FOUND", "message": "no such key"}}
            return {"key": payload["key"], "value": self.memo}
        if tool == "aimeat_memory_write":
            assert payload["key"] == "crews.records.support-drafts" and payload["visibility"] == "owner"
            self.memo = payload["value"]
            return {"key": payload["key"], "version": 1}
        assert tool == "aimeat_workspace_read" and payload["organism_id"] == "org-1" and payload["ws"] == "ws-p7"
        if "ids" not in payload:
            rows = [_row(i, r["updated"], r["version"], r["published"]) for i, r in self.records.items()]
            return {
                "mode": "index",
                "manifest": {
                    "name": "Support",
                    "objectTypes": [
                        {"name": "requests", "namespace": "shared.support-requests", "mode": "records"},
                        {"name": "drafts", "namespace": "shared.support-drafts", "mode": "records"},
                    ],
                },
                "index": {"requests": rows, "drafts": []},
            }
        assert payload["space"] == "requests"
        items = []
        for rid in payload["ids"]:
            r = self.records.get(rid)
            if r is None:
                continue
            item = {
                "space": "requests", "id": rid, "value": r["value"], "published": r["published"],
                "_version": r["version"], "_createdAt": r["created"], "_updatedAt": r["updated"],
            }  # fmt: skip
            if r["writer"]:
                item["ai_provenance"] = {"id": "p-1", "record": {"generator": {"principal": r["writer"]}}}
            items.append(item)
        return {"mode": "content", "items": items}

    def rest(self, agent, method, path, body=None, **_kw):
        self.calls.append(f"{method} {path}")
        assert (method, path) == ("POST", "/v1/agents/support-drafts/tasks"), "addressed by NAME, never the identity"
        if "tasks" in self.refuse:
            return self._err("tasks")
        live = next((t for t in self.tasks if t["idempotency_key"] == body["idempotency_key"]), None)
        if live:
            return {"task": {"id": live["id"], "status": "active"}, "deduplicated": True}
        self.tasks.append({**body, "id": f"task-{len(self.tasks) + 1}"})
        return {"task": {"id": self.tasks[-1]["id"], "status": "active"}, "start": {"policy": "automatic"}}


@pytest.fixture
def node(monkeypatch):
    n = Node()
    monkeypatch.setattr("crewaimeat.memory_tools._aimeat_call", n.call)
    return n


def _wake(node, said=None):
    return rw.RecordWake(ME, call=node.call, rest=node.rest, say=(said.append if said is not None else lambda _l: None))


# ── which records are new: the pure half ─────────────────────────────────────
def test_a_first_look_takes_what_is_there_as_handled():
    rows = [_row("a", "2026-10-12T10:00:00.000Z"), _row("b", "2026-10-12T15:00:00.000Z")]
    fresh, memo = rw.new_records(rows, None)
    assert fresh == [] and memo == {"mark": "2026-10-12T15:00:00.000Z", "recent": {"b": "2026-10-12T15:00:00.000Z"}}


def test_a_first_look_at_an_empty_space_then_takes_everything_that_comes():
    fresh, memo = rw.new_records([], None)
    assert fresh == [] and memo == {"mark": "", "recent": {}}
    fresh, _ = rw.new_records([_row("a", "2026-10-12T10:00:00.000Z")], memo)
    assert [e["id"] for e in fresh] == ["a"]


def test_records_after_the_mark_are_new_oldest_first():
    memo = {"mark": "2026-10-12T15:00:00.000Z", "recent": {"b": "2026-10-12T15:00:00.000Z"}}
    rows = [
        _row("d", "2026-10-12T15:05:00.000Z"),
        _row("b", "2026-10-12T15:00:00.000Z"),
        _row("c", "2026-10-12T15:01:00.000Z"),
    ]
    fresh, _ = rw.new_records(rows, memo)
    assert [e["id"] for e in fresh] == ["c", "d"]


def test_a_publish_that_did_not_move_the_version_number_is_still_a_publish():
    """Measured: a record first written as a bare key and then published through the workspace kept
    `version: 1` in the index while its `updated` moved."""
    memo = {"mark": "2026-10-12T16:03:43.296Z", "recent": {"req-5": "2026-10-12T16:03:43.296Z"}}
    fresh, _ = rw.new_records([_row("req-5", "2026-10-12T16:04:33.317Z", version=1)], memo)
    assert [e["id"] for e in fresh] == ["req-5"]


def test_a_handled_record_published_again_is_new_again():
    memo = {"mark": "2026-10-12T15:00:00.000Z", "recent": {"b": "2026-10-12T15:00:00.000Z"}}
    fresh, _ = rw.new_records([_row("b", "2026-10-12T15:02:00.000Z", version=2)], memo)
    assert [(e["id"], e["version"]) for e in fresh] == [("b", 2)]


def test_a_draft_is_not_work():
    memo = {"mark": "2026-10-12T15:00:00.000Z", "recent": {}}
    fresh, _ = rw.new_records([_row("c", "2026-10-12T15:02:00.000Z", published=False)], memo)
    assert fresh == []


def test_a_record_stamped_just_before_the_mark_and_seen_after_it_is_still_new():
    """Two writes a moment apart can become visible in the other order."""
    memo = {"mark": "2026-10-12T15:00:10.000Z", "recent": {"b": "2026-10-12T15:00:10.000Z"}}
    rows = [_row("b", "2026-10-12T15:00:10.000Z"), _row("late", "2026-10-12T15:00:09.500Z")]
    fresh, _ = rw.new_records(rows, memo)
    assert [e["id"] for e in fresh] == ["late"]


def test_old_records_are_never_new_however_the_memo_looks():
    memo = {"mark": "2026-10-12T15:00:00.000Z", "recent": {}}
    fresh, _ = rw.new_records([_row("old", "2026-10-11T09:00:00.000Z")], memo)
    assert fresh == []


def test_handling_moves_the_mark_and_forgets_ids_far_behind_it():
    memo = rw.handled(
        {"mark": "2026-10-12T15:00:00.000Z", "recent": {"a": "2026-10-12T15:00:00.000Z"}},
        "z",
        "2026-10-12T15:10:00.000Z",
    )
    assert memo == {"mark": "2026-10-12T15:10:00.000Z", "recent": {"z": "2026-10-12T15:10:00.000Z"}}


# ── the scan against the node ────────────────────────────────────────────────
def test_the_first_catch_up_makes_no_task_and_writes_the_memo(node):
    node.put("old-1", "2026-10-11T09:00:00.000Z")
    node.put("old-2", "2026-10-12T09:00:00.000Z")
    said: list = []

    done = _wake(node, said).on_event({"type": "workspace.record", **SPACE, "id": None, "op": "catchup"}, [SPACE])

    assert done == [] and node.tasks == []
    assert node.memo == {
        "spaces": {KEY: {"mark": "2026-10-12T09:00:00.000Z", "recent": {"old-2": "2026-10-12T09:00:00.000Z"}}}
    }
    assert "first look" in said[0]


def test_a_record_published_after_the_first_look_becomes_one_task(node):
    node.put("old", "2026-10-12T09:00:00.000Z")
    wake = _wake(node)
    wake.scan(SPACE)
    node.put(
        "req-2", "2026-10-12T15:43:14.028Z", value={"id": "req-2", "subject": "Salasana", "body": "En pääse sisään."}
    )

    done = wake.on_event({"type": "workspace.record", **SPACE, "id": "req-2", "op": "created"}, [SPACE])

    assert done == [{"id": "req-2", "updated": "2026-10-12T15:43:14.028Z", "task": "task-1"}]
    task = node.tasks[0]
    assert task["title"] == "Workspace record created: requests/req-2"
    assert task["idempotency_key"] == "record:org-1/ws-p7/shared.support-requests/req-2@2026-10-12T15:43:14.028Z"
    assert task["description"] == (
        "A record was created in a workspace space this agent watches.\n\n"
        "workspace: Support (id ws-p7)\n"
        "organism: org-1\n"
        "space: requests (namespace shared.support-requests)\n"
        "record id: req-2\n"
        "record version: 1, published 2026-10-12T15:43:14.028Z\n\n"
        "The record as published:\n"
        '{\n "id": "req-2",\n "subject": "Salasana",\n "body": "En pääse sisään."\n}\n\n'
        'To read or write in this workspace with your tools, name it by its id: workspace="ws-p7", '
        'organism="org-1". A space is named by its name, for example space="requests".'
    )


def test_the_same_event_twice_makes_one_task(node):
    """A worker's start is a catch-up of each space, and the event that started it is still queued."""
    wake = _wake(node)
    wake.scan(SPACE)
    node.put("req-2", "2026-10-12T15:43:14.028Z")
    event = {"type": "workspace.record", **SPACE, "id": "req-2", "op": "created"}

    wake.on_event({"type": "workspace.record", **SPACE, "id": None, "op": "catchup"}, [SPACE])
    again = wake.on_event(event, [SPACE])

    assert len(node.tasks) == 1 and again == []


def test_an_event_for_one_record_also_finds_the_others_nobody_announced(node):
    wake = _wake(node)
    wake.scan(SPACE)
    node.put("missed", "2026-10-12T15:40:00.000Z")
    node.put("req-2", "2026-10-12T15:43:14.028Z")

    wake.on_event({"type": "workspace.record", **SPACE, "id": "req-2", "op": "created"}, [SPACE])

    assert [t["title"].rsplit("/", 1)[1] for t in node.tasks] == ["missed", "req-2"]


def test_a_real_event_on_the_first_look_handles_the_record_it_names_and_no_other(node):
    """The memo may be gone (a key deleted by hand); the record a push names was published while this
    agent was subscribed, so it is work, and the rest of the space is still not gone back through."""
    node.put("old", "2026-10-11T09:00:00.000Z")
    node.put("req-9", "2026-10-12T15:43:14.028Z")

    _wake(node).on_event({"type": "workspace.record", **SPACE, "id": "req-9", "op": "created"}, [SPACE])

    assert [t["idempotency_key"].rsplit("/", 1)[1] for t in node.tasks] == ["req-9@2026-10-12T15:43:14.028Z"]


def test_an_updated_record_is_a_task_that_says_updated(node):
    wake = _wake(node)
    node.put("req-2", "2026-10-12T15:00:00.000Z")
    wake.scan(SPACE)
    node.put("req-2", "2026-10-12T15:50:00.000Z", version=2)

    wake.on_event({"type": "workspace.record", **SPACE, "id": "req-2", "op": "updated"}, [SPACE])

    assert node.tasks[0]["title"] == "Workspace record updated: requests/req-2"
    assert node.tasks[0]["idempotency_key"].endswith("req-2@2026-10-12T15:50:00.000Z")


def test_the_agents_own_write_makes_no_task_and_is_not_looked_at_again(node):
    """The node's event names no author and is pushed to the writer too."""
    wake = _wake(node)
    node.put("req-2", "2026-10-12T15:49:00.000Z")
    wake.scan(SPACE)
    node.put("req-2", "2026-10-12T15:50:00.000Z", version=1, writer=ME)  # as measured: the number stayed
    said: list = []
    wake._say = said.append

    done = wake.on_event({"type": "workspace.record", **SPACE, "id": "req-2", "op": "updated"}, [SPACE])
    again = wake.scan(SPACE)

    assert done == [{"id": "req-2", "updated": "2026-10-12T15:50:00.000Z", "skipped": "own write"}] and again == []
    assert node.tasks == [] and "own write" in said[0]


def test_another_agents_write_is_work(node):
    wake = _wake(node)
    wake.scan(SPACE)
    node.put("req-2", "2026-10-12T15:50:00.000Z", writer="support-drafts#someone-else@node")

    wake.scan(SPACE)

    assert len(node.tasks) == 1, "the same NAME under another owner is another agent"


def test_a_bare_name_reads_its_identity_from_its_credential_not_from_the_name(node, monkeypatch):
    import aimeat_crewai.daemon as pkg

    monkeypatch.setattr(pkg, "resolve_agent_identity", lambda name: type("I", (), {"gaii": ME})())
    wake = rw.RecordWake("support-drafts", call=node.call, rest=node.rest, say=lambda _l: None)
    wake.scan(SPACE)
    node.put("req-2", "2026-10-12T15:50:00.000Z", writer=ME)

    assert wake.scan(SPACE) == [{"id": "req-2", "updated": "2026-10-12T15:50:00.000Z", "skipped": "own write"}]


def test_a_record_gone_between_the_index_and_the_read_makes_no_task(node):
    wake = _wake(node)
    wake.scan(SPACE)
    node.put("req-2", "2026-10-12T15:50:00.000Z")
    real = node.call

    def _vanish(agent, tool, payload, **kw):
        if tool == "aimeat_workspace_read" and "ids" in payload:
            return {"mode": "content", "items": []}
        return real(agent, tool, payload, **kw)

    wake._call = _vanish

    assert wake.scan(SPACE) == [{"id": "req-2", "updated": "2026-10-12T15:50:00.000Z", "skipped": "gone"}]


def test_a_task_the_node_already_has_open_is_said_and_not_made_twice(node):
    wake = _wake(node)
    wake.scan(SPACE)
    node.put("req-2", "2026-10-12T15:50:00.000Z")
    wake.scan(SPACE)
    node.memo = {"spaces": {KEY: {"mark": "2026-10-12T15:00:00.000Z", "recent": {}}}}  # the memo write was lost
    said: list = []
    wake._say = said.append

    wake.scan(SPACE)

    assert len(node.tasks) == 1 and "already had it open" in said[0]


# ── what could not be done is said, and nothing is assumed ───────────────────
def test_a_memo_the_node_refuses_to_show_is_not_an_empty_memo(node):
    """Read as empty, every scan would be a first look, and no record would ever become a task."""
    from crewaimeat.memory_tools import MemoryReadRefused

    node.refuse["aimeat_memory_read"] = {"code": "SCOPE_DENIED", "message": "Requires memory:read"}
    with pytest.raises(MemoryReadRefused):
        _wake(node).scan(SPACE)
    assert node.tasks == [] and node.memo is None


def test_a_workspace_the_node_refuses_names_the_node_s_words(node):
    node.refuse["aimeat_workspace_read"] = {"code": "SCOPE_DENIED", "message": "Requires organism:read"}
    with pytest.raises(rw.RecordWakeError, match=r"SCOPE_DENIED.*organism:read") as caught:
        _wake(node).scan(SPACE)
    assert caught.value.refused and node.memo is None, "no memo is written over a space that was not read"


def test_a_namespace_the_workspace_does_not_have_names_the_ones_it_has(node):
    with pytest.raises(rw.RecordWakeError, match=r"shared\.nope.*shared\.support-requests, shared\.support-drafts"):
        _wake(node).scan({**SPACE, "space": "shared.nope"})


def test_a_task_the_node_refuses_keeps_the_record_for_the_next_scan(node):
    wake = _wake(node)
    wake.scan(SPACE)
    node.put("first", "2026-10-12T15:50:00.000Z")
    node.put("second", "2026-10-12T15:51:00.000Z")
    node.refuse["tasks"] = {"code": "AGENT_STOPPED", "message": "The agent is stopped"}

    with pytest.raises(rw.RecordWakeError, match="AGENT_STOPPED"):
        wake.scan(SPACE)
    del node.refuse["tasks"]
    done = wake.scan(SPACE)

    assert [d["id"] for d in done] == ["first", "second"] and len(node.tasks) == 2


def test_the_records_handled_before_a_failure_are_remembered(node):
    wake = _wake(node)
    wake.scan(SPACE)
    node.put("first", "2026-10-12T15:50:00.000Z")
    node.put("second", "2026-10-12T15:51:00.000Z")
    real = node.rest

    def _second_fails(agent, method, path, body=None, **kw):
        if "second" in body["title"]:
            return None  # no answer
        return real(agent, method, path, body, **kw)

    wake._rest = _second_fails
    with pytest.raises(rw.RecordWakeError, match="did not answer") as caught:
        wake.scan(SPACE)
    assert not caught.value.refused
    wake._rest = real

    done = wake.scan(SPACE)

    assert [d["id"] for d in done] == ["second"] and [t["title"][-5:] for t in node.tasks] == ["first", "econd"]


def test_an_event_for_a_space_the_definition_does_not_name_is_left_alone(node):
    said: list = []
    other = {**SPACE, "space": "shared.support-drafts"}
    assert (
        _wake(node, said).on_event({"type": "workspace.record", **other, "id": "d-1", "op": "created"}, [SPACE]) == []
    )
    assert node.calls == [] and "does not watch" in said[0]

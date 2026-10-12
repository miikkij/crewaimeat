"""A crew definition names the workspace spaces whose records wake its agent (`record_spaces`).

The field has the items of `CrewSpec.record_spaces` and of the connector's `/local/subscribe`, and it
is written by the node's new-agent page, so its shape is a contract between two repositories: these
tests are the crewaimeat half of it. Offline: no node, no model.
"""

from __future__ import annotations

import pytest

from crewaimeat import json_agent as ja
from crewaimeat.crew_def import CONTRACT_TAG, CrewDocError, crewspec_from_json, validate_crew_doc

SPACE = {
    "organism_id": "842662da-1201-41bf-938b-e94557c2a1f0",
    "ws": "ws-mv5abl2c06x",
    "space": "shared.support-requests",
}


def _doc(**over) -> dict:
    doc = {
        "agent_name": "support-drafts",
        "listen_for": ["tasks", "records"],
        "record_spaces": [dict(SPACE)],
        "agents": [{"name": "w", "role": "Drafter", "goal": "Draft", "backstory": "You draft."}],
        "tasks": [{"id": "t", "agent": "w", "description": "Do:\n{{ctx.prompt}}", "expected_output": "A draft."}],
    }
    doc.update(over)
    return {k: v for k, v in doc.items() if v is not None}


# ── the definition ───────────────────────────────────────────────────────────
def test_a_definition_with_record_spaces_builds_a_spec_that_carries_them():
    spec = crewspec_from_json(_doc())
    assert spec.record_spaces == [SPACE]
    assert tuple(spec.listen_for) == ("tasks", "records")


def test_only_the_three_keys_reach_the_daemon():
    """The page may keep its own notes beside an item (a name to show); the subscribe body takes three."""
    spec = crewspec_from_json(_doc(record_spaces=[{**SPACE, "label": "Support requests"}]))
    assert spec.record_spaces == [SPACE]


def test_a_definition_without_the_field_is_as_before():
    spec = crewspec_from_json(_doc(record_spaces=None, listen_for=["tasks"]))
    assert spec.record_spaces is None


def test_records_without_record_spaces_is_refused_and_names_the_field():
    errors = validate_crew_doc(_doc(record_spaces=None))
    assert len(errors) == 1 and errors[0].startswith('listen_for: "records" needs `record_spaces`'), errors


def test_record_spaces_without_records_is_refused():
    errors = validate_crew_doc(_doc(listen_for=["tasks"]))
    assert errors == [
        'record_spaces: needs "records" in `listen_for` (they are what the agent is woken by); write ["tasks", "records"]'
    ]


def test_record_spaces_with_no_listen_for_at_all_is_refused():
    """The default is tasks only, so a definition that names spaces and says nothing would look
    subscribed and never be woken."""
    errors = validate_crew_doc(_doc(listen_for=None))
    assert len(errors) == 1 and '"records"' in errors[0], errors


def test_record_spaces_without_tasks_is_refused():
    """A record reaches the crew as a task (crewaimeat.record_wake)."""
    errors = validate_crew_doc(_doc(listen_for=["records"]))
    assert errors == [
        'record_spaces: needs "tasks" in `listen_for` (each record becomes a task it runs); write ["tasks", "records"]'
    ]


@pytest.mark.parametrize("bad", [[], "shared.requests", [dict(SPACE)] * 21, {"organism_id": "o"}])
def test_the_list_is_one_to_twenty_objects(bad):
    errors = validate_crew_doc(_doc(record_spaces=bad))
    assert errors == ['record_spaces: must be a list of 1 to 20 {"organism_id", "ws", "space"} objects'], errors


def test_an_item_missing_a_key_is_named_by_index_and_key():
    errors = validate_crew_doc(_doc(record_spaces=[dict(SPACE), {"organism_id": "o", "ws": " ", "space": 7}, "x"]))
    assert errors == [
        "record_spaces[1]: ws, space must be a non-empty string",
        "record_spaces[2]: organism_id, ws, space must be a non-empty string",
    ]


def test_an_item_may_ask_for_the_records_already_there():
    """The owner's tick on the new-agent page. The daemon still subscribes with the three keys."""
    doc = _doc(record_spaces=[{**SPACE, "backfill": True}])
    assert validate_crew_doc(doc) == []
    assert crewspec_from_json(doc).record_spaces == [SPACE]


@pytest.mark.parametrize("bad", ["true", "false", 1, None])
def test_backfill_is_a_boolean_or_it_is_refused(bad):
    """A string "false" would read as true, and then every old record becomes a task."""
    errors = validate_crew_doc(_doc(record_spaces=[{**SPACE, "backfill": bad}]))
    assert errors == ["record_spaces[0]: backfill must be true or false"]


def test_an_invalid_field_stops_the_spec_from_being_built():
    with pytest.raises(CrewDocError):
        crewspec_from_json(_doc(listen_for=["tasks"]))


# ── the tag ──────────────────────────────────────────────────────────────────
class _Spec:
    agent_name = "support-drafts#owner@node"

    def __init__(self, tags):
        self.tags = tags


def _sync(tags, *, watched, held):
    written: list = []
    spec = _Spec(tags)
    said = ja.sync_contract_tag(
        spec, watched=watched, held=lambda _a: held, write=lambda _a, t: written.append(list(t)) or "written"
    )
    return said, spec.tags, written


def test_a_definition_with_its_own_tags_gets_the_contract_tag_among_them():
    said, tags, written = _sync(["support"], watched=True, held=["other"])
    assert tags == ["support", CONTRACT_TAG] and written == [], "run_crew pushes the definition's whole set"
    assert said == "in the definition's tags"


def test_a_tag_the_author_wrote_is_not_written_twice():
    _said, tags, _written = _sync(["support", CONTRACT_TAG], watched=True, held=[])
    assert tags == ["support", CONTRACT_TAG]


def test_a_definition_with_no_tags_keeps_what_the_node_holds_and_adds_one():
    """The node's tags may be the owner's or an app's: a role tag is how an app finds its agent."""
    said, tags, written = _sync(None, watched=True, held=["role:tvopas-recorder"])
    assert tags is None, "the definition still declares none, so run_crew pushes none"
    assert written == [["role:tvopas-recorder", CONTRACT_TAG]] and said == "added"


def test_the_tag_already_there_is_not_written_again():
    said, _tags, written = _sync(None, watched=True, held=[CONTRACT_TAG, "x"])
    assert said == "unchanged" and written == []


def test_nothing_is_ever_taken_away_and_an_agent_that_watches_nothing_is_not_even_asked_about():
    """A tag on the node may be its owner's; and every node-defined agent starts through here."""
    asked: list = []
    spec = _Spec(None)
    said = ja.sync_contract_tag(
        spec, watched=False, held=lambda a: asked.append(a) or ["x", CONTRACT_TAG], write=lambda *a: asked.append(a)
    )
    assert said == "not watched" and asked == [] and spec.tags is None

    _said, tags, _written = _sync(["support", CONTRACT_TAG], watched=False, held=[])
    assert tags == ["support", CONTRACT_TAG], "the author's own tags are the author's"


def test_a_node_that_does_not_say_its_tags_gets_nothing_written():
    said, _tags, written = _sync(None, watched=True, held=None)
    assert said == "unknown" and written == []


# ── the handler run_json_agent installs ──────────────────────────────────────
@pytest.fixture
def spec(monkeypatch):
    """`run_json_agent` up to the point it hands the spec to the daemon, for a definition that watches."""
    env = {"version": 1, "publishedAt": "2026-10-12T00:00:00Z", "agent_name": "support-drafts", "doc": _doc()}
    monkeypatch.setattr(ja, "read_owner_key_strict", lambda agent, key, **_kw: env)
    monkeypatch.setattr("crewaimeat.aimeat_crew._aimeat_call", lambda a, tool, payload, **_kw: {"ok": True})
    monkeypatch.setattr(ja, "sync_contract_tag", lambda spec, **_kw: "unchanged")
    captured: dict = {}
    monkeypatch.setattr("crewaimeat.aimeat_crew.run_crew", lambda spec: captured.setdefault("spec", spec))
    ja.run_json_agent("support-drafts")
    return captured["spec"]


def test_a_workspace_record_event_reaches_the_scan(spec, monkeypatch):
    """Without this branch the field subscribes the spaces and the handler drops every wake they send:
    run_json_agent's own on_record took only `crew.def_updated`."""
    seen: list = []
    monkeypatch.setattr(
        "crewaimeat.record_wake.RecordWake.on_event", lambda self, event, watched: seen.append((event, watched))
    )
    event = {"type": "workspace.record", **SPACE, "id": "req-1", "op": "created", "ts": "2026-10-12T15:43:14Z"}

    spec.on_record(event)

    assert seen == [(event, [SPACE])]


def test_the_scan_is_handed_the_definition_s_item_so_it_sees_backfill(monkeypatch):
    env = {"version": 1, "publishedAt": "2026-10-12T00:00:00Z", "agent_name": "support-drafts"}
    env["doc"] = _doc(record_spaces=[{**SPACE, "backfill": True}])
    monkeypatch.setattr(ja, "read_owner_key_strict", lambda agent, key, **_kw: env)
    monkeypatch.setattr("crewaimeat.aimeat_crew._aimeat_call", lambda a, tool, payload, **_kw: {"ok": True})
    monkeypatch.setattr(ja, "sync_contract_tag", lambda spec, **_kw: "unchanged")
    captured: dict = {}
    monkeypatch.setattr("crewaimeat.aimeat_crew.run_crew", lambda spec: captured.setdefault("spec", spec))
    ja.run_json_agent("support-drafts")
    seen: list = []
    monkeypatch.setattr("crewaimeat.record_wake.RecordWake.on_event", lambda self, event, watched: seen.append(watched))

    captured["spec"].on_record({"type": "workspace.record", **SPACE, "id": None, "op": "catchup"})

    assert seen == [[{**SPACE, "backfill": True}]] and captured["spec"].record_spaces == [SPACE]


def test_a_scan_the_node_refused_is_said_and_counted_as_a_refused_run(spec, monkeypatch, capsys):
    from crewaimeat import lifecycle
    from crewaimeat.record_wake import RecordWakeError

    def _refuse(self, event, watched):
        raise RecordWakeError(
            "the node refused the read of workspace ws-1 (SCOPE_DENIED): needs organism:read", refused=True
        )

    monkeypatch.setattr("crewaimeat.record_wake.RecordWake.on_event", _refuse)
    noted: dict = {}
    monkeypatch.setattr(lifecycle, "note_refused", lambda task_id, reason: noted.update({task_id: reason}))

    spec.on_record({"type": "workspace.record", **SPACE, "id": None, "op": "catchup"})

    assert "could NOT make tasks" in capsys.readouterr().err
    assert list(noted.values()) == ["the node refused the read of workspace ws-1 (SCOPE_DENIED): needs organism:read"]


def test_an_answer_that_did_not_arrive_is_said_and_is_not_a_refusal(spec, monkeypatch, capsys):
    from crewaimeat import lifecycle
    from crewaimeat.record_wake import RecordWakeError

    def _silent(self, event, watched):
        raise RecordWakeError("the node did not answer the read of workspace ws-1")

    monkeypatch.setattr("crewaimeat.record_wake.RecordWake.on_event", _silent)
    noted: list = []
    monkeypatch.setattr(lifecycle, "note_refused", lambda *a: noted.append(a))

    spec.on_record({"type": "workspace.record", **SPACE, "id": None, "op": "catchup"})

    assert "could NOT make tasks" in capsys.readouterr().err and noted == []


# ── what the runtime says about itself ───────────────────────────────────────
def test_the_menu_says_this_runtime_honours_record_spaces(monkeypatch):
    """The node's new-agent page offers "when something new arrives in a workspace" only where the
    menu of a connected agent carries this string: an older runtime ignores the field, and an agent
    made with it there would never be woken."""
    from crewaimeat import crew_invoke as ci

    monkeypatch.setattr("crewaimeat.llm_choice.default_is_node_road", lambda agent_name: False)  # a node read
    ok, menu = ci.handle("crew.menu", {}, agent_name="node-agent")
    assert ok and menu["features"] == ["record_spaces", "record_backfill"]

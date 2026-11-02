"""A run that did not write what its definition requires does not end done.

varoituskartta-vahti ended done three times on 2026-11-01 with nothing written to `varoituskartta.c.*`.
These hold the validator, the check, and the wiring into the task's close. No node: the read is replaced
at `_aimeat_rest`. The real node is in test_run_refusals_live.py.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from crewaimeat import lifecycle
from crewaimeat import required_outcome as ro
from crewaimeat.crew_def import validate_crew_doc

REQUIRED = {"memory_prefix": "kartta.c.", "namespace": "agent", "none_when": "NOTHING QUEUED"}


@pytest.fixture(autouse=True)
def _clean():
    lifecycle._OUTCOMES.clear()
    yield
    lifecycle._OUTCOMES.clear()


@pytest.fixture
def node(monkeypatch):
    """The agent's records as the node lists them; `answer` replaces the whole reply when set."""
    state = SimpleNamespace(records={}, answer=None, asked=[])

    def rest(agent, method, path, body=None, **_kw):
        state.asked.append((agent, method, path))
        if state.answer is not None:
            return state.answer
        return {"items": [{"key": k, "version": v} for k, v in state.records.items()], "total": len(state.records)}

    monkeypatch.setattr("crewaimeat.aimeat_crew._aimeat_rest", rest)
    return state


def _doc(**fields) -> dict:
    return {
        "agent_name": "kartta",
        "agents": [{"name": "E", "role": "r", "goal": "g", "backstory": "b"}],
        "tasks": [{"id": "t", "agent": "E", "description": "{{ctx.prompt}}", "expected_output": "x"}],
        **fields,
    }


# ── the definition ───────────────────────────────────────────────────────────────────────────


def test_a_definition_with_the_field_is_valid_and_one_without_stays_valid():
    assert validate_crew_doc(_doc(required_outcome=REQUIRED)) == []
    assert validate_crew_doc(_doc(required_outcome=THE_MAP)) == []
    assert validate_crew_doc(_doc()) == []


@pytest.mark.parametrize(
    ("value", "says"),
    [
        ("kartta.c.", "must be an object"),
        ({"namespace": "agent"}, "memory_prefix"),
        ({"memory_prefix": "a b.", "namespace": "agent"}, "memory_prefix"),
        ({"memory_prefix": "a.{x}.", "namespace": "agent"}, "memory_prefix"),
        ({"memory_prefix": "kartta."}, "whose memory"),
        ({"memory_prefix": "kartta.", "namespace": "owner"}, "whose memory"),
        ({"memory_prefix": "crews.kartta.", "namespace": "agent"}, "every run would pass"),
        ({"memory_prefix": "agents.kartta.", "namespace": "agent"}, "every run would pass"),
        ({"memory_prefix": "kartta.", "namespace": "agent", "none_when": " "}, "none_when"),
        ({"memory_prefix": "kartta.", "namespace": "agent", "must": 1}, "unknown field"),
        ({"memory_prefix": "kartta.", "namespace": "agent", "write_answer": "private"}, "visibility"),
        ({"memory_prefix": "kartta.", "namespace": "agent", "write_answer": "public"}, "value_schema"),
        ({"memory_prefix": "kartta.", "namespace": "agent", "value_schema": {"type": "object"}}, "name both"),
        (
            {
                "memory_prefix": "kartta.",
                "namespace": "agent",
                "write_answer": "public",
                "value_schema": {"type": "nope"},
            },
            "not a valid JSON Schema",
        ),
        (
            {
                "memory_prefix": "kartta.",
                "namespace": "agent",
                "write_answer": "public",
                "value_schema": {"properties": {"a": {"$ref": "https://schemas.invalid/x.json"}}},
            },
            "must stay inside",
        ),
    ],
)
def test_a_wrong_field_is_refused_when_the_definition_is_read(value, says):
    errors = validate_crew_doc(_doc(required_outcome=value))
    assert any(says in e for e in errors), errors


def test_the_definition_hands_the_field_to_the_crew():
    from crewaimeat.crew_def import crewspec_from_json

    assert crewspec_from_json(_doc(required_outcome=REQUIRED)).required_outcome == REQUIRED
    assert crewspec_from_json(_doc()).required_outcome is None


def test_the_runtime_says_it_honours_the_field():
    from crewaimeat.runtime_features import FEATURES

    assert "required_outcome" in FEATURES


# ── the check ────────────────────────────────────────────────────────────────────────────────


def test_nothing_written_fails(node):
    node.records = {"kartta.c.RU": 3}
    ro.check("kartta", "t1", REQUIRED, {"kartta.c.RU": 3}, "I wrote the record for RU.")
    failed, message = lifecycle.take_outcome("t1")
    assert failed is True
    assert "kartta.c." in message and "NOTHING QUEUED" in message


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ({"kartta.c.RU": 3}, {"kartta.c.RU": 4}),  # the same record again: a write adds one to its version
        ({}, {"kartta.c.RU": 1}),  # a new record
        ({"kartta.c.RU": 3}, {"kartta.c.RU": 3, "kartta.c.SE": 1}),  # one more beside an old one
    ],
)
def test_a_write_is_any_changed_version(node, before, after):
    node.records = after
    ro.check("kartta", "t1", REQUIRED, before, "done")
    assert lifecycle.take_outcome("t1") is None


def test_the_words_of_nothing_to_do_pass_exactly_and_ask_nothing(node):
    ro.check("kartta", "t1", REQUIRED, {}, "NOTHING QUEUED")
    assert lifecycle.take_outcome("t1") is None
    assert node.asked == []


def test_working_notes_before_the_words_of_nothing_to_do_are_not_read(node):
    ro.check("kartta", "t1", REQUIRED, {}, "The queue was empty.\nNOTHING QUEUED")
    assert lifecycle.take_outcome("t1") is None


@pytest.mark.parametrize("answer", ["Nothing queued.", "NOTHING QUEUED today", "I found NOTHING QUEUED"])
def test_other_words_are_not_nothing_to_do(node, answer):
    ro.check("kartta", "t1", REQUIRED, {}, answer)
    assert lifecycle.take_outcome("t1")[0] is True


def test_without_none_when_only_a_write_passes(node):
    required = {"memory_prefix": "kartta.c.", "namespace": "agent"}
    ro.check("kartta", "t1", required, {}, "NOTHING QUEUED")
    failed, message = lifecycle.take_outcome("t1")
    assert failed is True and "its answer was not" not in message


@pytest.mark.parametrize(
    ("before", "answer"),
    [
        (None, {"items": []}),  # the read before the run got no answer
        ({}, {"ok": False, "error": {"code": "UNAVAILABLE"}, "http_status": 503}),  # nor the one after
        ({}, {"total": 0}),  # an answer with no list
    ],
)
def test_when_it_cannot_be_read_the_task_completes_and_says_so(node, before, answer):
    node.answer = answer
    ro.check("kartta", "t1", REQUIRED, before, "done")
    failed, message = lifecycle.take_outcome("t1")
    assert failed is False and "could not be checked" in message


def test_a_run_already_failed_keeps_its_own_reason(node):
    lifecycle.note_outcome("t1", "the workflow's key is refused", failed=True)
    ro.check("kartta", "t1", REQUIRED, None, "done")
    assert lifecycle.take_outcome("t1") == (True, "the workflow's key is refused")


def test_the_read_is_the_agents_own_memory_with_no_values(node):
    assert ro.record_versions("kartta#o@n", "kartta.c.") == {}
    assert node.asked == [("kartta#o@n", "GET", "/v1/memory?prefix=kartta.c.&include=meta")]


# ── the close ────────────────────────────────────────────────────────────────────────────────


def test_the_close_runs_after_the_check_and_acts_on_it(node):
    closed = []

    def close(out):
        closed.append((out, lifecycle.has_failed_outcome("t1")))

    last = SimpleNamespace(output=SimpleNamespace(raw=" I wrote it. "))
    ro.checked_close("kartta", "t1", REQUIRED, {}, last, close)("finalize output")
    assert closed == [("finalize output", True)]


def test_a_check_that_raises_still_closes_the_task(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("tunnel dropped")

    monkeypatch.setattr(ro, "check", boom)
    closed = []
    ro.checked_close("kartta", "t1", REQUIRED, {}, SimpleNamespace(output=None), closed.append)("x")
    assert closed == ["x"]
    assert "could not be checked" in lifecycle.take_outcome("t1")[1]


# ── a whole run: the scaffold's builder, real CrewAI, a stub model ───────────────────────────


class _Run:
    """One task through `run_crew`'s real builder and a real kickoff; only the model and the node are stubs."""

    def __init__(self, monkeypatch, answer: str, records: dict, *, required: dict = REQUIRED):
        from crewai import Agent, BaseLLM

        from crewaimeat import aimeat_crew as ac
        from crewaimeat.crew_def import crewspec_from_json

        class _Model(BaseLLM):
            def call(self, messages, **_kw):  # noqa: ARG002 - one answer, whatever is asked
                return "Thought: done" + chr(10) + f"Final Answer: {answer}"

            def supports_function_calling(self):
                return False

            def supports_stop_words(self):
                return False

        # CrewAI's panel about tracing goes past pytest's capture in the console's own encoding.
        from crewai.events.listeners.tracing.utils import set_suppress_tracing_messages

        set_suppress_tracing_messages(True)
        self.calls: list[tuple[str, dict]] = []
        model = _Model(model="stub")

        def call(agent, tool, payload, **_kw):
            self.calls.append((tool, payload))
            if tool == "aimeat_memory_write" and payload["key"].startswith("kartta.c."):
                records[payload["key"]] = records.get(payload["key"], 0) + 1
            return {"ok": True}

        def rest(agent, method, path, body=None, **_kw):
            if path.startswith("/v1/memory?prefix="):
                return {"items": [{"key": k, "version": v} for k, v in records.items()]}
            return {"refusals": []} if "/refusals" in path else {}

        captured: dict = {}
        monkeypatch.setattr(ac, "_aimeat_call", call)
        monkeypatch.setattr(ac, "_aimeat_rest", rest)
        monkeypatch.setattr(ac, "get_llm", lambda *a, **k: model)
        monkeypatch.setattr(ac, "run_crew_daemon", lambda *a, **k: captured.update(k))
        # The start-up before the daemon: it waits for the owner's approval and onboards, and is not this test's.
        monkeypatch.setattr(ac, "_acquire_single_instance", lambda *_a: True)
        monkeypatch.setattr(ac, "_wait_for_auth", lambda *_a: None)
        monkeypatch.setattr(ac, "_serve_attach_bridge", lambda *_a: None)
        monkeypatch.setattr(ac, "ensure_serve", lambda **_k: {"port": 1})
        monkeypatch.setattr(ac, "_onboarding_state", lambda *_a: (True, 0))
        ac.run_crew(crewspec_from_json(_doc(required_outcome=required), owner="o"))
        task = {"id": "t1", "description": "do it", "title": "do it", "status": "active"}
        liaison = Agent(role="l", goal="g", backstory="b", llm=model)
        captured["build_crew"](task, liaison).kickoff()

    def tools(self) -> list[str]:
        return [t for t, _ in self.calls if t in ("aimeat_task_fail", "aimeat_task_complete")]


def test_a_real_run_that_only_said_it_wrote_ends_failed(monkeypatch):
    run = _Run(monkeypatch, "I wrote the record for RU.", {"kartta.c.RU": 3})
    assert run.tools() == ["aimeat_task_fail"]
    assert "kartta.c." in dict(run.calls)["aimeat_task_fail"]["message"]


def test_a_real_run_with_nothing_to_do_completes(monkeypatch):
    assert _Run(monkeypatch, "NOTHING QUEUED", {}).tools() == ["aimeat_task_complete"]


# ── code writes the answer ───────────────────────────────────────────────────────────────────

# The real shape, as the map's two readers want it (varoituskartta/extension/refresh.js cleanRecord and
# varoituskartta.html sigOk): one object per signal, under the signal's name, and at least one signal,
# because an empty set wipes that country's signals on the map.
_SIGNAL = {
    "type": "object",
    "required": ["s", "q", "fi", "en", "part"],
    "additionalProperties": False,
    "properties": {
        "s": {"enum": ["us", "uk", "ca", "de", "se"]},
        "q": {"type": "string", "minLength": 1},
        "fi": {"type": "string", "minLength": 1},
        "en": {"type": "string", "minLength": 1},
        "part": {"type": "boolean"},
    },
}
COUNTRY = {
    "type": "object",
    "required": ["at", "sig", "sum"],
    "additionalProperties": False,
    "properties": {
        "at": {"type": "string", "minLength": 1},
        "sig": {
            "type": "object",
            "minProperties": 1,
            "additionalProperties": False,
            "properties": {
                k: {"$ref": "#/$defs/signal"} for k in ("border", "military", "prepare", "stock", "milinstr")
            },
        },
        "sum": {
            "type": "object",
            "required": ["fi", "en"],
            "additionalProperties": False,
            "properties": {"fi": {"type": "string", "minLength": 1}, "en": {"type": "string", "minLength": 1}},
        },
        "se": {"type": "array", "minItems": 5, "maxItems": 5},
    },
    "$defs": {"signal": _SIGNAL},
}
WRITES = {**REQUIRED, "write_answer": "public", "value_schema": {"type": ["object", "integer"]}}
THE_MAP = {**REQUIRED, "write_answer": "public", "value_schema": COUNTRY}
SIGNAL = {"s": "ca", "q": "The border is closed.", "fi": "Raja suljettu", "en": "Border closed", "part": True}
RIGHT = {"at": "2026-11-02T15:42:58.306Z", "sig": {"border": SIGNAL}, "sum": {"fi": "a", "en": "b"}}
# What task 6b517c5c answered on 2026-11-02: the signals under the ministries' names.
BY_MINISTRY = {**RIGHT, "sig": {"ca": [SIGNAL]}}
# What the live record KE holds: a list per signal, which both of the map's readers drop.
A_LIST_PER_SIGNAL = {**RIGHT, "sig": {"border": [SIGNAL]}}


@pytest.fixture
def writes(monkeypatch):
    """The memory writes that reached the node; `refuse` maps a key to the node's refusal of it."""
    state = SimpleNamespace(made=[], refuse={})

    def call(agent, tool, payload, **_kw):
        assert tool == "aimeat_memory_write"
        if payload["key"] in state.refuse:
            return {"ok": False, "error": {"code": state.refuse[payload["key"]], "message": "no"}}
        state.made.append(payload)
        return {"key": payload["key"]}

    monkeypatch.setattr("crewaimeat.aimeat_crew._aimeat_call", call)
    return state


def test_code_writes_each_record_at_the_visibility_the_definition_names(writes):
    ro.write_answer(
        "kartta", "t1", WRITES, '[{"key": "kartta.c.RU", "value": {"at": "x"}}, {"key": "kartta.c.SE", "value": 1}]'
    )
    assert writes.made == [
        {"key": "kartta.c.RU", "value": {"at": "x"}, "visibility": "public"},
        {"key": "kartta.c.SE", "value": 1, "visibility": "public"},
    ]
    assert lifecycle.take_outcome("t1") is None


def test_a_code_fence_around_the_list_is_not_a_failure(writes):
    ro.write_answer("kartta", "t1", WRITES, '```json\n[{"key": "kartta.c.RU", "value": 1}]\n```')
    assert [w["key"] for w in writes.made] == ["kartta.c.RU"]


def test_working_notes_before_a_fenced_list_are_not_read(writes):
    # The measured shape: task 6b517c5c, varoituskartta-vahti, 2026-11-02.
    answer = (
        'Let me write [{"key": "kartta.c.XX", "value": 0}] first.\n```text\nnotes\n```\nFinal answer.'
        '```json\n[{"key": "kartta.c.RU", "value": 1}]\n```'
    )
    ro.write_answer("kartta", "t1", WRITES, answer)
    assert [w["key"] for w in writes.made] == ["kartta.c.RU"]


def test_a_list_in_the_notes_with_text_after_it_is_not_taken(writes):
    ro.write_answer("kartta", "t1", WRITES, 'Maybe [{"key": "kartta.c.RU", "value": 1}], or not.')
    assert writes.made == []
    assert lifecycle.take_outcome("t1")[0] is True


def test_the_measured_answer_with_no_fence_is_read_and_passes_the_maps_schema(writes):
    # The real end of task 447f5e83's answer, 2026-11-02, with its signals shortened to two of CD's.
    required = {**THE_MAP, "memory_prefix": "varoituskartta.c."}
    answer = (
        'CD se: [3, 4, sentence, date, url]. For a note like [{"key": "x"}] nothing is taken.\n\nLet me final'
        '[{"key": "varoituskartta.c.CD", "value": {"at": "2026-11-02T15:42:58.306Z", "sig": {"border": '
        '{"s": "ca", "q": "Land and water border crossings may also close without notice.", '
        '"fi": "Raja-asemat voivat sulkeutua", "en": "Border crossings may close", "part": false}, '
        '"stock": {"s": "ca", "q": "shelter in place and have adequate supplies, like food and water", '
        '"fi": "Pysy suojassa ja varaa ruokaa ja vettä", "en": "Shelter in place with food and water", '
        '"part": true}}, "sum": {"fi": "Vältä kaikkea matkustamista.", "en": "Avoid all travel."}, '
        '"se": [3, 4, "Med anledning av säkerhetsläget avråder Utrikesdepartementet.", "2025-06-03", '
        '"https://www.regeringen.se/ud-avrader/demokratiska-republiken-kongo/"]}}]'
    )
    ro.write_answer("kartta", "t1", required, answer)
    assert lifecycle.take_outcome("t1") is None
    assert [w["key"] for w in writes.made] == ["varoituskartta.c.CD"]
    assert writes.made[0]["value"]["se"][3] == "2025-06-03"


@pytest.mark.parametrize(
    "answer", ["I wrote them.", "[]", '{"key": "kartta.c.RU", "value": 1}', '[{"key": "kartta.c.R']
)
def test_an_answer_that_is_not_the_list_writes_nothing_and_fails(writes, answer):
    ro.write_answer("kartta", "t1", WRITES, answer)
    assert writes.made == []
    failed, message = lifecycle.take_outcome("t1")
    assert failed is True and "not the JSON list" in message


def test_a_right_record_passes_the_maps_schema(writes):
    ro.write_answer("kartta", "t1", THE_MAP, json.dumps([{"key": "kartta.c.RU", "value": RIGHT}]))
    assert [w["key"] for w in writes.made] == ["kartta.c.RU"]
    assert lifecycle.take_outcome("t1") is None


def test_the_measured_wrong_shape_stores_nothing_not_even_the_right_records_beside_it(writes):
    answer = json.dumps([{"key": "kartta.c.RU", "value": RIGHT}, {"key": "kartta.c.KE", "value": BY_MINISTRY}])
    ro.write_answer("kartta", "t1", THE_MAP, answer)
    assert writes.made == [], "one wrong record refuses the whole list, before the first write"
    failed, message = lifecycle.take_outcome("t1")
    assert failed is True and "Nothing was written" in message
    assert "kartta.c.KE" in message and "ca" in message, "the owner reads which record and what is wrong"


@pytest.mark.parametrize(
    "value",
    [
        {"at": "x", "sig": {}},  # no summary
        {**RIGHT, "extra": 1},
        {**RIGHT, "sig": {"border": {**SIGNAL, "part": "yes"}}},
        {**RIGHT, "sig": {}},  # an empty set wipes the country's signals on the map
        {**RIGHT, "se": [4]},
        A_LIST_PER_SIGNAL,
        "a text",
    ],
)
def test_a_value_of_the_wrong_shape_is_refused(writes, value):
    ro.write_answer("kartta", "t1", THE_MAP, json.dumps([{"key": "kartta.c.RU", "value": value}]))
    assert writes.made == [] and lifecycle.take_outcome("t1")[0] is True


def test_a_check_that_cannot_run_lets_nothing_through(writes):
    required = {**WRITES, "value_schema": {"$ref": "https://schemas.invalid/x.json"}}  # no validator lets this in
    ro.write_answer("kartta", "t1", required, '[{"key": "kartta.c.RU", "value": 1}]')
    assert writes.made == [] and "could not be checked" in lifecycle.take_outcome("t1")[1]


def test_a_key_outside_the_prefix_refuses_the_list(writes):
    answer = '[{"key": "kartta.c.RU", "value": 1}, {"key": "crews.registry.kartta", "value": {}}, {"key": "kartta.c.", "value": 2}]'
    ro.write_answer("kartta", "t1", WRITES, answer)
    assert writes.made == []
    failed, message = lifecycle.take_outcome("t1")
    assert failed is True and "crews.registry.kartta" in message and "Nothing was written" in message


def test_a_key_the_node_refuses_fails_the_run_by_name_after_the_good_ones(writes):
    writes.refuse["kartta.c.RU"] = "KEY_RESERVED"
    ro.write_answer("kartta", "t1", WRITES, '[{"key": "kartta.c.RU", "value": 1}, {"key": "kartta.c.SE", "value": 2}]')
    assert [w["key"] for w in writes.made] == ["kartta.c.SE"]
    failed, message = lifecycle.take_outcome("t1")
    assert failed is True and "kartta.c.RU" in message and "KEY_RESERVED" in message


@pytest.mark.parametrize("record", ['"text"', '{"key": "kartta.c.RU"}', '{"value": 1}'])
def test_a_malformed_record_is_named(writes, record):
    ro.write_answer("kartta", "t1", WRITES, f"[{record}]")
    assert writes.made == []
    assert lifecycle.take_outcome("t1")[0] is True


def test_none_when_writes_nothing(writes, node):
    ro.check("kartta", "t1", WRITES, {}, "NOTHING QUEUED")
    assert writes.made == [] and lifecycle.take_outcome("t1") is None


def test_a_real_run_whose_answer_is_the_list_is_written_by_code_and_ends_done(monkeypatch):
    answer = '[{"key": "kartta.c.RU", "value": {"at": "x"}}]'
    run = _Run(monkeypatch, answer, {"kartta.c.RU": 3}, required=WRITES)
    assert run.tools() == ["aimeat_task_complete"]
    assert ("aimeat_memory_write", {"key": "kartta.c.RU", "value": {"at": "x"}, "visibility": "public"}) in run.calls

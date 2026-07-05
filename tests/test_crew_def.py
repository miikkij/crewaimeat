"""Declarative crew-def: validator + interpreter (crewaimeat.crew_def).

Pure, offline, deterministic — no LLM call, no network. The interpreter builds real ``crewai``
Agent/Task objects (the LLM is a real offline ``LLM`` from ``crew_fixtures.make_ctx``, never called),
and the validator is exercised class-by-class so a regression names exactly which guard broke. The
last test proves the shipped ``crew_defs/joker.json`` reconstructs a crew equivalent to the hand-written
``crews/joker_crew.py`` — the Phase-1 "crew as data" proof.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from crewaimeat.crew_def import (
    CrewDocError,
    build_domain_from_json,
    crewspec_from_json,
    load_crew_doc,
    validate_crew_doc,
)
from tests.crew_fixtures import SENTINEL, make_ctx

JOKER_JSON = Path(__file__).resolve().parents[1] / "crew_defs" / "joker.json"


def _minimal_doc() -> dict:
    """A small valid two-agent, two-task doc (writer -> editor, editor reads the writer's output)."""
    return {
        "agent_name": "demo",
        "agents": [
            {"name": "writer", "role": "Writer", "goal": "Write", "backstory": "You write."},
            {"name": "editor", "role": "Editor", "goal": "Edit", "backstory": "You edit."},
        ],
        "tasks": [
            {
                "id": "draft",
                "agent": "writer",
                "description": "Write about: {{ctx.prompt}}",
                "expected_output": "A draft.",
            },
            {
                "id": "polish",
                "agent": "editor",
                "description": "Polish the draft. Today is {{ctx.today}}.",
                "expected_output": "A polished result.",
                "context": ["draft"],
            },
        ],
    }


# ── validator: the happy path ─────────────────────────────────────────────────
def test_minimal_doc_is_valid():
    assert validate_crew_doc(_minimal_doc()) == []


def test_shipped_joker_doc_is_valid():
    assert validate_crew_doc(load_crew_doc(JOKER_JSON)) == []


# ── validator: each error class is caught individually ────────────────────────
def test_missing_agent_name():
    doc = _minimal_doc()
    del doc["agent_name"]
    assert any("agent_name" in e for e in validate_crew_doc(doc))


def test_agent_missing_required_field():
    doc = _minimal_doc()
    del doc["agents"][0]["backstory"]
    assert any("backstory" in e for e in validate_crew_doc(doc))


def test_duplicate_agent_key():
    doc = _minimal_doc()
    doc["agents"][1]["name"] = "writer"
    assert any("duplicate agent key" in e for e in validate_crew_doc(doc))


def test_unknown_tool_name():
    doc = _minimal_doc()
    doc["agents"][0]["tools"] = ["memory", "bogus-tool"]
    unknown = [e for e in validate_crew_doc(doc) if "unknown tool" in e]
    assert len(unknown) == 1 and "bogus-tool" in unknown[0]  # only the bogus name is flagged, not 'memory'


def test_task_references_missing_agent():
    doc = _minimal_doc()
    doc["tasks"][0]["agent"] = "nobody"
    assert any("does not match any defined agent" in e for e in validate_crew_doc(doc))


def test_task_missing_expected_output():
    doc = _minimal_doc()
    del doc["tasks"][0]["expected_output"]
    assert any("expected_output" in e for e in validate_crew_doc(doc))


def test_duplicate_task_id():
    doc = _minimal_doc()
    doc["tasks"][1]["id"] = "draft"
    assert any("duplicate task id" in e for e in validate_crew_doc(doc))


def test_forward_context_ref_is_non_dag():
    # draft points at polish, which is defined LATER — a forward edge (non-DAG) must be rejected.
    doc = _minimal_doc()
    doc["tasks"][0]["context"] = ["polish"]
    assert any("EARLIER task" in e for e in validate_crew_doc(doc))


def test_self_context_ref():
    doc = _minimal_doc()
    doc["tasks"][0]["context"] = ["draft"]
    assert any("references itself" in e for e in validate_crew_doc(doc))


def test_unknown_context_ref():
    doc = _minimal_doc()
    doc["tasks"][1]["context"] = ["ghost"]
    assert any("EARLIER task" in e and "ghost" in e for e in validate_crew_doc(doc))


def test_unknown_template_placeholder():
    doc = _minimal_doc()
    doc["tasks"][0]["description"] = "Write about {{ctx.propmt}}"  # typo — must not pass silently
    errs = validate_crew_doc(doc)
    assert any("unknown placeholder" in e for e in errs)


def test_missing_ctx_prompt_injection():
    doc = _minimal_doc()
    doc["tasks"][0]["description"] = "Write something generic."  # no {{ctx.prompt}} anywhere
    doc["tasks"][1]["description"] = "Polish it."
    assert any("ctx.prompt" in e for e in validate_crew_doc(doc))


def test_bad_process():
    doc = _minimal_doc()
    doc["process"] = "parallel"
    assert any("process" in e for e in validate_crew_doc(doc))


def test_bad_temperature():
    doc = _minimal_doc()
    doc["temperature"] = 5
    assert any("temperature" in e for e in validate_crew_doc(doc))


def test_bad_tag_charset():
    doc = _minimal_doc()
    doc["tags"] = ["ok-tag", "bad:tag"]  # ':' is rejected by the node's tag charset
    assert any("charset" in e and "bad:tag" in e for e in validate_crew_doc(doc))


def test_malformed_signal():
    doc = _minimal_doc()
    doc["signals"] = {"success_signal": {"kind": "deterministic", "op": "not-a-real-op", "key": "k"}}
    assert any("signals.success_signal" in e for e in validate_crew_doc(doc))


def test_valid_signal_passes():
    doc = _minimal_doc()
    doc["signals"] = {
        "required_to_function": "none",
        "success_signal": {"kind": "deterministic", "op": "nonempty", "key_glob": "crews.demo.*"},
        "deliverable_location": {"key": "crews.demo"},
    }
    assert validate_crew_doc(doc) == []


# ── interpreter: builds real agents/tasks with the right wiring ───────────────
def test_interpreter_builds_agents_and_tasks():
    agents, tasks = build_domain_from_json(_minimal_doc(), make_ctx())
    assert [a.role for a in agents] == ["Writer", "Editor"]
    assert len(tasks) == 2
    # context wiring: polish reads the draft Task object (the actual earlier object, not a string).
    assert tasks[1].context == [tasks[0]]
    assert tasks[0].context != [tasks[1]]  # the draft declared no context -> not wired to polish


def test_interpreter_injects_ctx_prompt_and_today():
    ctx = make_ctx()  # prompt carries the SENTINEL
    _agents, tasks = build_domain_from_json(_minimal_doc(), ctx)
    assert SENTINEL in tasks[0].description  # {{ctx.prompt}} substituted
    assert "{{ctx.prompt}}" not in tasks[0].description  # the placeholder is gone
    assert "2026-06-05" in tasks[1].description  # {{ctx.today}} substituted


def test_interpreter_resolves_tool_names_to_factories():
    doc = _minimal_doc()
    doc["agents"][0]["tools"] = ["memory"]
    agents, _tasks = build_domain_from_json(doc, make_ctx())
    tool_names = {getattr(t, "name", "") for t in agents[0].tools}
    assert {"write_memory", "read_memory"} <= tool_names  # real memory tools were attached
    assert agents[1].tools == []  # an agent with no tools declared gets none


def test_interpreter_honours_async_flag():
    doc = _minimal_doc()
    doc["tasks"][0]["async"] = True
    _agents, tasks = build_domain_from_json(doc, make_ctx())
    assert tasks[0].async_execution is True
    assert tasks[1].async_execution is False


def test_interpreter_raises_crewdocerror_on_invalid_doc():
    doc = _minimal_doc()
    doc["tasks"][0]["agent"] = "nobody"
    with pytest.raises(CrewDocError) as ei:
        build_domain_from_json(doc, make_ctx())
    assert ei.value.errors  # the raised error carries the full problem list


def test_interpreter_rejects_unknown_tool_loudly():
    doc = _minimal_doc()
    doc["agents"][0]["tools"] = ["bogus"]
    with pytest.raises(CrewDocError):
        build_domain_from_json(doc, make_ctx())


# ── crewspec_from_json: the doc becomes a runnable CrewSpec ────────────────────
def test_crewspec_from_json_carries_crew_level_fields():
    from crewai import Process

    spec = crewspec_from_json(load_crew_doc(JOKER_JSON))
    assert spec.agent_name == "joker"
    assert spec.temperature == 0.7
    assert spec.process == Process.sequential
    assert "humor" in spec.tags
    assert spec.offer and spec.offer["id"] == "tell-jokes"
    # its build_domain is the interpreter bound to the doc — it produces the crew.
    agents, tasks = spec.build_domain(make_ctx())
    assert len(agents) == 5 and len(tasks) == 5


def test_crewspec_overrides_win():
    spec = crewspec_from_json(_minimal_doc(), listen_for=("tasks", "records"))
    assert spec.listen_for == ("tasks", "records")


# ── the proof: JSON joker reconstructs the hand-written joker ─────────────────
def test_json_joker_equivalent_to_python_joker():
    from crews import joker_crew

    ctx = make_ctx()
    py_agents, py_tasks = joker_crew.build_domain(ctx)
    js_agents, js_tasks = build_domain_from_json(load_crew_doc(JOKER_JSON), ctx)

    # same cast of comedians, same number of tasks
    assert {a.role for a in js_agents} == {a.role for a in py_agents}
    assert len(js_agents) == len(py_agents) == 5
    assert len(js_tasks) == len(py_tasks) == 5

    # the host task fans in the four comedians' outputs, in both builds
    py_host = py_tasks[-1]
    js_host = js_tasks[-1]
    assert len(js_host.context) == len(py_host.context) == 4

    # and the topic (ctx.prompt) reached the comedians' descriptions in the JSON build
    assert all(SENTINEL in t.description for t in js_tasks[:4])

"""Data-driven agency templates — crewaimeat.brain_json.

Offline and deterministic. Proves: the shipped JSON research-assistant template validates, loads into the
gallery, and builds a crew EQUIVALENT to the hand-written Python research-assistant (same agent, tasks,
tools, and prose/request/publish injection); bad templates are caught at author time; and the AI-authoring
loop (generate_brain_template) returns a VALIDATED template — a fake LLM stands in for a real model, so the
"AI writes a runnable brain, no compile" loop is exercised without the network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import crewaimeat.agency as _agency
from crewaimeat import brain_json
from crewaimeat import brain_templates as bt
from crewaimeat.crew_def import TOOL_REGISTRY
from tests.crew_fixtures import make_ctx

TEMPLATE_JSON = Path(_agency.__file__).resolve().parent / "templates" / "research_assistant.json"


def _load_tj() -> dict:
    return json.loads(TEMPLATE_JSON.read_text(encoding="utf-8"))


def _brain(agent_name: str = "eu-ai-watch") -> dict:
    return {
        "agent_name": agent_name,
        "template_id": "research-assistant-json",
        "prose": "Focus on obligations for small SaaS companies.",
        "policy": {"visibility": "public", "publish_key": "", "key_mode": "date"},
    }


# ── the new tool ids the agency templates need ────────────────────────────────
def test_new_tools_are_registered():
    assert "local_memory" in TOOL_REGISTRY and "article_fetch" in TOOL_REGISTRY
    from crewaimeat.crew_def import render_tool_catalog

    cat = render_tool_catalog()
    assert "local_memory" in cat and "article_fetch" in cat and "web" in cat


# ── validation ────────────────────────────────────────────────────────────────
def test_shipped_template_is_valid():
    assert brain_json.validate_template(_load_tj()) == []


def test_validate_rejects_missing_header():
    assert any("template" in e for e in brain_json.validate_template({"crew": {}}))


def test_validate_rejects_bad_crew_doc():
    tj = _load_tj()
    tj["crew"]["agents"][0]["tools"] = ["web", "bogus-tool"]  # unknown tool
    assert any("unknown tool" in e for e in brain_json.validate_template(tj))


def test_validate_requires_ctx_prompt():
    tj = _load_tj()
    tj["crew"]["tasks"][0]["description"] = "{{brain.prose}} — answer generically."  # no {{ctx.prompt}}
    assert any("ctx.prompt" in e for e in brain_json.validate_template(tj))


def test_validate_rejects_unknown_brain_placeholder():
    tj = _load_tj()
    tj["crew"]["tasks"][0]["description"] += " {{brain.bogus}}"
    assert any("brain.bogus" in e for e in brain_json.validate_template(tj))


# ── the bridge builds a live crew with brain + ctx vars injected ──────────────
def test_bridge_injects_brain_and_ctx_vars():
    tj = _load_tj()
    tmpl = brain_json.template_from_json(tj)
    ctx = make_ctx("What are the EU's 2026 AI transparency rules?")
    agents, tasks = tmpl.build(ctx, _brain())
    assert len(agents) == 1 and len(tasks) == 1 and agents[0].role == "Research Assistant"
    desc = tasks[0].description
    assert "small SaaS" in desc  # {{brain.prose}}
    assert "2026 AI transparency" in desc  # {{ctx.prompt}} (the per-run request)
    assert "answers.eu-ai-watch." in desc  # {{brain.publish_key}} (policy-derived)
    assert "visibility='public'" in desc  # {{brain.visibility}}
    assert "{{" not in desc  # no placeholder left unsubstituted


def test_json_template_equivalent_to_python_research_assistant():
    ctx, brain = make_ctx("What are the EU's 2026 AI transparency rules?"), _brain()
    py_agents, py_tasks = bt.get("research-assistant").build(ctx, brain)
    js_agents, js_tasks = brain_json.template_from_json(_load_tj()).build(ctx, brain)

    assert {a.role for a in js_agents} == {a.role for a in py_agents}
    assert len(js_agents) == len(py_agents) == 1
    assert len(js_tasks) == len(py_tasks) == 1
    # same toolbelt (web search + article fetch + the local-memory tools)
    assert {getattr(t, "name", "") for t in js_agents[0].tools} == {getattr(t, "name", "") for t in py_agents[0].tools}
    # both weave the operator prose, the run request, and the same publish key into the task
    for tasks in (py_tasks, js_tasks):
        assert "small SaaS" in tasks[0].description and "answers.eu-ai-watch." in tasks[0].description


# ── loader: a JSON file becomes a gallery template ────────────────────────────
def test_load_json_templates_registers(tmp_path):
    tj = _load_tj()
    tj["template"]["id"] = "loaded-under-test"
    (tmp_path / "t.json").write_text(json.dumps(tj), encoding="utf-8")
    loaded = brain_json.load_json_templates(tmp_path)
    assert [t.id for t in loaded] == ["loaded-under-test"]
    assert bt.get("loaded-under-test") is not None


def test_load_skips_invalid_file(tmp_path, capsys):
    good = _load_tj()
    good["template"]["id"] = "good-one"
    bad = _load_tj()
    bad["template"]["id"] = "bad-one"
    bad["crew"]["agents"][0]["tools"] = ["nope"]  # invalid -> must be skipped, not crash
    (tmp_path / "good.json").write_text(json.dumps(good), encoding="utf-8")
    (tmp_path / "bad.json").write_text(json.dumps(bad), encoding="utf-8")
    loaded = brain_json.load_json_templates(tmp_path)
    assert [t.id for t in loaded] == ["good-one"]
    assert bt.get("bad-one") is None
    assert "skipping template bad.json" in capsys.readouterr().err


# ── AI authoring loop (fake LLM stands in for a real model) ───────────────────
class _FakeLLM:
    def __init__(self, out: str):
        self._out = out

    def call(self, *args, **kwargs):
        return self._out


def _valid_generated_template() -> dict:
    return {
        "template": {
            "id": "summarizer",
            "title": "Summarizer",
            "description": "Summarize a topic from the live web.",
            "default_prose": "Summarize what is genuinely new on the topic, with sources.",
            "default_publish_base": "summaries",
        },
        "crew": {
            "temperature": 0.3,
            "agents": [
                {
                    "name": "s",
                    "role": "Summarizer",
                    "goal": "Summarize",
                    "backstory": "You summarize.",
                    "tools": ["web"],
                }
            ],
            "tasks": [
                {
                    "id": "sum",
                    "agent": "s",
                    "description": "{{ctx.today}}\n{{brain.prose}}\nTopic: {{ctx.prompt}}",
                    "expected_output": "A short cited summary.",
                }
            ],
        },
    }


def test_generate_brain_template_returns_validated_json():
    llm = _FakeLLM("```json\n" + json.dumps(_valid_generated_template()) + "\n```")
    ok, tj, errs = brain_json.generate_brain_template("summarize a topic weekly", llm=llm)
    assert ok and errs == [] and tj["template"]["id"] == "summarizer"
    # the returned template is genuinely loadable into the gallery (proves it is runnable, not just parseable)
    assert brain_json.template_from_json(tj).id == "summarizer"


def test_generate_rejects_invalid_model_output():
    bad = _valid_generated_template()
    bad["crew"]["agents"][0]["tools"] = ["not-a-tool"]
    ok, tj, errs = brain_json.generate_brain_template("x", llm=_FakeLLM(json.dumps(bad)))
    assert not ok and tj is None and any("unknown tool" in e for e in errs)


def test_generate_handles_non_json_output():
    ok, tj, errs = brain_json.generate_brain_template("x", llm=_FakeLLM("sorry, I can't do that"))
    assert not ok and tj is None and any("did not return a JSON object" in e for e in errs)


def test_generation_prompt_lists_tools_and_brain_vars():
    brief = brain_json.render_template_schema_brief()
    assert "local_memory" in brief and "web" in brief  # tool menu from crew_def
    assert "{{brain.prose}}" in brief and "{{ctx.prompt}}" in brief  # placeholder rules
    assert "default_policy" in brief and "cron" in brief  # schedule/policy guidance


# ── shipped built-ins + user-dir persistence ──────────────────────────────────
def test_builtin_templates_ship_in_package():
    d = brain_json.default_templates_dir()
    ids = {
        json.loads((d / f).read_text(encoding="utf-8"))["template"]["id"]
        for f in ["research_assistant.json", "topic_watcher.json", "daily_briefing.json"]
    }
    assert ids == {"research-assistant-json", "topic-watcher-json", "daily-briefing-json"}
    # every shipped template validates
    for f in d.glob("*.json"):
        assert brain_json.validate_template(json.loads(f.read_text(encoding="utf-8"))) == [], f.name


def test_register_builtins_loads_all_three():
    loaded = {t.id for t in brain_json.register_builtin_json_templates()}
    assert {"research-assistant-json", "topic-watcher-json", "daily-briefing-json"} <= loaded
    assert bt.get("topic-watcher-json") is not None and bt.get("daily-briefing-json") is not None


def test_save_user_template_persists_and_registers(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    tj = _valid_generated_template()  # id 'summarizer'
    tmpl = brain_json.save_user_template(tj)
    assert tmpl.id == "summarizer" and bt.get("summarizer") is not None
    saved = tmp_path / "templates" / "summarizer.json"
    assert saved.is_file() and json.loads(saved.read_text(encoding="utf-8"))["template"]["id"] == "summarizer"


def test_save_user_template_rejects_invalid(tmp_path, monkeypatch):
    from crewaimeat.crew_def import CrewDocError

    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    bad = _valid_generated_template()
    bad["crew"]["agents"][0]["tools"] = ["nope"]
    with pytest.raises(CrewDocError):
        brain_json.save_user_template(bad)
    assert not (tmp_path / "templates").exists() or not list((tmp_path / "templates").glob("*.json"))


def test_suggested_agent_name():
    tj = _valid_generated_template()
    tj["template"]["suggested_agent_name"] = "Weekly Summarizer!"
    assert brain_json.suggested_agent_name(tj) == "weekly-summarizer"
    del tj["template"]["suggested_agent_name"]
    assert brain_json.suggested_agent_name(tj) == "summarizer"  # falls back to id

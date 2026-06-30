"""brains + templates — the brain model (template + prose + policy), versioning, and brain->CrewSpec
wiring. Isolated to a tmp AIMEAT_HOME; the live-node bits (run_crew, scheduling) are not exercised."""

from __future__ import annotations

import pytest


def test_template_registry_has_topic_watcher():
    from crewaimeat import brain_templates as templates

    t = templates.get("topic-watcher")
    assert t is not None
    assert t.title == "Topic watcher"
    assert "topic-watcher" in {x.id for x in templates.all_templates()}
    assert t.default_policy["autonomy"] == "draft"


def test_template_localized():
    from crewaimeat import brain_templates as templates

    t = templates.get("topic-watcher")
    en, fi = t.localized("en"), t.localized("fi")
    assert en["title"] == "Topic watcher" and fi["title"] == "Aiheen vahti"
    assert fi["default_prose"].startswith("Seuraa")  # Finnish starting prose
    assert t.localized("de")["title"] == "Topic watcher"  # unknown lang falls back to English


def test_save_brain_defaults_from_template(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    from crewaimeat import brain_templates as templates
    from crewaimeat import brains

    b = brains.save_brain("watcher-1", "topic-watcher")
    tmpl = templates.get("topic-watcher")
    assert b["version"] == 1
    assert b["prose"] == tmpl.default_prose  # prose defaulted from the template
    assert b["policy"]["schedule"]["cron"] == "0 8 * * *"  # policy defaulted from the template
    assert brains.get_brain("watcher-1")["title"] == "Topic watcher"


def test_unknown_template_fails_loud(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    from crewaimeat import brains

    with pytest.raises(ValueError):
        brains.save_brain("x", "no-such-template")


def test_edit_prose_keeps_policy_and_versions(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    from crewaimeat import brains

    brains.save_brain("w", "topic-watcher", prose="watch Finnish AI funding")
    # editing only the prose preserves the policy (and bumps the version)
    b2 = brains.save_brain("w", "topic-watcher", prose="watch German AI funding")
    assert b2["version"] == 2
    assert b2["prose"] == "watch German AI funding"
    assert b2["policy"]["schedule"]["cron"] == "0 8 * * *"  # untouched
    # editing only the policy preserves the prose
    b3 = brains.save_brain("w", "topic-watcher", policy={"autonomy": "act"})
    assert b3["version"] == 3 and b3["prose"] == "watch German AI funding"
    assert b3["policy"] == {"autonomy": "act"}


def test_history_and_rollback(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    from crewaimeat import brains

    brains.save_brain("w", "topic-watcher", prose="v1 prose")
    brains.save_brain("w", "topic-watcher", prose="v2 prose")
    assert [h["version"] for h in brains.history("w")] == [2, 1]

    restored = brains.rollback("w", 1)
    assert restored["version"] == 3  # rollback is non-destructive — a new version
    assert restored["prose"] == "v1 prose"
    assert brains.get_brain("w")["prose"] == "v1 prose"
    with pytest.raises(ValueError):
        brains.rollback("w", 99)


def test_list_and_delete(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    from crewaimeat import brains

    brains.save_brain("a", "topic-watcher")
    brains.save_brain("b", "topic-watcher")
    assert {x["agent_name"] for x in brains.list_brains()} == {"a", "b"}
    assert brains.delete_brain("a") is True
    assert brains.get_brain("a") is None
    assert brains.history("a") == []
    assert {x["agent_name"] for x in brains.list_brains()} == {"b"}


def test_apply_policy_sets_and_clears_model_override(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    from crewaimeat import brains, llm

    # model set in policy -> override pinned
    brains.save_brain("w", "topic-watcher", policy={"model": {"kind": "model", "model": "x/y"}})
    brains.apply_policy("w")
    assert llm.agent_override("w") == {"kind": "model", "model": "x/y"}

    # model cleared -> override removed
    brains.save_brain("w", "topic-watcher", policy={"model": None})
    brains.apply_policy("w")
    assert llm.agent_override("w") is None


def test_build_crewspec_wires_template(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)  # _web_tools() -> [] without it (no network)
    from crewaimeat import brains
    from crewaimeat.aimeat_crew import BuildContext

    brains.save_brain("watcher-x", "topic-watcher", prose="watch space weather")
    spec = brains.build_crewspec("watcher-x")
    assert spec.agent_name == "watcher-x"

    # the spec's build_domain dispatches to the topic-watcher template
    ctx = BuildContext(task={}, prompt="", llm="openrouter/owl-alpha", today="2026-06-28")
    agents, tasks = spec.build_domain(ctx)
    assert len(agents) == 1 and len(tasks) == 1
    tool_names = {t.name for t in agents[0].tools}
    assert {"remember", "publish_memory"} <= tool_names  # local-memory tools attached
    assert "watch space weather" in tasks[0].description  # the brain's prose drives the task

    with pytest.raises(ValueError):
        brains.build_crewspec("no-brain")


def test_topic_watcher_injects_both_prose_and_request(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    from crewaimeat import brains
    from crewaimeat.aimeat_crew import BuildContext

    brains.save_brain("w", "topic-watcher", prose="check news on any topic given in the task")
    spec = brains.build_crewspec("w")
    # the per-run topic (ctx.prompt — from a test-run box / an offer order) MUST reach the agent
    ctx = BuildContext(task={}, prompt="cold fusion breakthroughs", llm="openrouter/owl-alpha", today="2026-06-28")
    _agents, tasks = spec.build_domain(ctx)
    desc = tasks[0].description
    assert "check news on any topic given in the task" in desc  # the operator's standing prose
    assert "cold fusion breakthroughs" in desc  # AND this run's specific topic


def test_topic_watcher_key_mode_dates_the_publish_key(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    import datetime

    from crewaimeat import brains
    from crewaimeat.aimeat_crew import BuildContext

    brains.save_brain("nw", "topic-watcher", policy={"key_mode": "date"})
    spec = brains.build_crewspec("nw")
    ctx = BuildContext(task={}, prompt="x", llm="openrouter/owl-alpha", today="2026-06-28")
    _a, tasks = spec.build_domain(ctx)
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    assert f"watch.nw.{today}" in tasks[0].description  # dated, sortable key (not …latest)

    brains.save_brain("nw", "topic-watcher", policy={"key_mode": "latest"})
    _a, tasks2 = brains.build_crewspec("nw").build_domain(ctx)
    assert "watch.nw.latest" in tasks2[0].description


def test_write_crew_stub(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    from crewaimeat import brains

    brains.save_brain("News Watcher 1", "topic-watcher")
    path = brains.write_crew_stub("News Watcher 1", crews_dir=tmp_path / "crews")
    assert path.endswith("news_watcher_1_crew.py")
    text = (tmp_path / "crews" / "news_watcher_1_crew.py").read_text(encoding="utf-8")
    assert 'AGENT_NAME = "News Watcher 1"' in text
    assert "from crewaimeat.brains import run_brain" in text


@pytest.mark.parametrize(
    "tid", ["topic-watcher", "research-assistant", "daily-briefing", "page-watcher", "company-watcher", "map-snapshot"]
)
def test_every_template_builds(tid, tmp_path, monkeypatch):
    """Each registered template's build() must construct (agents, tasks) with an interpolated task — this
    catches a broken prompt/format in any template, not just topic-watcher."""
    import types

    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    from crewaimeat import brain_templates as templates

    t = templates.get(tid)
    ctx = types.SimpleNamespace(
        llm="ollama/gemma4",  # crewai accepts a string model id; no real LLM needed to build
        today="Today is 2026-06-30.",
        task={"title": "Tesla", "description": "latest news"},
        prompt="",
    )
    brain = {"agent_name": "smoke", "prose": t.default_prose, "policy": t.default_policy}
    agents, tasks = t.build(ctx, brain)
    assert agents and tasks
    assert tasks[0].description and "Tesla" in tasks[0].description  # this-run request reached the task


def test_slug_agent_name_matches_connector_rule():
    from crewaimeat import brains

    assert brains.slug_agent_name("Mapmaker") == "mapmaker"  # the bug: uppercase rejected by the connector
    assert brains.slug_agent_name("News Paska!") == "news-paska"
    assert brains.slug_agent_name("  Map Maker 2 ") == "map-maker-2"
    assert brains.slug_agent_name("ÄÖ###") == ""  # nothing usable


def test_migrate_invalid_names_self_heals(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    from crewaimeat import brains

    # an OLD brain stored with an invalid uppercase name (save_brain doesn't slug; only the API did)
    brains.save_brain("Mapmaker", "map-snapshot", prose="keep me")
    fixed = brains.migrate_invalid_names()
    assert ("Mapmaker", "mapmaker") in fixed
    assert brains.get_brain("Mapmaker") is None
    got = brains.get_brain("mapmaker")
    assert got is not None and got["prose"] == "keep me"  # content preserved through the rename

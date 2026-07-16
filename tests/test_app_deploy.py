"""Deterministic floor for the Agent-Bundled Apps deploy path (crewaimeat side, spec doc-76ab674).

No network, no LLM, no processes: every AIMEAT call, install and process probe is monkeypatched.
Covers the shared-contract behaviors the node builds against: scope recognition, crew-def
validation + rejection (unknown tool / unsanctioned skill / malformed), the single-tenant owner
guard, idempotent redeploy, the deploy-key writes, and undeploy's file/key cleanup.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crewaimeat import app_deploy
from crewaimeat.app_deploy import (
    DeployError,
    deploy_app_agent,
    deploy_key,
    deployed_agent_name,
    extract_cortex_agents,
    is_deploy_app_agent,
    is_undeploy_app_agent,
    select_crew_def,
    undeploy_app_agent,
    validate_app_crew_def,
)

OWNER = "happydude500001"
DEMO_APP_PATH = Path(__file__).resolve().parent.parent / "examples" / "agent_bundled_app" / "notes_summarizer_app.json"
DEMO_APP = json.loads(DEMO_APP_PATH.read_text(encoding="utf-8"))
DEMO_DOC = DEMO_APP["cortex"]["agents"][0]


def _scope_task(kind: str, **fields: str) -> dict:
    scope = [{"name": "kind", "value": kind}] + [{"name": k, "value": v} for k, v in fields.items()]
    return {"id": "t-test", "title": "anything at all", "description": "irrelevant", "scope": scope}


def _deploy_task(owner: str | None = OWNER) -> dict:
    fields = {"app_id": "agent-notes-demo", "agent_name": "notes-summarizer"}
    if owner is not None:
        fields["owner"] = owner
    return _scope_task("deploy-app-agent", **fields)


@pytest.fixture
def own_fleet(monkeypatch):
    monkeypatch.setenv("AIMEAT_OWNER", OWNER)


# --------------------------------------------------------------------------- #
# Recognition — by scope.kind, never by title
# --------------------------------------------------------------------------- #
def test_recognizes_deploy_scope_list_form():
    assert is_deploy_app_agent(_deploy_task())
    assert not is_undeploy_app_agent(_deploy_task())


def test_recognizes_dict_scope():
    task = {"scope": {"kind": "deploy-app-agent", "app_id": "a", "agent_name": "n"}}
    assert is_deploy_app_agent(task)


def test_title_never_triggers_recognition():
    assert not is_deploy_app_agent({"title": "deploy-app-agent agent-notes-demo", "scope": []})
    assert not is_deploy_app_agent({"title": "deploy-app-agent", "description": "deploy-app-agent"})


def test_recognizes_undeploy_scope():
    task = _scope_task("undeploy-app-agent", app_id="a", agent_name="n")
    assert is_undeploy_app_agent(task)
    assert not is_deploy_app_agent(task)


# --------------------------------------------------------------------------- #
# Naming — the shared-contract namespacing rule
# --------------------------------------------------------------------------- #
def test_deployed_agent_name_is_app_namespaced_and_charset_safe():
    assert deployed_agent_name("agent-notes-demo", "notes-summarizer") == "notes-summarizer-agent-notes-demo"
    assert deployed_agent_name("My App!", "Notes") == "notes-my-app"


def test_deploy_key_shape():
    assert deploy_key("notes-summarizer-agent-notes-demo") == "agents.notes-summarizer-agent-notes-demo.deploy"


# --------------------------------------------------------------------------- #
# Validation — the demo def is deployable; hostile/broken defs are rejected loudly
# --------------------------------------------------------------------------- #
def test_demo_crew_def_validates_and_maps_to_agents_and_tasks():
    assert validate_app_crew_def(DEMO_DOC) == []
    from crew_fixtures import make_ctx

    from crewaimeat.crew_def import build_domain_from_json

    agents, tasks = build_domain_from_json(DEMO_DOC, make_ctx("summarize my koi-pond-XYZZY notes"))
    assert len(agents) == 1 and len(tasks) == 1
    assert "koi-pond-XYZZY" in tasks[0].description  # {{ctx.prompt}} reaches the task


def test_unknown_tool_is_rejected():
    doc = json.loads(json.dumps(DEMO_DOC))
    doc["agents"][0]["tools"] = ["shell"]
    errs = validate_app_crew_def(doc)
    assert any("shell" in e for e in errs)


def test_registry_tool_outside_vetted_catalog_is_rejected():
    # article_fetch resolves in crew_def.TOOL_REGISTRY but is NOT in forge_catalog's vetted set —
    # an app may only attach what the catalog vets.
    doc = json.loads(json.dumps(DEMO_DOC))
    doc["agents"][0]["tools"] = ["article_fetch"]
    errs = validate_app_crew_def(doc)
    assert any("article_fetch" in e and "vetted" in e for e in errs)


def test_unsanctioned_and_pathlike_skills_are_rejected():
    doc = json.loads(json.dumps(DEMO_DOC))
    doc["skills"] = ["definitely-not-installed-xyz"]
    assert any("not installed" in e for e in validate_app_crew_def(doc))
    doc["skills"] = ["../../etc"]
    assert any("path-like" in e for e in validate_app_crew_def(doc))


def test_malformed_crew_def_is_rejected():
    assert validate_app_crew_def("not a dict") == ["crew def must be a JSON object"]
    doc = json.loads(json.dumps(DEMO_DOC))
    del doc["tasks"]
    assert validate_app_crew_def(doc)


# --------------------------------------------------------------------------- #
# App-record resolution
# --------------------------------------------------------------------------- #
def test_extract_cortex_agents_top_level_and_enveloped():
    assert extract_cortex_agents(DEMO_APP)[0]["agent_name"] == "notes-summarizer"
    nested = {"app": {"manifest": json.dumps({"cortex": {"agents": [{"agent_name": "x"}]}})}}
    assert extract_cortex_agents(nested) == [{"agent_name": "x"}]


def test_extract_cortex_agents_missing_is_loud():
    with pytest.raises(DeployError, match="no non-empty cortex.agents"):
        extract_cortex_agents({"app_id": "plain-app", "html": "<p>no agents</p>"})


def test_select_crew_def_missing_names_what_exists():
    with pytest.raises(DeployError, match="notes-summarizer"):
        select_crew_def([{"agent_name": "notes-summarizer"}], "other-agent")


# --------------------------------------------------------------------------- #
# Owner guard — single-tenant, fail loud
# --------------------------------------------------------------------------- #
def test_foreign_owner_in_scope_is_hard_rejected(own_fleet):
    with pytest.raises(DeployError, match="FOREIGN OWNER"):
        deploy_app_agent("crew-forge", _deploy_task(owner="mallory"))


def test_unset_fleet_owner_refuses_deploy(monkeypatch):
    monkeypatch.delenv("AIMEAT_OWNER", raising=False)
    with pytest.raises(DeployError, match="AIMEAT_OWNER is not set"):
        deploy_app_agent("crew-forge", _deploy_task())


def test_foreign_owner_on_app_record_is_hard_rejected(own_fleet, monkeypatch):
    app = json.loads(json.dumps(DEMO_APP))
    app["owner"] = "mallory"
    monkeypatch.setattr(app_deploy, "_aimeat_call", lambda *_a, **_k: app)
    with pytest.raises(DeployError, match="FOREIGN OWNER"):
        deploy_app_agent("crew-forge", _deploy_task(owner=None))


def test_missing_scope_fields_are_rejected(own_fleet):
    with pytest.raises(DeployError, match="app_id"):
        deploy_app_agent("crew-forge", _scope_task("deploy-app-agent", agent_name="n", owner=OWNER))


# --------------------------------------------------------------------------- #
# Deploy — happy path, invalid embedded def, idempotent redeploy
# --------------------------------------------------------------------------- #
@pytest.fixture
def deploy_harness(own_fleet, monkeypatch):
    """Fake AIMEAT + fleet: app_get serves the demo app, installs and key writes are recorded."""
    calls = {"aimeat": [], "installs": [], "running": False}

    def fake_call(agent, tool, payload, **_kw):
        calls["aimeat"].append((tool, payload))
        if tool == "aimeat_app_get":
            return json.loads(json.dumps(DEMO_APP))
        return {"ok": True}

    def fake_install(doc, *, agent, gaii=None, register=True):
        calls["installs"].append(doc)
        return f"INSTALLED '{doc['agent_name']}': VALID: 1 agents, 1 tasks -> (stubbed)"

    import crewaimeat.crew_registry as crew_registry
    import crewaimeat.forge as forge

    monkeypatch.setattr(app_deploy, "_aimeat_call", fake_call)
    monkeypatch.setattr(crew_registry, "install_crew_def", fake_install)
    monkeypatch.setattr(forge, "is_crew_running", lambda name: calls["running"])
    return calls


def test_deploy_happy_path_installs_namespaced_and_writes_live_key(deploy_harness):
    report = deploy_app_agent("crew-forge", _deploy_task())
    assert "notes-summarizer-agent-notes-demo" in report
    assert len(deploy_harness["installs"]) == 1
    assert deploy_harness["installs"][0]["agent_name"] == "notes-summarizer-agent-notes-demo"
    writes = [p for t, p in deploy_harness["aimeat"] if t == "aimeat_memory_write"]
    assert writes and writes[0]["key"] == "agents.notes-summarizer-agent-notes-demo.deploy"
    assert writes[0]["value"]["status"] == "live"
    assert writes[0]["value"]["app_id"] == "agent-notes-demo"
    assert writes[0]["visibility"] == "owner"


def test_redeploy_of_live_agent_is_noop(deploy_harness):
    deploy_harness["running"] = True
    report = deploy_app_agent("crew-forge", _deploy_task())
    assert "no-op" in report
    assert deploy_harness["installs"] == []  # nothing re-installed, nothing re-launched
    writes = [p for t, p in deploy_harness["aimeat"] if t == "aimeat_memory_write"]
    assert writes and writes[0]["value"]["status"] == "live"  # key refreshed


def test_deploy_rejects_invalid_embedded_def(deploy_harness, monkeypatch):
    bad_app = json.loads(json.dumps(DEMO_APP))
    bad_app["cortex"]["agents"][0]["agents"][0]["tools"] = ["shell"]
    monkeypatch.setattr(
        app_deploy,
        "_aimeat_call",
        lambda a, t, p, **k: bad_app if t == "aimeat_app_get" else {"ok": True},
    )
    with pytest.raises(DeployError, match="failed validation"):
        deploy_app_agent("crew-forge", _deploy_task())
    assert deploy_harness["installs"] == []  # nothing partial reached the fleet


def test_deploy_fails_loud_when_app_unreadable(own_fleet, monkeypatch):
    monkeypatch.setattr(app_deploy, "_aimeat_call", lambda *_a, **_k: None)
    with pytest.raises(DeployError, match="aimeat_app_get returned nothing"):
        deploy_app_agent("crew-forge", _deploy_task())


# --------------------------------------------------------------------------- #
# Undeploy — stop + remove materialized files + flip the key
# --------------------------------------------------------------------------- #
def test_undeploy_stops_removes_and_flips_key(own_fleet, monkeypatch, tmp_path):
    import crewaimeat.forge as forge

    deployed = "notes-summarizer-agent-notes-demo"
    (tmp_path / "crews").mkdir()
    (tmp_path / "crew_defs").mkdir()
    loader = tmp_path / "crews" / forge._fname(deployed)
    docfile = tmp_path / "crew_defs" / f"{deployed.replace('-', '_')}.json"
    loader.write_text("# loader", encoding="utf-8")
    docfile.write_text("{}", encoding="utf-8")

    stopped, writes = [], []
    monkeypatch.setattr(forge, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(forge, "stop_crew", lambda name: stopped.append(name) or f"Stopped '{name}'.")
    monkeypatch.setattr(app_deploy, "_aimeat_call", lambda a, t, p, **k: writes.append((t, p)) or {"ok": True})

    report = undeploy_app_agent("crew-forge", "agent-notes-demo", "notes-summarizer")
    assert stopped == [deployed]
    assert not loader.exists() and not docfile.exists()
    assert writes[0][0] == "aimeat_memory_write"
    assert writes[0][1]["key"] == f"agents.{deployed}.deploy"
    assert writes[0][1]["value"]["status"] == "undeployed"
    assert "undeployed" in report


def test_undeploy_requires_names(own_fleet):
    with pytest.raises(DeployError, match="app_id and agent_name"):
        undeploy_app_agent("crew-forge", "", "notes-summarizer")


# --------------------------------------------------------------------------- #
# crew-forge routing — the deploy scope reaches the deterministic deployer domain
# --------------------------------------------------------------------------- #
def _forge_domain(task: dict):
    from crew_fixtures import make_ctx

    from crews import crew_forge_crew

    ctx = make_ctx("deploy it")
    ctx.task = task
    return crew_forge_crew.build_domain(ctx)


def test_crew_forge_routes_deploy_task_to_deployer_domain():
    agents, tasks = _forge_domain(_deploy_task())
    assert len(agents) == 1 and len(tasks) == 1
    assert [t.name for t in agents[0].tools] == ["deploy_app_agent"]
    assert "deploy it" in tasks[0].description  # ctx.prompt injected


def test_crew_forge_routes_undeploy_task_to_undeployer_domain():
    agents, _tasks = _forge_domain(_scope_task("undeploy-app-agent", app_id="a", agent_name="n", owner=OWNER))
    assert [t.name for t in agents[0].tools] == ["undeploy_app_agent"]


def test_crew_forge_without_deploy_scope_keeps_normal_build_path():
    agents, _tasks = _forge_domain({"id": "t", "title": "deploy-app-agent", "description": "make me an agent"})
    roles = [a.role for a in agents]
    assert "App Agent Deployer" not in roles  # a title alone never routes to the deployer

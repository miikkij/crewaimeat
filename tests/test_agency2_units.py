"""aimeat-agency 2.0 — the parts that are code, tested without a node, a model or a subprocess.

The live chain (bundled engine → two local nodes → register → self-publish → run → monitor) is proven
separately against throwaway local nodes; these pin the logic that decides what the person sees.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from crewaimeat.agency2 import author, connect, health, migrate, node, paths, store


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_AGENCY_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("AIMEAT_AGENCY_NODE_DIR", raising=False)
    monkeypatch.delenv("AIMEAT_AGENCY_CONNECTOR_DIR", raising=False)
    return tmp_path


# ── store ────────────────────────────────────────────────────────────────────


def test_url_is_normalized_and_junk_refused():
    assert store.normalize_url("aimeat.io/") == "https://aimeat.io"
    assert store.normalize_url("http://localhost:40561/v1/health") == "http://localhost:40561"
    with pytest.raises(store.StoreError):
        store.normalize_url("ftp://x")


@pytest.mark.parametrize("bad", ["ab", "Mapmaker", "-x-", "a b c", "x" * 65])
def test_agent_name_follows_the_connector_rule(bad):
    with pytest.raises(store.StoreError):
        store.check_agent_name(bad)


def test_one_name_per_machine_across_instances():
    store.add_instance("http://localhost:40561", "teemu")
    store.add_instance("http://localhost:40562", "teemu")
    store.add_agent("uutiset", "http://localhost:40561")
    with pytest.raises(store.StoreError, match="one name per machine"):
        store.add_agent("uutiset", "http://localhost:40562")
    # the same instance again is an update, not a duplicate
    store.add_agent("uutiset", "http://localhost:40561", description="uusi")
    assert [a["description"] for a in store.agents()] == ["uusi"]


def test_instance_with_agents_cannot_be_removed():
    store.add_instance("http://localhost:40561", "teemu")
    store.add_agent("uutiset", "http://localhost:40561")
    with pytest.raises(store.StoreError):
        store.remove_instance("http://localhost:40561")


def test_key_lives_in_the_data_dir_env_file():
    paths.set_env_key("sk-or-test")
    assert (paths.data_dir() / ".env").read_text(encoding="utf-8").strip() == "OPENROUTER_API_KEY=sk-or-test"
    assert paths.get_env_key() == "sk-or-test"


# ── connector output ─────────────────────────────────────────────────────────

# Verbatim from connector 3.17.0 against a local node (2026-09-18).
_CONNECT_OUT = """AIMEAT Agent Connector
Requesting device authorization...
Verification code: Z6FH-93Z3
Open http://localhost:40561/v1/agents/verify to approve.
Polling for approval (every 5s)...
Approved!
Token stored (aimeat:probe-a@teemu)
"""


def test_connector_output_is_parsed():
    p = connect.parse(_CONNECT_OUT)
    assert p == {
        "code": "Z6FH-93Z3",
        "verify_url": "http://localhost:40561/v1/agents/verify",
        "approved": True,
        "stored": True,
    }
    assert connect.parse("Requesting device authorization...\n")["code"] is None


def test_connect_never_spawns_under_pytest():
    with pytest.raises(RuntimeError):
        connect.start("x-agent", "http://localhost:40561", "teemu")


# ── authoring ────────────────────────────────────────────────────────────────

_VALID = {
    "agent_name": "whatever",
    "process": "sequential",
    "agents": [{"name": "w", "role": "Writer", "goal": "Summarise", "backstory": "Short and plain."}],
    "tasks": [
        {"id": "s", "agent": "w", "description": "Summarise: {{ctx.prompt}}", "expected_output": "Three sentences."}
    ],
}


def test_author_retries_with_the_validator_errors_and_forces_the_name():
    prompts = []
    answers = ["not json at all", json.dumps({**_VALID, "tasks": []}), json.dumps(_VALID)]

    def call(p):
        prompts.append(p)
        return answers[len(prompts) - 1]

    res = author.author("uutiset", "tiivistä uutiset", call=call)
    assert res["ok"] and res["attempts"] == 3
    assert res["doc"]["agent_name"] == "uutiset"
    assert "role.task-runner" in res["doc"]["tags"]
    assert "REJECTED" in prompts[2]  # the second answer's errors were handed back


def test_author_keeps_the_best_attempt_when_none_is_valid():
    one_error = {**_VALID, "tasks": [{**_VALID["tasks"][0], "description": "no prompt placeholder"}]}
    answers = [json.dumps(one_error), "garbage", "garbage"]
    res = author.author("uutiset", "x", call=lambda p, it=iter(answers): next(it))
    assert not res["ok"]
    assert res["doc"] is not None and res["doc"]["tasks"][0]["description"] == "no prompt placeholder"
    assert res["errors"]


def test_author_reports_a_model_failure():
    def boom(_):
        raise TimeoutError("slow")

    res = author.author("uutiset", "x", call=boom)
    assert not res["ok"] and "TimeoutError" in res["errors"][0]


def test_summary_is_plain_language():
    doc = {**_VALID, "agents": [{**_VALID["agents"][0], "tools": ["web"]}]}
    s = author.summary(doc, "fi")
    assert s["members"] == [{"role": "Writer", "goal": "Summarise", "tools": ["hakee verkosta"]}]
    assert s["steps"] == [{"who": "Writer", "delivers": "Three sentences."}]


# ── health ───────────────────────────────────────────────────────────────────


def _serve_json(home, agents):
    (home / "serve.json").write_text(json.dumps({"schema_version": 2, "port": 1, "agents": agents}), encoding="utf-8")


def test_health_without_a_key_offers_the_key_fix():
    rows = health.check("en", openrouter_fn=lambda: {"ok": False, "detail": "no key saved"})
    key = next(r for r in rows if r["id"] == "openrouter")
    assert key["level"] == "error" and key["fix"] == "key"


def test_health_flags_scope_mode_and_stopped_runtime(_isolated):
    store.add_instance("http://localhost:40561", "teemu")
    store.add_agent("uutiset", "http://localhost:40561")
    store.update_agent("uutiset", connected=True)
    _serve_json(
        _isolated / "home",
        [{"agent": "uutiset", "gaii": "uutiset#teemu@n", "node_url": "http://localhost:40561", "transport": "tunnel"}],
    )
    roster = [{"name": "uutiset", "mode": "interactive", "default_scopes": ["memory:read"]}]
    rows = health.check(
        "en",
        openrouter_fn=lambda: {"ok": True, "detail": "", "usage": 0.5, "limit_remaining": 9.0},
        health_fn=lambda url: {"ok": True, "node_id": "n", "detail": {}},
        roster_fn=lambda name: roster,
        running_fn=lambda name: None,
    )
    row = next(r for r in rows if r["id"] == "agent:uutiset")
    assert row["level"] == "error" and row["fix"] == "reconnect:uutiset"
    assert "agent:write" in row["detail"] and "interactive" in row["detail"] and "not running" in row["detail"]


def test_health_names_a_revoked_key_as_a_refusal(_isolated):
    store.add_instance("http://localhost:40561", "teemu")
    store.add_agent("uutiset", "http://localhost:40561")
    store.update_agent("uutiset", connected=True)
    _serve_json(_isolated / "home", [{"agent": "uutiset", "gaii": "g", "node_url": "u", "transport": "tunnel"}])

    def refused(name):
        raise node.Refused(401, "UNAUTHORIZED", "token revoked")

    rows = health.check(
        "en",
        openrouter_fn=lambda: {"ok": True, "detail": ""},
        health_fn=lambda url: {"ok": True, "node_id": "n", "detail": {}},
        roster_fn=refused,
        running_fn=lambda name: {"pid": 1},
    )
    row = next(r for r in rows if r["id"] == "agent:uutiset")
    assert row["level"] == "error" and "refused" in row["detail"] and row["fix"] == "reconnect:uutiset"


def test_health_warns_about_an_instance_without_the_live_connection(_isolated):
    store.add_instance("http://localhost:40561", "teemu")
    store.add_agent("uutiset", "http://localhost:40561")
    store.update_agent("uutiset", connected=True)
    _serve_json(_isolated / "home", [{"agent": "uutiset", "gaii": "g", "node_url": "u", "transport": "direct"}])
    rows = health.check(
        "en",
        openrouter_fn=lambda: {"ok": True, "detail": ""},
        health_fn=lambda url: {"ok": True, "node_id": "n", "detail": {}},
        roster_fn=lambda n: [{"name": "uutiset", "mode": "task-runner", "default_scopes": ["agent:write"]}],
        running_fn=lambda n: {"pid": 1},
    )
    inst = next(r for r in rows if r["id"].startswith("instance:"))
    assert inst["level"] == "warn" and "30 s" in inst["detail"]
    assert next(r for r in rows if r["id"] == "agent:uutiset")["level"] == "ok"


# ── migration from 0.8.x ─────────────────────────────────────────────────────


def test_old_brains_are_listed_once_and_never_modified(_isolated):
    db = _isolated / "home" / "brains.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE brains (agent_name TEXT PRIMARY KEY, template_id TEXT, prose TEXT, policy TEXT, title TEXT, version INTEGER, created REAL, updated REAL)"
    )
    con.execute(
        "INSERT INTO brains VALUES ('news-watcher','topic-watcher','Seuraa AI-rahoitusta','{}','Uutisvahti',1,0,0)"
    )
    con.commit()
    con.close()
    before = db.read_bytes()
    st = migrate.status()
    assert st["pending"] and st["old_agents"] == [
        {
            "name": "news-watcher",
            "template": "topic-watcher",
            "description": "Seuraa AI-rahoitusta",
            "title": "Uutisvahti",
        }
    ]
    migrate.acknowledge()
    assert migrate.status()["pending"] is False
    assert db.read_bytes() == before


def test_no_old_install_means_no_notice():
    assert migrate.status()["pending"] is False


# ── the server ───────────────────────────────────────────────────────────────


def _client():
    from fastapi.testclient import TestClient

    from crewaimeat.agency2.app import create_app

    return TestClient(create_app("tok"))


def test_every_api_call_needs_the_token():
    c = _client()
    assert c.get("/api/state").status_code == 401
    assert c.get("/").status_code == 401
    assert c.get("/?boot=tok").status_code == 200
    r = c.get("/api/state", headers={"Authorization": "Bearer tok"})
    assert r.status_code == 200 and r.json()["has_key"] is False


def test_a_bad_key_is_refused_before_it_is_saved(monkeypatch):
    from crewaimeat.agency2 import app as app_mod

    monkeypatch.setattr(
        app_mod.health, "openrouter", lambda key, **kw: {"ok": False, "detail": "OpenRouter refused the key"}
    )
    c = _client()
    r = c.post("/api/key", json={"key": "sk-bad"}, headers={"Authorization": "Bearer tok"})
    assert r.status_code == 400 and "refused" in r.json()["detail"]
    assert paths.get_env_key() == ""


def test_an_instance_that_does_not_answer_is_not_added(monkeypatch):
    from crewaimeat.agency2 import app as app_mod

    monkeypatch.setattr(app_mod.node, "health", lambda url: {"ok": False, "node_id": None, "detail": "ConnectionError"})
    c = _client()
    r = c.post("/api/instances", json={"url": "localhost:1", "owner": "teemu"}, headers={"Authorization": "Bearer tok"})
    assert r.status_code == 400 and store.instances() == []


def test_runner_refuses_a_gaii():
    from crewaimeat.agency2 import runner

    with pytest.raises(SystemExit):
        runner.main(["probe-a#teemu@node"])


# ── the environment belongs to the app ───────────────────────────────────────


def test_a_stray_dotenv_found_by_a_library_is_taken_back_out(tmp_path, monkeypatch):
    """litellm/crewai call a bare load_dotenv() that walks up from the LIBRARY's folder; in a dev
    checkout that is the checkout's .env (measured: NVIDIA_KEY, then a free OPENROUTER_MODEL)."""
    from crewaimeat.agency2 import engine

    lib = tmp_path / "checkout" / ".venv" / "site-packages" / "crewai"
    lib.mkdir(parents=True)
    stray = tmp_path / "checkout" / ".env"
    stray.write_text("NVIDIA_KEY=x\nOPENROUTER_MODEL=some/free:model\nKEEP_ME=launch\n", encoding="utf-8")
    assert engine._stray_dotenv(lib) == stray
    paths.set_env_key("sk-or-mine")
    assert engine._stray_dotenv(paths.data_dir()) is None  # our own .env is never "stray"

    monkeypatch.setattr(engine, "_stray_dotenv", lambda start: stray)
    launch = (set(__import__("os").environ) - {"NVIDIA_KEY", "OPENROUTER_MODEL"}) | {"KEEP_ME"}
    monkeypatch.setattr("crewaimeat.agency2.LAUNCH_ENV", frozenset(launch))
    monkeypatch.setenv("NVIDIA_KEY", "x")
    monkeypatch.setenv("OPENROUTER_MODEL", "some/free:model")
    monkeypatch.setenv("KEEP_ME", "launch")
    import os

    removed = engine.openrouter_only()
    assert "NVIDIA_KEY" in removed and "OPENROUTER_MODEL" in removed
    assert "NVIDIA_KEY" not in os.environ and "OPENROUTER_MODEL" not in os.environ
    assert os.environ["KEEP_ME"] == "launch"  # it was there at launch: not ours to remove
    assert os.environ["OPENROUTER_API_KEY"] == "sk-or-mine"


def test_children_never_get_the_other_providers(monkeypatch):
    from crewaimeat.agency2 import engine

    monkeypatch.setenv("NVIDIA_KEY", "x")
    monkeypatch.setenv("USE_XAI", "1")
    env = engine.child_env()
    assert "NVIDIA_KEY" not in env and "USE_XAI" not in env and env["LITELLM_MODE"] == "PRODUCTION"


def test_healthz_is_open_and_says_the_version():
    r = _client().get("/healthz")
    assert r.status_code == 200 and r.json() == {"ok": True, "version": "2.0.0"}


def test_health_says_a_stopped_agent_has_no_definition(_isolated):
    """An agent made on the node has nothing to be until it is described; 'not running' alone would
    send the person to the Start button, which cannot work."""
    store.add_instance("http://localhost:40561", "teemu")
    store.add_agent("myyntiraportti", "http://localhost:40561")
    store.update_agent("myyntiraportti", connected=True)
    _serve_json(_isolated / "home", [{"agent": "myyntiraportti", "gaii": "g", "node_url": "u", "transport": "tunnel"}])
    rows = health.check(
        "en",
        openrouter_fn=lambda: {"ok": True, "detail": ""},
        health_fn=lambda url: {"ok": True, "node_id": "n", "detail": {}},
        roster_fn=lambda n: [{"name": "myyntiraportti", "mode": "task-runner", "default_scopes": ["agent:write"]}],
        running_fn=lambda n: None,
        log_fn=lambda n: "[myyntiraportti] CANNOT START — crews.registry.myyntiraportti holds no crew definition",
    )
    row = next(r for r in rows if r["id"] == "agent:myyntiraportti")
    assert "no definition" in row["detail"] and row["fix"] == "agent:myyntiraportti"

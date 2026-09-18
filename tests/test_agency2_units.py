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
        "already": False,
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
        roster_fn=lambda n: [
            {"name": "uutiset", "mode": "task-runner", "default_scopes": list(connect.REQUIRED_SCOPES)}
        ],
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
        roster_fn=lambda n: [
            {"name": "myyntiraportti", "mode": "task-runner", "default_scopes": list(connect.REQUIRED_SCOPES)}
        ],
        running_fn=lambda n: None,
        log_fn=lambda n: "[myyntiraportti] CANNOT START — crews.registry.myyntiraportti holds no crew definition",
    )
    row = next(r for r in rows if r["id"] == "agent:myyntiraportti")
    assert "no definition" in row["detail"] and row["fix"] == "agent:myyntiraportti"


# ── schedules ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("preset", "time", "day", "cron", "fi"),
    [
        ("daily", "07:05", 1, "5 7 * * *", "joka päivä klo 07.05"),
        ("weekdays", "8:00", 1, "0 8 * * 1-5", "arkisin klo 08.00"),
        ("weekly", "09:30", 1, "30 9 * * 1", "maanantaisin klo 09.30"),
        ("weekly", "18:00", 7, "0 18 * * 0", "sunnuntaisin klo 18.00"),
        ("hourly", "", 1, "0 * * * *", "joka tasatunti"),
    ],
)
def test_the_cron_is_built_by_code_and_read_back_in_words(preset, time, day, cron, fi):
    from crewaimeat.agency2 import schedule

    assert schedule.build_cron(preset, time, day) == cron
    assert schedule.describe(cron, "fi") == fi


def test_bad_schedule_input_is_refused():
    from crewaimeat.agency2 import schedule

    with pytest.raises(schedule.ScheduleError):
        schedule.build_cron("daily", "25:00")
    with pytest.raises(schedule.ScheduleError):
        schedule.build_cron("monthly", "07:00")
    assert schedule.describe("*/5 * * * *", "fi") == "*/5 * * * *"  # not ours: shown as it is, not guessed


def test_the_list_shows_only_this_agents_task_schedules(monkeypatch):
    from crewaimeat.agency2 import schedule

    rows = {
        "schedules": [
            {
                "id": "a",
                "type": "agent_task",
                "agentName": "uutiset",
                "cron": "0 7 * * *",
                "enabled": True,
                "taskTemplate": {"title": "t", "description": "Kerää uutiset"},
            },
            {"id": "b", "type": "agent_task", "agentName": "toinen", "cron": "0 8 * * *"},
            {"id": "c", "type": "ai", "cron": "0 9 * * *"},
        ]
    }
    monkeypatch.setattr(schedule.node, "call", lambda agent, tool, args=None, **kw: rows)
    got = schedule.list_for("uutiset", "fi")
    assert [(g["id"], g["when"], g["what"]) for g in got] == [("a", "joka päivä klo 07.00", "Kerää uutiset")]


def test_schedules_need_their_scopes_at_approval():
    assert {"task:write", "workflow:read"} <= set(connect.REQUIRED_SCOPES)


def test_a_reconnect_sets_the_old_key_aside_where_the_connector_cannot_see_it(_isolated):
    tokens = _isolated / "home" / "tokens"
    tokens.mkdir()
    (tokens / "uutiset@teemu.token").write_text("old", encoding="utf-8")
    src, moved = connect._set_aside("uutiset", "teemu")
    assert src.name == "uutiset@teemu.token" and moved.read_text(encoding="utf-8") == "old"
    assert moved.parent.name == ".replaced" and not (tokens / "uutiset@teemu.token").exists()
    assert [p.name for p in tokens.glob("*.token")] == []  # what the connector lists: nothing
    assert connect._set_aside("uutiset", "teemu") is None


def test_already_connected_is_parsed():
    assert connect.parse("Already connected! Token is valid.\n")["already"] is True


def test_the_single_model_path_asks_openrouter_for_the_real_cost(monkeypatch, tmp_path):
    """The installed app has no llm_providers.json; without usage.include every call reached the
    ledger unpriced (cost_usd 0, unpriced_calls = calls — measured on a local node)."""
    import crewai.llm  # noqa: F401 — its import-time load_dotenv runs NOW, before the env is cleared

    from crewaimeat import llm

    monkeypatch.chdir(tmp_path)  # no llm_providers.json here
    for v in ("LLM_PROVIDERS_FILE", "NVIDIA_KEY", "USE_XAI", "OPENROUTER_FALLBACK_MODELS"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    got = llm._build_llm(False, 0.3, None)
    extra = (getattr(got, "additional_params", None) or {}).get("extra_body") or {}
    assert extra.get("usage") == {"include": True}
    assert getattr(got, "is_litellm", False) is True  # the native class drops usage.cost


# ── costs ────────────────────────────────────────────────────────────────────


def test_costs_come_from_the_ledger_and_unpriced_is_not_free(monkeypatch):
    from crewaimeat.agency2 import costs

    def fake(agent, path, params=None, **kw):
        assert path == "/v1/ledger/usage"
        if params.get("group_by") == "agent":
            return {
                "groups": [
                    {
                        "key": "uutiset#teemu@n",
                        "cost_usd": 0.0123,
                        "calls": 4,
                        "total_tokens": 900,
                        "unpriced_calls": 1,
                    },
                ]
            }
        return {
            "groups": [
                {
                    "key": "deepseek/deepseek-v4-pro",
                    "cost_usd": 0.0123,
                    "calls": 4,
                    "total_tokens": 900,
                    "unpriced_calls": 1,
                }
            ],
            "totals": {"cost_usd": 0.0123, "calls": 4, "total_tokens": 900, "unpriced_calls": 1},
        }

    monkeypatch.setattr(costs.node, "rest_get", fake)
    assert costs.by_agent("uutiset") == {
        "uutiset": {"cost_usd": 0.0123, "calls": 4, "tokens": 900, "unpriced_calls": 1}
    }
    one = costs.for_agent("uutiset")
    assert one["total"]["unpriced_calls"] == 1 and one["models"][0]["model"] == "deepseek/deepseek-v4-pro"


def test_costs_need_their_scope_at_approval():
    assert "wallet:read" in connect.REQUIRED_SCOPES


def test_every_ui_string_exists_in_finnish_and_english():
    """A missing key renders as the raw key (`cost_unavailable`) — measured in a screenshot. The UI
    is for a person who reads Finnish; every t("…") and data-t="…" must be in both tables."""
    import re
    from pathlib import Path

    html = (Path(__file__).parent.parent / "src/crewaimeat/agency2/static/index.html").read_text(encoding="utf-8")
    used = set(re.findall(r'\bt\("([a-z0-9_]+)"\)', html)) | set(re.findall(r'data-t="([a-z0-9_]+)"', html))
    used |= {f"day{d}" for d in range(1, 8)}  # built as t("day"+d)
    fi = html[html.index(" fi:{") : html.index(" en:{")]
    en = html[html.index(" en:{") : html.index("const qs")]
    defined = lambda block: set(re.findall(r"\b([a-z0-9_]+):\"", block))  # noqa: E731
    assert not (used - defined(fi)), f"missing in Finnish: {sorted(used - defined(fi))}"
    assert not (used - defined(en)), f"missing in English: {sorted(used - defined(en))}"


def test_children_carry_the_environment_hook():
    """The spawner's run_once workers are not agency2 code; the sitecustomize on their PYTHONPATH is
    what keeps a stray .env out of them."""
    from crewaimeat.agency2 import engine

    env = engine.child_env()
    hook = env["PYTHONPATH"].split(__import__("os").pathsep)[0]
    text = (__import__("pathlib").Path(hook) / "sitecustomize.py").read_text(encoding="utf-8")
    assert "_silence_bare_load_dotenv" in text and "import crewai" not in text


def test_the_spawn_roster_is_this_apps_connected_agents_only():
    from crewaimeat.agency2 import spawn

    store.add_instance("http://localhost:40561", "teemu")
    store.add_agent("uutiset", "http://localhost:40561")
    store.add_agent("pysaytetty", "http://localhost:40561")
    store.add_agent("kesken", "http://localhost:40561")
    store.update_agent("uutiset", connected=True)
    store.update_agent("pysaytetty", connected=True, autostart=False)
    assert spawn.roster() == ["uutiset"]


# ── what reaches the disk and a command line ─────────────────────────────────


@pytest.mark.parametrize("bad", ["../../x", r"..\..\x", "a/../../b"])
def test_no_path_leaves_the_apps_folders(bad):
    with pytest.raises(ValueError):
        paths.contained(paths.data_dir() / bad / ".." / ".." / ".." / "escape.json")


def test_a_name_the_app_does_not_know_never_reaches_a_command_line():
    from crewaimeat.agency2 import procs

    with pytest.raises(ValueError):
        procs.log_path("../../etc")
    store.add_instance("http://localhost:40561", "teemu")
    store.add_agent("uutiset", "http://localhost:40561")
    assert procs.log_path("uutiset").name == "uutiset.log"


def test_logs_are_selected_from_the_folder_not_built_from_the_name(_isolated):
    from crewaimeat.agency2 import procs

    (paths.logs_dir() / "uutiset.log").write_text("oma", encoding="utf-8")
    runs = _isolated / "home" / "spawn" / "logs"
    runs.mkdir(parents=True)
    (runs / "uutiset-uutiset-1a.log").write_text("ajo", encoding="utf-8")
    (runs / "muu-muu-1b.log").write_text("vieras", encoding="utf-8")
    out = procs.tail("uutiset")
    assert "oma" in out and "ajo" in out and "vieras" not in out
    assert procs.tail("../uutiset") == ""


def test_a_url_name_the_app_does_not_know_is_404_everywhere():
    c = _client()
    h = {"Authorization": "Bearer tok"}
    for method, path in [
        ("POST", "/api/agents/..%2F..%2Fx/start"),
        ("POST", "/api/agents/ghost/reconnect"),
        ("GET", "/api/agents/ghost"),
        ("DELETE", "/api/agents/ghost"),
        ("GET", "/api/agents/ghost/schedules"),
    ]:
        r = c.request(method, path, headers=h)
        assert r.status_code == 404, (method, path, r.status_code)

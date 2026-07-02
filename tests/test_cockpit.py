"""cockpit — the agency control server. Node-independent surface only (brains, templates, local memory,
sync, dry-run preview, token gating). Fleet controls + upward publish need the live node and are not
exercised here. Isolated to a tmp AIMEAT_HOME."""

from __future__ import annotations

import pytest

TOKEN = "test-token-123"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)  # _web_tools() -> [] (no network in dry-run)
    from starlette.testclient import TestClient

    from crewaimeat.agency.cockpit import create_app

    c = TestClient(create_app(token=TOKEN))
    c.headers.update({"Authorization": f"Bearer {TOKEN}"})
    return c


def test_index_serves_ui_with_token(client):
    r = client.get("/", headers={"Authorization": ""})  # the page itself is open (localhost-only)
    assert r.status_code == 200
    assert "aimeat-agency" in r.text
    assert TOKEN in r.text  # the per-launch token is injected into the page
    assert "__AGENCY_TOKEN__" not in r.text  # placeholder fully replaced


def test_healthz_is_open(client):
    # strip the auth header — /healthz must answer without a token
    r = client.get("/healthz", headers={"Authorization": ""})
    assert r.status_code == 200 and r.json()["ok"] is True


def test_api_requires_token(client):
    assert client.get("/api/templates", headers={"Authorization": ""}).status_code == 401
    assert client.get("/api/templates", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/templates").status_code == 200  # fixture sets the right token


def test_templates_lists_topic_watcher(client):
    ids = {t["id"] for t in client.get("/api/templates").json()["templates"]}
    assert "topic-watcher" in ids


def test_setup_status_shape(client, monkeypatch):
    # Onboarding wizard contract: fresh home → owner not set, no brains, model not ready.
    monkeypatch.delenv("AIMEAT_OWNER", raising=False)  # else a loaded .env owner makes owner_set True
    monkeypatch.setattr("crewaimeat.agency.cockpit._ollama_probe", lambda: (False, []))
    monkeypatch.setattr("crewaimeat.agency.cockpit._has_openrouter_key", lambda: False)
    s = client.get("/api/setup/status").json()
    for k in ("owner_set", "ollama", "openrouter_key", "brain_count", "first_agent_running"):
        assert k in s
    assert s["owner_set"] is False and s["brain_count"] == 0
    assert s["ollama"]["running"] is False and s["ollama"]["has_model"] is False and s["ollama"]["models"] == []
    assert s["ollama"]["default_model"]  # the wizard pulls/picks this light default


def test_setup_status_sees_default_model(client, monkeypatch):
    # has_model is True when a model of the default family is present
    from crewaimeat.agency.cockpit import DEFAULT_OLLAMA_MODEL

    fam = DEFAULT_OLLAMA_MODEL.split(":")[0]
    monkeypatch.setattr("crewaimeat.agency.cockpit._ollama_probe", lambda: (True, [f"{fam}:latest", "qwen2.5:7b"]))
    monkeypatch.setattr("crewaimeat.agency.cockpit._has_openrouter_key", lambda: False)
    s = client.get("/api/setup/status").json()
    assert s["ollama"]["running"] is True and s["ollama"]["has_model"] is True


def test_reset_wipes_state(client, monkeypatch, tmp_path):
    # chdir so the 'crews' glob in reset can never touch the dev repo's real crews/
    monkeypatch.chdir(tmp_path)
    import crewaimeat.tui.actions as actions
    from crewaimeat import brains

    monkeypatch.setattr(actions, "stop_fleet", lambda: "stopped")
    brains.save_brain("reset-test-agent", "topic-watcher", prose="hi")  # a real brain in the DB
    (tmp_path / "agency_account.json").write_text("{}", encoding="utf-8")
    assert brains.list_brains(), "precondition: a brain exists"
    r = client.post("/api/reset").json()
    assert r["ok"] is True
    assert brains.list_brains() == []  # the bug was: brains/agents SURVIVED reset
    assert not (tmp_path / "agency_account.json").exists()  # plain files still go too


def test_openrouter_key_requires_value(client):
    assert client.post("/api/setup/openrouter-key", json={"key": "  "}).status_code == 400


def test_open_external_rejects_non_http(client):
    assert client.post("/api/open", json={"url": "file:///etc/passwd"}).status_code == 400
    assert client.post("/api/open", json={"url": "javascript:alert(1)"}).status_code == 400


def test_open_external_opens_http(client, monkeypatch):
    opened = {}
    monkeypatch.setattr("webbrowser.open", lambda u: opened.setdefault("url", u))
    r = client.post("/api/open", json={"url": "https://aimeat.io"})
    assert r.status_code == 200 and opened["url"] == "https://aimeat.io"


def test_update_check(client, monkeypatch):
    monkeypatch.setattr("crewaimeat.agency.cockpit.COCKPIT_VERSION", "0.8.2")
    monkeypatch.setattr("crewaimeat.agency.cockpit._latest_agency_release", lambda: ("0.9.0", "http://x/rel"))
    u = client.get("/api/update-check").json()
    assert u["update_available"] is True and u["latest"] == "0.9.0" and u["url"] == "http://x/rel"
    monkeypatch.setattr("crewaimeat.agency.cockpit._latest_agency_release", lambda: ("0.8.2", "http://x"))
    assert client.get("/api/update-check").json()["update_available"] is False


def test_shutdown_stops_fleet_no_selfexit(client, monkeypatch):
    # Without the shell's env token, /api/shutdown must NOT self-exit (so tests/dev stay alive).
    monkeypatch.delenv("AIMEAT_AGENCY_TOKEN", raising=False)
    called = {}
    import crewaimeat.tui.actions as actions

    monkeypatch.setattr(actions, "stop_fleet", lambda: called.setdefault("v", "stopped 3"))
    r = client.post("/api/shutdown")
    detail = r.json()["detail"]
    # fleet stopped + the ollama-unload note appended (skipped under pytest — never the live models)
    assert r.status_code == 200 and detail.startswith("stopped 3") and called["v"]
    assert "ollama unload skipped (pytest)" in detail


def test_models_catalogue(client, monkeypatch):
    from crewaimeat import llm

    monkeypatch.setattr("crewaimeat.agency.cockpit._ollama_models", lambda: [])  # isolate from real local Ollama
    monkeypatch.setattr(
        llm,
        "available_models",
        lambda: [
            {
                "label": "openrouter:x/y",
                "id": "x/y",
                "context": 8000,
                "provider": {"type": "openrouter", "models": [{"id": "x/y"}]},
            }
        ],
    )
    models = client.get("/api/models").json()["models"]
    assert models[0]["label"] == "openrouter:x/y"
    assert models[0]["spec"]["kind"] == "model" and models[0]["spec"]["provider"]["type"] == "openrouter"


def test_models_include_local_ollama(client, monkeypatch):
    import requests

    from crewaimeat import llm

    monkeypatch.setattr(llm, "available_models", lambda: [])  # no cloud models, just Ollama

    class _R:
        status_code = 200

        def json(self):
            return {"models": [{"name": "gemma4"}, {"name": "qwen3.6"}]}

    monkeypatch.setattr(requests, "get", lambda url, timeout=2: _R())
    models = client.get("/api/models").json()["models"]
    assert "ollama:gemma4" in [m["label"] for m in models]
    assert models[0]["local"] is True
    assert models[0]["spec"]["provider"]["type"] == "ollama"


def test_token_via_query_param_for_sse(client):
    # EventSource can't set headers, so the token may arrive as ?token= — strip the header to prove it
    assert client.get(f"/api/templates?token={TOKEN}", headers={"Authorization": ""}).status_code == 200
    assert client.get("/api/templates?token=wrong", headers={"Authorization": ""}).status_code == 401


def test_tasks_agent_not_attached(client, monkeypatch):
    client.post("/api/brains", json={"agent_name": "watcher", "template_id": "topic-watcher"})
    import crewaimeat.aimeat_crew as ac

    monkeypatch.setattr(ac, "_aimeat_call", lambda *a, **k: None)  # connector swallowed the error
    assert client.get("/api/agents/watcher/tasks").json()["error"] == "agent_not_attached"


def test_templates_localized_fi(client):
    # look up topic-watcher by id (the list is sorted, so it isn't necessarily first)
    en = next(t for t in client.get("/api/templates?lang=en").json()["templates"] if t["id"] == "topic-watcher")
    fi = next(t for t in client.get("/api/templates?lang=fi").json()["templates"] if t["id"] == "topic-watcher")
    assert en["title"] == "Topic watcher" and fi["title"] == "Aiheen vahti"
    assert "aiheesta" in fi["default_prose"]  # the starting prose is Finnish (generic: topic from the task)
    assert "fi" in fi["languages"]


def test_account_ignores_ambient_env(client, monkeypatch):
    # The agency account must NOT come from a stray AIMEAT_OWNER env — that would silently skip onboarding
    # on a dev/system machine that happens to have it set. Only an explicit connect sets the owner.
    monkeypatch.setenv("AIMEAT_OWNER", "happydude500001")
    acc = client.get("/api/account").json()
    assert acc["owner_set"] is False and acc["owner"] is None
    assert acc["node"].endswith("aimeat.io")
    client.post("/api/account/connect", json={"owner": "jdoe2026"})
    assert client.get("/api/account").json()["owner"] == "jdoe2026"


def test_fleet_logs_empty_when_no_log(client):
    # an agent that never ran has no log file -> empty lines, no error
    r = client.get("/api/fleet/never-ran/logs").json()
    assert r["file"] is None and r["lines"] == []


def test_first_run_connect_flow(client, monkeypatch):
    monkeypatch.delenv("AIMEAT_OWNER", raising=False)
    # fresh install: no owner -> the UI must show the Connect screen
    assert client.get("/api/account").json()["owner_set"] is False
    # connecting sets the owner + node new agents register under
    r = client.post("/api/account/connect", json={"owner": "happydude500001", "node": "https://aimeat.io"})
    assert r.json()["owner_set"] is True
    acc = client.get("/api/account").json()
    assert acc["owner"] == "happydude500001" and acc["owner_set"] is True


def test_agent_auth_status(client, tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    client.post("/api/account/connect", json={"owner": "happydude500001"})
    # no token yet -> not authorized (the gate before running)
    assert client.get("/api/agents/watch-1/auth-status").json() == {
        "agent": "watch-1",
        "has_token": False,
        "authorized": False,
    }
    # simulate the owner approving it: the connector writes the token file
    toks = tmp_path / "tokens"
    toks.mkdir()
    (toks / "watch-1@happydude500001.token").write_text("tok", encoding="utf-8")
    monkeypatch.setattr("crewaimeat.aimeat_crew._auth_alive", lambda *a, **k: None)  # no live probe offline
    s = client.get("/api/agents/watch-1/auth-status").json()
    assert s["has_token"] is True and s["authorized"] is True


def test_activity_log_records_brain_saves_with_diff(client, tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    client.post("/api/brains", json={"agent_name": "watcher", "template_id": "topic-watcher", "prose": "v1"})
    client.patch("/api/brains/watcher", json={"prose": "v2", "policy": {"autonomy": "act"}})
    evs = client.get("/api/agents/watcher/activity").json()["events"]
    kinds = [e["kind"] for e in evs]
    assert kinds == ["brain_saved", "brain_saved"]  # newest first, two saves
    assert evs[1]["detail"]["changed"] == ["created"]  # first save
    assert "prose" in evs[0]["detail"]["changed"] and "policy.autonomy" in evs[0]["detail"]["changed"]


def test_offer_surface_and_publish(client, monkeypatch):
    client.post("/api/brains", json={"agent_name": "watcher", "template_id": "topic-watcher", "prose": "watch x"})
    # the template advertises an offer; not yet opted in
    info = client.get("/api/agents/watcher/offer").json()
    assert info["available"] is True and info["enabled"] is False
    assert info["offer"]["id"] == "topic-summary"

    # publishing advertises it (mock the node publish), flips enabled, logs an event
    from crewaimeat import offers

    published = {}
    monkeypatch.setattr(
        offers,
        "publish_meta_offer",
        lambda agent, meta, with_sample=False: published.update(agent=agent, id=meta["id"]) or (True, "ok"),
    )
    r = client.post("/api/agents/watcher/offer/publish", json={}).json()
    assert r["ok"] is True and r["offer_id"] == "topic-summary"
    assert published == {"agent": "watcher", "id": "topic-summary"}
    assert client.get("/api/agents/watcher/offer").json()["enabled"] is True
    assert any(e["kind"] == "offer_published" for e in client.get("/api/agents/watcher/activity").json()["events"])


def test_offer_publish_surfaces_failure(client, monkeypatch):
    client.post("/api/brains", json={"agent_name": "watcher", "template_id": "topic-watcher"})
    from crewaimeat import offers

    monkeypatch.setattr(offers, "publish_meta_offer", lambda *a, **k: (False, "node rejected the offer"))
    r = client.post("/api/agents/watcher/offer/publish", json={})
    assert r.status_code == 502 and "rejected" in r.json()["detail"]


def test_tasks_list_and_test_run(client, monkeypatch):
    client.post("/api/brains", json={"agent_name": "watcher", "template_id": "topic-watcher"})
    import crewaimeat.aimeat_crew as ac

    tid = "12345678-1234-1234-1234-1234567890ab"

    def fake_call(agent, tool, params):
        if tool == "aimeat_task_list":
            return {"tasks": [{"id": tid, "title": "Test: x", "status": "active", "createdAt": "2026-06-28"}]}
        if tool == "aimeat_task_create":
            return {"id": tid}
        if tool == "aimeat_memory_list":
            return {"items": [{"key": f"crews.watcher.{tid.split('-')[0]}.latest_output"}]}
        if tool == "aimeat_memory_read":
            return {"value": "the summary output"}
        return None

    monkeypatch.setattr(ac, "_aimeat_call", fake_call)

    # queue view
    tasks = client.get("/api/agents/watcher/tasks").json()["tasks"]
    assert tasks[0]["status"] == "active" and tasks[0]["id"] == tid

    # test run -> creates a task, returns its id, logs an event
    r = client.post("/api/agents/watcher/test", json={"prompt": "summarize fusion"}).json()
    assert r["task_id"] == tid
    assert any(e["kind"] == "test_run" for e in client.get("/api/agents/watcher/activity").json()["events"])

    # poll the result -> deliverable landed
    res = client.get(f"/api/agents/watcher/task/{tid}/result").json()
    assert res["done"] is True and res["result"] == "the summary output"


def test_agent_run_status_shape(client):
    s = client.get("/api/fleet/never-ran/status").json()
    assert s["agent"] == "never-ran" and s["status"] in ("down", "down (stale lock)")


def test_register_surfaces_code_and_url(client, monkeypatch):
    client.post("/api/account/connect", json={"owner": "happydude500001"})
    from crewaimeat import forge

    monkeypatch.setattr(
        forge,
        "register_agent",
        lambda agent, owner, url: (
            True,
            f"APPROVE to activate: open https://aimeat.io/verify and enter code WXYZ-1234 ({agent}/{owner}/{url})",
        ),
    )
    r = client.post("/api/agents/watch-1/register", json={}).json()
    assert r["code"] == "WXYZ-1234" and r["verify_url"] == "https://aimeat.io/verify"


def test_brain_crud_and_versioning(client):
    # create
    r = client.post("/api/brains", json={"agent_name": "watcher1", "template_id": "topic-watcher", "prose": "v1"})
    assert r.status_code == 200 and r.json()["version"] == 1

    # bad template -> 400
    assert client.post("/api/brains", json={"agent_name": "xyz", "template_id": "nope"}).status_code == 400

    # edit (patch) keeps template, bumps version
    r = client.patch("/api/brains/watcher1", json={"prose": "v2"})
    assert r.json()["version"] == 2 and r.json()["prose"] == "v2"

    # list + get
    assert "watcher1" in {b["agent_name"] for b in client.get("/api/brains").json()["brains"]}
    assert client.get("/api/brains/watcher1").json()["prose"] == "v2"
    assert client.get("/api/brains/missing").status_code == 404

    # history + rollback
    assert [v["version"] for v in client.get("/api/brains/watcher1/history").json()["versions"]] == [2, 1]
    r = client.post("/api/brains/watcher1/rollback", json={"version": 1})
    assert r.json()["version"] == 3 and r.json()["prose"] == "v1"

    # delete
    assert client.delete("/api/brains/watcher1").json()["deleted"] is True
    assert client.get("/api/brains/watcher1").status_code == 404


def test_dry_run_preview(client):
    client.post("/api/brains", json={"agent_name": "watch-x", "template_id": "topic-watcher", "prose": "watch quantum"})
    r = client.get("/api/brains/watch-x/dry-run")
    assert r.status_code == 200
    data = r.json()
    assert data["agents"][0]["role"] == "Topic Watcher"
    assert {"remember", "publish_memory"} <= set(data["agents"][0]["tools"])
    assert "watch quantum" in data["tasks"][0]["description"]
    assert client.get("/api/brains/none/dry-run").status_code == 404


def test_memory_browser_and_sync(client, tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    from crewaimeat import local_memory as lm

    r1 = lm.remember("w", {"a": 1}, topic="funding", source="tc")
    lm.remember("w", {"b": 2}, topic="hiring")
    lm.mark_published("w", r1, key="news.funding")  # one published, one still raw

    recs = client.get("/api/memory/w?topic=funding").json()["records"]
    assert len(recs) == 1 and recs[0]["body"] == {"a": 1}
    assert client.get("/api/memory/w/facets").json()["topic"] == {"funding": 1, "hiring": 1}
    assert client.get(f"/api/memory/w/record/{r1}").json()["status"] == "published"

    # sync: raw is local; "published" now reads the NODE's actual keys (deliverables + watch keys),
    # filtering out internal keys (.offers / .live / config / readme / statistics).
    import crewaimeat.aimeat_crew as ac

    monkeypatch.setattr(
        ac,
        "_aimeat_call",
        lambda agent, tool, params: (
            {
                "items": [
                    {
                        "key": "crews.w.x-1234.latest_output",
                        "visibility": "owner",
                        "updated_at": "2026-06-28T05:00:00Z",
                    },
                    {"key": "agents.w.offers", "visibility": "owner"},  # internal -> filtered out
                ]
            }
            if tool == "aimeat_memory_list"
            else None
        ),
    )
    sync = client.get("/api/sync/w").json()
    assert sync["raw_count"] == 1 and sync["in_sync"] is False
    assert sync["published_count"] == 1 and sync["published"][0]["key"] == "crews.w.x-1234.latest_output"


def test_fleet_local_read(client):
    # node=0 default: local-only snapshot, no network/daemon. Just assert the shape comes back.
    data = client.get("/api/fleet").json()
    assert "rows" in data and "n_locks" in data


def test_create_brain_normalizes_uppercase_name(client):
    # 'Mapmaker' would break device-auth (connector rejects uppercase) — the API must slug it.
    r = client.post("/api/brains", json={"agent_name": "Mapmaker", "template_id": "map-snapshot"})
    assert r.status_code == 200 and r.json()["agent_name"] == "mapmaker"
    # too short after slugging -> rejected with a clear 400
    r2 = client.post("/api/brains", json={"agent_name": "@@", "template_id": "map-snapshot"})
    assert r2.status_code == 400


def test_delete_removes_connector_token(client, tmp_path):
    # Deleting an agent must also remove its connector token, else the serve daemon keeps serving a
    # deleted agent (the 'news-paska still served' zombie).
    client.post("/api/brains", json={"agent_name": "zombie-test", "template_id": "topic-watcher"})
    toks = tmp_path / "tokens"
    toks.mkdir(exist_ok=True)
    tokf = toks / "zombie-test@owner1.token"
    tokf.write_text("t", encoding="utf-8")
    assert client.delete("/api/brains/zombie-test").json()["deleted"] is True
    assert not tokf.exists()  # token gone -> no zombie

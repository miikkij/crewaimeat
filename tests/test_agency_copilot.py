"""Data-app builder + copilot (journey/chat) — node-independent surface. Pure units (publish_key_base,
journey.compute, apps store, chat_store, template render, advisor helpers) + the cockpit routes with the
live node/model short-circuited (pytest guards + monkeypatched probes). Isolated to a tmp AIMEAT_HOME."""

from __future__ import annotations

import pytest

TOKEN = "test-token-123"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    monkeypatch.delenv("AIMEAT_OWNER", raising=False)
    # keep the journey/chat snapshot node-free + deterministic
    monkeypatch.setattr("crewaimeat.agency.cockpit._ollama_probe", lambda: (False, []))
    monkeypatch.setattr("crewaimeat.agency.cockpit._has_openrouter_key", lambda: False)
    from starlette.testclient import TestClient

    from crewaimeat.agency.cockpit import create_app

    c = TestClient(create_app(token=TOKEN))
    c.headers.update({"Authorization": f"Bearer {TOKEN}"})
    return c


# ── pure units ──────────────────────────────────────────────────────────────


def test_publish_key_base_matches_write_path(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    from crewaimeat import brain_templates as bt

    for tid, prefix in bt.DEFAULT_PUBLISH_BASE.items():
        b = {"agent_name": "x", "template_id": tid, "policy": {}}
        assert bt.publish_key_base("x", b) == f"{prefix}.x"
    # an explicit publish_key wins, and a trailing `.latest` is stripped to the base
    b = {"agent_name": "x", "template_id": "topic-watcher", "policy": {"publish_key": "my.key.latest"}}
    assert bt.publish_key_base("x", b) == "my.key"
    # the base is exactly what the write path (_resolve_publish_key) uses, minus the key_mode suffix
    key = bt._resolve_publish_key("x", {"key_mode": "latest"}, "watch.x")
    assert key == "watch.x.latest"


def test_journey_ladder_progression():
    from crewaimeat.agency import journey

    fresh = {
        "owner_set": False,
        "engine": {"ready": False},
        "ollama": {"has_model": False},
        "openrouter_key": False,
        "brain_count": 0,
        "node": "https://aimeat.io",
    }
    j = journey.compute(fresh, [], None, produced_data=False, lang="en")
    assert j["current_id"] == "account" and j["next"]["cta"]["kind"] == "goto_step"
    assert j["steps"][0]["is_next"] is True

    # running + data produced, offer template present but not enabled -> publish_offer is next
    b = {"agent_name": "news", "template_id": "topic-watcher", "policy": {"offer_enabled": False}}
    done = {
        "owner_set": True,
        "engine": {"ready": True},
        "ollama": {"has_model": True},
        "openrouter_key": False,
        "brain_count": 1,
        "first_agent": "news",
        "first_agent_connected": True,
        "first_agent_running": True,
        "node": "https://aimeat.io",
    }
    j2 = journey.compute(done, [b], {"url": "https://n/app.html"}, produced_data=True, lang="en")
    assert j2["current_id"] == "publish_offer" and j2["next"]["optional"] is False

    # offer enabled -> the terminal aimeat.io band becomes next (optional/ongoing)
    b["policy"]["offer_enabled"] = True
    j3 = journey.compute(done, [b], {"url": "https://n/app.html"}, produced_data=True, lang="fi")
    assert j3["next"]["optional"] is True and j3["next"]["id"].startswith("aimeat")


def test_apps_store_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    from crewaimeat.agency import apps

    assert apps.get_app("a") is None
    apps.set_app(
        "a",
        filename="a-dashboard.html",
        url="https://n/a",
        variant="dashboard",
        visibility="owner",
        status="live",
        verified=None,
    )
    got = apps.get_app("a")
    assert got["url"] == "https://n/a" and got["verified"] is None  # NULL -> unverified
    apps.set_app(
        "a",
        filename="a-dashboard.html",
        url="https://n/a",
        variant="public_viewer",
        visibility="public",
        status="live",
        verified=True,
    )
    assert apps.get_app("a")["verified"] is True and apps.get_app("a")["variant"] == "public_viewer"
    assert apps.clear_app("a") is True and apps.get_app("a") is None


def test_chat_store_order_and_window(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    from crewaimeat.agency import chat_store

    chat_store.append("s", "user", "hi")
    chat_store.append("s", "assistant", "hello", actions=[{"kind": "build_app", "agent": "x"}])
    h = chat_store.history("s")
    assert [m["role"] for m in h] == ["user", "assistant"]  # chat order (oldest first)
    assert h[1]["actions"][0]["kind"] == "build_app"
    w = chat_store.window("s", turns=1)
    assert len(w) == 1 and set(w[0]) == {"role", "text"}  # window is role+text only


def test_render_template_fills_placeholders(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    from crewaimeat import author_tool
    from crewaimeat.agency import app_builder

    for variant in ("dashboard", "public_viewer"):
        html = app_builder.render_template(
            variant,
            agent="news",
            prefix="watch.news",
            title='He said "hi"',
            lang="fi",
            key_mode="date",
            publisher="news#o@n",
            keys=["watch.news.2026-07-04"],
        )
        # every placeholder replaced (json.dumps injected the literals) + the JS still parses
        assert "__" not in html.split("<script>")[1]
        assert '\\"hi\\"' in html  # title was json-escaped, not raw-substituted
        ok, err = author_tool._check_js(author_tool._extract_inline_js(html))
        assert ok, err


def test_advisor_suggest_and_scripted():
    from crewaimeat.agency import advisor

    templates = [
        {"id": "company-watcher", "title": "Company watcher", "description": "watch a company"},
        {"id": "topic-watcher", "title": "Topic watcher", "description": "watch a topic"},
    ]
    # english + finnish interest keywords both map to a template
    assert advisor.suggest_builds("track my competitors", templates)[0]["template_id"] == "company-watcher"
    assert advisor.suggest_builds("seuraa yritystä", templates)[0]["template_id"] == "company-watcher"
    assert advisor.suggest_builds("nothing relevant here", templates) == []
    # scripted reply names the next step (model-free fallback)
    j = {"next": {"title": "Build an app for its data", "hint": "shows what your agent produces"}}
    assert "Build an app" in advisor.scripted_reply(j, "en")
    assert advisor.scripted_reply({"next": None}, "fi")  # terminal reply is non-empty


# ── cockpit routes (node/model short-circuited) ──────────────────────────────


def test_journey_route(client):
    assert client.get("/api/journey", headers={"Authorization": ""}).status_code == 401
    j = client.get("/api/journey?lang=en").json()
    assert j["steps"][0]["id"] == "account" and j["current_id"] == "account"


def test_chat_scripted_when_no_model(client):
    # pytest + no model -> scripted next-step reply; an interest keyword surfaces a create-agent button
    r = client.post("/api/chat", json={"message": "I want to watch competitors", "lang": "en"}).json()
    assert r["reply"] and r["session_id"]
    tids = [a.get("template_id") for a in r["actions"] if a["kind"] == "create_brain"]
    assert "company-watcher" in tids
    assert any(a["kind"] == "goto_step" for a in r["actions"])  # the journey's next step is always offered
    # history round-trips both turns
    h = client.get(f"/api/chat/history?session_id={r['session_id']}").json()["messages"]
    assert [m["role"] for m in h] == ["user", "assistant"]


def test_chat_rejects_empty(client):
    assert client.post("/api/chat", json={"message": "   "}).status_code == 400


def test_app_build_requires_brain(client):
    assert client.post("/api/agents/ghost/app/build").status_code == 404


def test_app_build_requires_connect(client):
    client.post("/api/account/connect", json={"owner": "happydude500001"})
    client.post("/api/brains", json={"agent_name": "watcher", "template_id": "topic-watcher"})
    # no token yet -> must connect (approve) first
    r = client.post("/api/agents/watcher/app/build")
    assert r.status_code == 400 and "connect" in r.json()["detail"].lower()


def test_app_build_pytest_guarded(client, tmp_path):
    client.post("/api/account/connect", json={"owner": "owner1"})
    client.post("/api/brains", json={"agent_name": "watcher", "template_id": "topic-watcher"})
    toks = tmp_path / "tokens"
    toks.mkdir(exist_ok=True)
    (toks / "watcher@owner1.token").write_text("t", encoding="utf-8")
    # connected -> passes the gate, then the pytest guard stops it before any real publish
    r = client.post("/api/agents/watcher/app/build").json()
    assert r == {"started": False, "reason": "pytest"}


def test_get_app_no_brain_shape(client):
    r = client.get("/api/agents/ghost/app").json()
    assert r["app"] is None and r["build"]["running"] is False
    assert r["data"]["ready"] is False


def test_build_data_app_end_to_end(tmp_path, monkeypatch):
    # Drive the WHOLE deterministic builder (data_status -> render -> publish -> verify -> persist -> event)
    # with only the node REST publish + Playwright faked out — the orchestration logic itself is real.
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    # no login creds -> owner apps take the smoke path and are marked unverified (the appliance's real state)
    monkeypatch.delenv("AIMEAT_APP_LOGIN_USER", raising=False)
    monkeypatch.delenv("AIMEAT_APP_LOGIN_PASSWORD", raising=False)
    from crewaimeat import author_tool, brains
    from crewaimeat.agency import app_builder, apps, events

    brains.save_brain("news", "topic-watcher", prose="watch AI", policy={"visibility": "owner", "key_mode": "date"})

    def fake_call(agent, tool, params):
        if tool == "aimeat_memory_list":
            return {
                "items": [
                    {"key": "watch.news.2026-07-04", "value": "AI news today", "owner_gaii": "news#o@n"},
                    {"key": "watch.news.2026-07-03", "value": "older", "owner_gaii": "news#o@n"},
                ]
            }
        return None

    captured = {}
    monkeypatch.setattr("crewaimeat.aimeat_crew._aimeat_call", fake_call)
    monkeypatch.setattr(app_builder, "_smoke_ok", lambda *a, **k: True)  # served OK (no real HTTP)
    monkeypatch.setattr(
        author_tool,
        "publish_app_html",
        lambda agent, owner, filename, html, **k: (
            captured.update(filename=filename, html=html) or (True, f"https://n/v1/apps/o/{filename}?mode=inline")
        ),
    )
    res = app_builder.build_data_app("news", "owner1", lang="en")
    assert res["status"] == "live" and res["variant"] == "dashboard"
    assert res["verified"] is None  # owner app: smoke-verified, marked unverified (owner logs in to view)
    assert captured["filename"] == "news-dashboard.html"
    assert "watch.news" in captured["html"] and "__" not in captured["html"].split("<script>")[1]
    # persisted + logged
    assert apps.get_app("news")["url"].endswith("news-dashboard.html?mode=inline")
    assert any(e["kind"] == "app_built" for e in events.activity("news"))


def test_build_data_app_no_data(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    from crewaimeat import brains
    from crewaimeat.agency import app_builder, apps

    brains.save_brain("news", "topic-watcher", prose="watch AI")
    monkeypatch.setattr("crewaimeat.aimeat_crew._aimeat_call", lambda *a, **k: {"items": []})
    res = app_builder.build_data_app("news", "owner1")
    assert res["status"] == "no_data"  # never ships an empty shell
    assert apps.get_app("news") is None


def test_delete_brain_clears_app_pointer(client, tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    from crewaimeat.agency import apps

    client.post("/api/brains", json={"agent_name": "watcher", "template_id": "topic-watcher"})
    apps.set_app(
        "watcher",
        filename="watcher-dashboard.html",
        url="https://n/w",
        variant="dashboard",
        visibility="owner",
        status="live",
        verified=None,
    )
    assert apps.get_app("watcher") is not None
    client.delete("/api/brains/watcher")
    assert apps.get_app("watcher") is None  # pointer cleaned so a rebuilt agent doesn't inherit a stale app

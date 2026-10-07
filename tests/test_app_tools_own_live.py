"""Integration: an agent lists and calls its own owner's UNPRICED app tool, against a REAL node and daemon.

The sandbox the wish names: a real node from an aimeat-protocol checkout, an owner with an app whose
manifest offers an unpriced tool bound to an extension (`ext:<ext>:import_records`), a stranger with a
PRICED tool of their own, and the owner's agent reaching the node through the real serve daemon with
crewaimeat's own `app_tools`. Nothing below app_tools is faked.

Skips where the other live tests skip (no node binary, no aimeat-protocol checkout with pnpm install).
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import time
import urllib.request

import pytest
import requests

from tests.test_concierge_propose_live import AIMEAT_DIR, _free_port, _kill, _token

AGENT = "crm"
APP = "crm-sandbox.html"
PRICED_APP = "audit-shop.html"

pytestmark = [
    pytest.mark.skipif(AIMEAT_DIR is None, reason="needs an aimeat-protocol checkout with `pnpm install` done"),
    pytest.mark.loopback,
]

IMPORT_SCRIPT = (
    "export default async function (ctx, input) {"
    " const rows = Array.isArray(input && input.rows) ? input.rows : [];"
    " return { mode: (input && input.mode) || null, would_create: rows.length }; }"
)


def _owner(base: str) -> tuple[str, dict]:
    name = f"o{int(time.time() * 1000) % 10**9}"
    r = requests.post(f"{base}/v1/owners", json={"name": name, "public_key": "placeholder"}, timeout=20)
    assert r.status_code == 201, r.text
    return name, {"Authorization": f"Bearer {_token(base, name, r.json()['data']['private_key'], agent=False)}"}


def _app_with_tool(base: str, owner: str, oh: dict, app: str, tool: str, script: str, price: dict | None) -> str:
    """Publish `app`, install + activate an extension whose action is `tool`, return its ext name. The
    manifest is written by the caller once the operator has aggregated capabilities."""
    r = requests.post(
        f"{base}/v1/apps",
        headers=oh,
        json={
            "filename": app,
            "name": app,
            "description": "app_tools live",
            "content": base64.b64encode(b"<!doctype html><title>x</title><p>x").decode(),
        },
        timeout=30,
    )
    assert r.status_code in (200, 201), r.text
    ext = f"ext{int(time.time() * 1000) % 10**9}{tool[:4]}"
    manifest = "\n".join(
        [
            "metadata:",
            f"  name: {ext}",
            "  version: 1.0.0",
            "  description: app_tools live fixture",
            "  author: t",
            "config:",
            "  app:",
            "    type: string",
            f"    default: {owner}/{app}",
            "actions:",
            f"  - id: {tool}",
            "    method: POST",
            f"    path: /{tool}",
            "    input: { type: object }",
            "    output: { type: object }",
            f"    script: {tool}.js",
        ]
    )
    r = requests.post(
        f"{base}/v1/extensions", headers=oh, json={"manifest": manifest, "scripts": {f"{tool}.js": script}}, timeout=30
    )
    assert r.status_code in (200, 201), r.text
    r = requests.post(f"{base}/v1/extensions/{ext}/activate", headers=oh, json={}, timeout=30)
    assert r.status_code == 200, r.text
    return ext


def _manifest(base: str, oh: dict, app: str, tool: str, ext: str, description: str, price: dict | None) -> None:
    schema = {
        "type": "object",
        "properties": {"rows": {"type": "array"}, "csv": {"type": "string"}, "mode": {"type": "string"}},
    }
    entry = {"name": tool, "description": description, "action_id": f"ext:{ext}:{tool}", "inputSchema": schema}
    if price:
        entry["price"] = price
    r = requests.post(
        f"{base}/v1/memory",
        headers=oh,
        json={"key": f"apps.{app}.tools", "visibility": "public", "value": {"version": 1, "tools": [entry]}},
        timeout=30,
    )
    assert r.status_code in (200, 201), r.text


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("apptools-live")
    home = tmp / "home"
    (home / "tokens").mkdir(parents=True)
    port = _free_port()
    base = f"http://localhost:{port}"
    env = {**os.environ, "AIMEAT_PORT": str(port), "AIMEAT_BASE_URL": base, "AIMEAT_CONNECT_TUNNEL_ENABLED": "true"}
    for k in ("AIMEAT_RL_GLOBAL", "AIMEAT_RL_AUTH", "AIMEAT_RL_WORK", "AIMEAT_RL_MEMORY"):
        env[k] = "100000"
    env.pop("AIMEAT_DEFAULT_AGENT_SCOPES", None)
    node = subprocess.Popen(
        [
            "node",
            "--import",
            "tsx",
            "src/index.ts",
            "start",
            "--db",
            "sqlite",
            "--db-path",
            str(tmp / "n.db"),
            "--port",
            str(port),
        ],
        cwd=AIMEAT_DIR,
        env=env,
        stdout=open(tmp / "node.log", "w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
    )
    serve_pid = None
    old_home = os.environ.get("AIMEAT_HOME")
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"{base}/v1/spec", timeout=2):
                    break
            except OSError:
                time.sleep(0.3)
        else:
            pytest.fail("node did not start")

        # The first account is the operator, who aggregates capabilities; it is also the tool's owner.
        owner, oh = _owner(base)
        stranger, sh = _owner(base)
        ext = _app_with_tool(base, owner, oh, APP, "import_records", IMPORT_SCRIPT, None)
        pext = _app_with_tool(
            base, stranger, sh, PRICED_APP, "audit", "export default async function () { return { ran: true }; }", None
        )
        r = requests.post(f"{base}/v1/admin/capabilities/aggregate", headers=oh, timeout=60)
        assert r.status_code == 200, r.text
        _manifest(
            base, oh, APP, "import_records", ext, "Import a whole file of records in ONE call (dry_run | apply).", None
        )
        _manifest(base, sh, PRICED_APP, "audit", pext, "A priced audit.", {"morsels": 20})

        r = requests.post(
            f"{base}/v1/agents",
            headers=oh,
            json={"name": AGENT, "owner": owner, "capabilities": ["memory"], "scopes": ["*"]},
            timeout=20,
        )
        assert r.status_code == 201, r.text
        gaii = r.json()["data"]["agent"]["gaii"]
        (home / "tokens" / f"{AGENT}@{owner}.token").write_text(
            _token(base, gaii, r.json()["data"]["private_key"], agent=True), encoding="utf-8"
        )
        (home / "agents" / AGENT).mkdir(parents=True)
        (home / "agents" / AGENT / "config.yaml").write_text(
            f"agent: {AGENT}\nowner: {owner}\nnode_url: {base}\nprimary: true\n", encoding="utf-8"
        )
        os.environ["AIMEAT_HOME"] = str(home)
        from aimeat_crewai.mcp_client import ensure_serve

        doc = ensure_serve(
            aimeat_command=["node", "--import", "tsx", "src/index.ts"],
            spawn_cwd=str(AIMEAT_DIR),
            env={**os.environ, "AIMEAT_HOME": str(home)},
            start_timeout=120,
        )
        serve_pid = doc.get("pid")
        yield {"base": base, "home": home, "owner": owner, "stranger": stranger, "ext": ext, "log": tmp / "node.log"}
    finally:
        _kill(serve_pid)
        if node.poll() is None:
            node.kill()
            node.wait(timeout=20)
        if old_home is None:
            os.environ.pop("AIMEAT_HOME", None)
        else:
            os.environ["AIMEAT_HOME"] = old_home


@pytest.fixture
def tools(sandbox, monkeypatch):
    """crewaimeat's own app_tools for the owner's agent, in the sandbox's home."""
    monkeypatch.setenv("AIMEAT_HOME", str(sandbox["home"]))
    from crewaimeat import aimeat_crew
    from crewaimeat.app_tools import make_app_tools

    aimeat_crew._serve_reset()
    return {t.name: t for t in make_app_tools(AGENT)}


def test_list_app_tools_shows_the_owners_unpriced_tool_free_for_you(sandbox, tools):
    rows = json.loads(tools["list_app_tools"].run(query="import"))
    mine = [r for r in rows if r["sku"] == f"app-tool:{sandbox['owner']}/{APP}:import_records"]
    assert mine, rows
    assert mine[0]["free_for_you"] is True and mine[0]["price"] is None
    assert "ONE call" in mine[0]["does"] and mine[0]["input"]["properties"]["rows"]["type"] == "array"


def test_call_app_tool_runs_a_dry_run_and_answers_the_tools_result(sandbox, tools):
    payload = {"rows": [{"name": "Rantanen Ky"}, {"name": "Acme Oy"}], "mode": "dry_run"}
    out = tools["call_app_tool"].run(
        sku=f"app-tool:{sandbox['owner']}/{APP}:import_records", input_json=json.dumps(payload)
    )
    result = json.loads(out)
    assert result.get("mode") == "dry_run" and result.get("would_create") == 2, out


def test_invoke_app_tool_reaches_the_same_tool(sandbox, tools):
    out = tools["invoke_app_tool"].run(sku="import_records", input_json=json.dumps({"rows": [], "mode": "dry_run"}))
    assert json.loads(out).get("mode") == "dry_run", out


def test_a_foreign_priced_tool_behaves_as_before(sandbox, tools):
    rows = json.loads(tools["list_app_tools"].run(query="audit"))
    theirs = [r for r in rows if r["sku"].endswith(f"{PRICED_APP}:audit")]
    assert theirs and theirs[0]["free_for_you"] is False, rows
    out = tools["call_app_tool"].run(sku=theirs[0]["sku"], input_json="{}")
    assert "did NOT run" in out and "paying IS the call" in out, out


def test_another_owners_unpriced_tool_is_never_listed(sandbox, tools):
    skus = [r["sku"] for r in json.loads(tools["list_app_tools"].run(query=""))]
    assert not any(s.startswith(f"app-tool:{sandbox['stranger']}/") and "free" in s for s in skus)
    assert all(not s.startswith(f"app-tool:{sandbox['stranger']}/{APP}") for s in skus)


def _serves_include_own() -> bool:
    try:
        src = (AIMEAT_DIR / "src" / "routes" / "commerce-acp.ts").read_text(encoding="utf-8")
    except (OSError, TypeError):
        return False
    return "include.includes('own')" in src


@pytest.mark.skipif(not _serves_include_own(), reason="the node checkout predates ?include=own (08b619ad8)")
def test_on_a_node_that_serves_include_own_the_listing_is_one_read(sandbox, tools, monkeypatch):
    from crewaimeat import aimeat_crew

    real = aimeat_crew._aimeat_rest
    asked: list[str] = []

    def recording(agent, method, path, *a, **k):
        asked.append(f"{method} {path}")
        return real(agent, method, path, *a, **k)

    monkeypatch.setattr(aimeat_crew, "_aimeat_rest", recording)
    rows = json.loads(tools["list_app_tools"].run(query="import"))
    assert any(r["sku"] == f"app-tool:{sandbox['owner']}/{APP}:import_records" and r["free_for_you"] for r in rows)
    assert asked == ["GET /v1/commerce/tools?include=own"], asked

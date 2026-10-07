"""On a node that serves GET /v1/commerce/tools?include=own (aimeat-protocol 08b619ad8), app_tools reads
the owner's free tools in that ONE call; on an older node it keeps the two-step listing.

The node marks the caller's own owner's unpriced callable tools `own: true` with `price: null`, after the
priced entries. An older node ignores the unknown flag and answers the priced catalog, which is what
tests/test_app_tools_own.py stands in for.
"""

from __future__ import annotations

import json

import pytest

import crewaimeat.app_tools as at

PRICED = {
    "sku": "app-tool:stranger/audit:run",
    "app": "stranger/audit",
    "ownerName": "stranger",
    "name": "run",
    "description": "Run an audit",
    "inputSchema": {"type": "object"},
    "fulfillment": "call",
    "price": {"morsels": 20},
    "webmcp": {"invoke": "https://node.example/v1/apps/stranger/audit/webmcp/tools/run"},
}
OWN = {
    "sku": "app-tool:me/crm.html:import_records",
    "app": "me/crm.html",
    "ownerName": "me",
    "name": "import_records",
    "description": "Import a whole file of records in ONE call (dry_run | apply).",
    "inputSchema": {"type": "object", "properties": {"rows": {"type": "array"}, "mode": {"type": "string"}}},
    "fulfillment": "call",
    "price": None,
    "own": True,
    "webmcp": {"invoke": "https://node.example/v1/apps/me/crm.html/webmcp/tools/import_records"},
}


@pytest.fixture
def new_node(monkeypatch):
    calls: list = []

    def rest(agent, method, path, body=None, *, retries=3, backoff=1.5, raw=False, return_error=False):
        calls.append((method, path))
        if method == "GET" and path == "/v1/commerce/tools?include=own":
            return {"tools": [dict(PRICED), dict(OWN)], "total": 2}
        if method == "POST" and path == "/v1/apps/me/crm.html/webmcp/tools/import_records":
            return {
                "metered": False,
                "result": {"mode": (body or {}).get("mode"), "would_create": len(body.get("rows") or [])},
            }
        return None

    def call(agent, tool, payload, **kw):
        calls.append((tool, None))
        raise AssertionError("on a node that serves ?include=own, no memory listing is made")

    monkeypatch.setattr("crewaimeat.aimeat_crew._aimeat_rest", rest)
    monkeypatch.setattr("crewaimeat.aimeat_crew._aimeat_call", call)
    monkeypatch.setattr(at, "_owner_of", lambda a: "me")
    return calls


def _tools():
    return {t.name: t for t in at.make_app_tools("crm")}


def test_the_catalog_is_asked_with_the_flag_and_answers_in_one_read(new_node):
    rows = json.loads(_tools()["list_app_tools"].run(query=""))
    by_sku = {r["sku"]: r for r in rows}
    assert by_sku["app-tool:me/crm.html:import_records"]["free_for_you"] is True
    assert by_sku["app-tool:me/crm.html:import_records"]["price"] is None
    assert by_sku["app-tool:stranger/audit:run"]["free_for_you"] is False
    assert new_node == [("GET", "/v1/commerce/tools?include=own")], "one read, no per-app listing"


def test_an_own_entry_is_called_on_its_invoke_path_from_both_tools(new_node, monkeypatch):
    payload = {"rows": [{"n": 1}, {"n": 2}], "mode": "dry_run"}
    out = _tools()["call_app_tool"].run(sku="app-tool:me/crm.html:import_records", input_json=json.dumps(payload))
    assert json.loads(out) == {"mode": "dry_run", "would_create": 2}

    def no_mcp(*a, **k):
        raise AssertionError("the MCP door needs a contract nobody holds against their own free tool")

    monkeypatch.setattr(at, "_invoke_via_mcp", no_mcp)
    out = _tools()["invoke_app_tool"].run(sku="import_records", input_json=json.dumps(payload))
    assert json.loads(out)["mode"] == "dry_run"


def test_an_older_node_still_gets_the_two_step_listing(monkeypatch):
    """No entry marked `own`: the flag was ignored (or the owner has no free tools), so the listing of
    the owner's manifests runs as before."""
    asked = []
    monkeypatch.setattr(at, "_priced_catalog", lambda agent: [dict(PRICED)])
    monkeypatch.setattr(at, "_own_unpriced", lambda agent, owner: asked.append(owner) or [dict(OWN, own=None)])
    tools = at._catalog("crm", "me")
    assert asked == ["me"] and {t["sku"] for t in tools} == {PRICED["sku"], OWN["sku"]}

"""An agent sees and calls its OWN OWNER's unpriced app tools (wish-app-tools-an-agent-sees-and-calls-
its-own-owner-s-unpriced-a).

The commerce catalog lists priced tools only, so a free tool of the agent's own owner -- CADENCE's
import_records for its crm agent -- was invisible to list_app_tools and unknown to call_app_tool. These
tests hold the change against a stand-in node at the two seams the code reads through: the owner-scoped
memory listing of `apps.<file>.tools`, and each app's WebMCP listing and invoke path.
"""

from __future__ import annotations

import json

import pytest

import crewaimeat.app_tools as at

PRICED_CATALOG = {
    "tools": [
        {
            "sku": "app-tool:stranger/audit:run",
            "app": "stranger/audit",
            "ownerName": "stranger",
            "name": "run",
            "description": "Run an audit",
            "inputSchema": {"type": "object"},
            "fulfillment": "call",
            "price": {"morsels": 20},
            "webmcp": {"invoke": "https://aimeat.io/v1/apps/stranger/audit/webmcp/tools/run"},
        }
    ]
}

INVOKE = "https://node.example/v1/apps/me/crm.html/webmcp/tools/"

CRM_LISTING = {
    "app": "me/crm.html",
    "tools": [
        {
            "name": "import_records",
            "description": "Import a whole file of records in ONE call: csv or rows, mode dry_run | apply.",
            "inputSchema": {
                "type": "object",
                "required": ["organism", "ws", "type"],
                "properties": {"rows": {"type": "array"}, "csv": {"type": "string"}, "mode": {"type": "string"}},
            },
            "fulfillment": "call",
            "payment": {"required": False},
            "invoke": {"method": "POST", "url": INVOKE + "import_records"},
        },
        {
            "name": "import_template",
            "description": "The CSV template of a record type.",
            "inputSchema": {"type": "object"},
            "fulfillment": "call",
            "payment": {"required": False},
            "invoke": {"method": "POST", "url": INVOKE + "import_template"},
        },
        {
            "name": "pro_report",
            "description": "A priced report (in the catalog already).",
            "fulfillment": "call",
            "payment": {"required": True},
            "invoke": {"method": "POST", "url": INVOKE + "pro_report"},
        },
        {
            "name": "ask_a_person",
            "description": "An unpriced TASK tool: nothing to run.",
            "fulfillment": "task",
            "payment": {"required": False},
            "invoke": {"method": "POST", "url": INVOKE + "ask_a_person"},
        },
    ],
}

PAYMENT_REFUSAL = {
    "ok": False,
    "http_status": 402,
    "error": {"code": "PAYMENT_REQUIRED", "message": "Tool run is priced"},
    "payment": {"required": True, "price": {"morsels": 20, "unit": "per-call"}},
}


@pytest.fixture
def node(monkeypatch):
    calls: list = []

    def rest(agent, method, path, body=None, *, retries=3, backoff=1.5, raw=False, return_error=False):
        calls.append((method, path, body))
        if method == "GET" and path.startswith("/v1/commerce/tools"):
            return PRICED_CATALOG  # an OLDER node: it ignores ?include=own
        if method == "GET" and path == "/v1/apps/me/crm.html/webmcp":
            return CRM_LISTING
        if method == "POST" and path == "/v1/apps/me/crm.html/webmcp/tools/import_records":
            rows = (body or {}).get("rows") or []
            return {"app": "me", "metered": False, "result": {"mode": body.get("mode"), "would_create": len(rows)}}
        if method == "POST" and path.startswith("/v1/apps/stranger/"):
            return PAYMENT_REFUSAL if return_error else None
        return None

    def call(agent, tool, payload, **kw):
        calls.append((tool, payload))
        if tool == "aimeat_memory_list":
            return {
                "items": [
                    {"key": "apps.crm.html.tools", "owner_gaii": "me@node"},
                    {"key": "apps.crm.html.version", "owner_gaii": "me@node"},  # not a manifest
                    {"key": "apps.theirs.html.tools", "owner_gaii": "someone-else@node"},  # never read
                ]
            }
        return None

    monkeypatch.setattr("crewaimeat.aimeat_crew._aimeat_rest", rest)
    monkeypatch.setattr("crewaimeat.aimeat_crew._aimeat_call", call)
    monkeypatch.setattr(at, "_owner_of", lambda a: "me")
    return calls


def _tools():
    return {t.name: t for t in at.make_app_tools("crm")}


def test_the_owners_unpriced_tools_are_listed_free_for_you(node):
    rows = json.loads(_tools()["list_app_tools"].run(query="import"))
    by_sku = {r["sku"]: r for r in rows}
    assert set(by_sku) == {"app-tool:me/crm.html:import_records", "app-tool:me/crm.html:import_template"}
    rec = by_sku["app-tool:me/crm.html:import_records"]
    assert rec["free_for_you"] is True and rec["price"] is None
    assert rec["input"]["required"] == ["organism", "ws", "type"] and "ONE call" in rec["does"]


def test_priced_task_and_other_owners_tools_are_not_added(node):
    skus = {r["sku"] for r in json.loads(_tools()["list_app_tools"].run(query=""))}
    assert "app-tool:me/crm.html:pro_report" not in skus, "a priced tool comes from the catalog, not here"
    assert "app-tool:me/crm.html:ask_a_person" not in skus, "an unpriced task tool has nothing to run"
    assert not any("theirs.html" in s for s in skus), "another owner's manifest is never read"
    assert ("GET", "/v1/apps/me/theirs.html/webmcp", None) not in node
    assert "app-tool:stranger/audit:run" in skus, "the priced catalog is still there"


def test_call_app_tool_runs_the_owners_free_tool_on_its_invoke_path(node):
    payload = {"organism": "o", "ws": "w", "type": "contact", "rows": [{"n": 1}, {"n": 2}], "mode": "dry_run"}
    out = _tools()["call_app_tool"].run(sku="app-tool:me/crm.html:import_records", input_json=json.dumps(payload))
    assert json.loads(out) == {"mode": "dry_run", "would_create": 2}
    posts = [c for c in node if c[0] == "POST"]
    assert posts == [("POST", "/v1/apps/me/crm.html/webmcp/tools/import_records", payload)]


def test_invoke_app_tool_takes_the_same_path_for_the_owners_free_tool(node, monkeypatch):
    def no_mcp(*a, **k):
        raise AssertionError("the MCP door needs a contract nobody holds against their own free tool")

    monkeypatch.setattr(at, "_invoke_via_mcp", no_mcp)
    out = _tools()["invoke_app_tool"].run(
        sku="import_records", input_json=json.dumps({"organism": "o", "ws": "w", "type": "t", "mode": "dry_run"})
    )
    assert json.loads(out)["mode"] == "dry_run"


def test_a_foreign_priced_tool_still_says_payment_is_the_call(node):
    out = _tools()["call_app_tool"].run(sku="app-tool:stranger/audit:run", input_json="{}")
    assert "did NOT run" in out and "PAYMENT_REQUIRED" in out and "paying IS the call" in out


def test_the_descriptions_say_a_list_goes_in_one_call():
    tools = {t.name: t for t in at.make_app_tools("crm")}
    for name in ("list_app_tools", "call_app_tool", "invoke_app_tool"):
        assert "one call" in tools[name].description.lower(), name


def test_with_no_owner_nothing_is_added(monkeypatch):
    called = []
    monkeypatch.setattr(at, "_own_manifest_files", lambda a, o: called.append(1) or ["x"])
    assert at._own_unpriced("crm", "") == [] and called == []


def test_the_crew_definition_still_accepts_app_tools():
    from crewaimeat.crew_def import validate_crew_doc

    doc = {
        "agent_name": "crm",
        "agents": [{"name": "w", "role": "CRM", "goal": "Import contacts", "backstory": "b", "tools": ["app_tools"]}],
        "tasks": [{"id": "t", "agent": "w", "description": "Do:\n{{ctx.prompt}}", "expected_output": "A line."}],
    }
    assert validate_crew_doc(doc) == []

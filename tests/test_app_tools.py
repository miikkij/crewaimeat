"""app_tools (item C) — a crewai agent finds and calls an app-tool.

The transport is mocked so these stay deterministic and offline; the real node round-trip was proven
by hand against the live catalog (same-owner free tools return their result, metered:false). What is
pinned here is the meaning: the family line (free vs priced), the honest report when a stranger's
priced tool cannot run, and that the caller reads a tool's input schema before invoking it.
"""

from __future__ import annotations

import json

import pytest

import crewaimeat.app_tools as at

CATALOG = {
    "tools": [
        {
            "sku": "app-tool:me/laake.html:disruptions",
            "app": "me/laake.html",
            "ownerName": "me",
            "name": "disruptions",
            "description": "Current medicine shortages.",
            "inputSchema": {"type": "object", "properties": {"limit": {"type": "number"}}},
            "fulfillment": "call",
            "price": None,
            "webmcp": {"invoke": "https://aimeat.io/v1/apps/me/laake.html/webmcp/tools/disruptions"},
        },
        {
            "sku": "app-tool:me/sanomat.html:getEdition",
            "app": "me/sanomat.html",
            "ownerName": "session-x#me",  # a GAII whose owner is still `me` — same family
            "name": "getEdition",
            "description": "Fetch an edition.",
            "inputSchema": {"type": "object"},
            "fulfillment": "call",
            "price": {"morsels": 2, "unit": "per-call"},
            "webmcp": {"invoke": "https://aimeat.io/v1/apps/me/sanomat.html/webmcp/tools/getEdition"},
        },
        {
            "sku": "app-tool:stranger/audit:run",
            "app": "stranger/audit",
            "ownerName": "stranger",
            "name": "run",
            "description": "A paid audit owned by someone else.",
            "inputSchema": {"type": "object", "required": ["text"]},
            "fulfillment": "task",
            "price": {"morsels": 20, "unit": "per-call"},
            "webmcp": {"invoke": "https://aimeat.io/v1/apps/stranger/audit/webmcp/tools/run"},
        },
    ]
}


@pytest.fixture
def node(monkeypatch):
    """Fake `_aimeat_rest`: GET raw returns the catalog; POST returns a result for family tools and
    None (a 402/failure) for the stranger's priced one — exactly what the live node does."""
    calls: list = []

    def _rest(agent, method, path, body=None, *, retries=3, backoff=1.5, raw=False):
        calls.append((method, path, body))
        if method == "GET" and raw:
            return CATALOG
        if method == "POST" and "/apps/me/" in path:
            return {"app": "me", "metered": False, "result": {"ok": True, "n": 3}}
        if method == "POST" and "/apps/stranger/" in path:
            return None  # the node answered 402; _aimeat_rest turns that into None
        return None

    monkeypatch.setattr("crewaimeat.aimeat_crew._aimeat_rest", _rest)
    monkeypatch.setattr(at, "_owner_of", lambda a: "me")
    return calls


def _tools(node):
    return {t.name: t for t in at.make_app_tools("news-writer")}


def test_it_is_in_the_crew_def_menu():
    from crewaimeat.crew_def import TOOL_PURPOSES, TOOL_REGISTRY

    assert "app_tools" in TOOL_REGISTRY and "app_tools" in TOOL_PURPOSES


def test_list_shows_the_family_line(node):
    rows = json.loads(_tools(node)["list_app_tools"].run(query=""))
    by_sku = {r["sku"]: r for r in rows}

    # Own family — free, and the price is hidden because it does not apply to you.
    assert by_sku["app-tool:me/laake.html:disruptions"]["free_for_you"] is True
    assert by_sku["app-tool:me/laake.html:disruptions"]["price"] is None
    # A GAII under the same owner is still family.
    assert by_sku["app-tool:me/sanomat.html:getEdition"]["free_for_you"] is True
    # A stranger's tool — not free, and the price is shown.
    stranger = by_sku["app-tool:stranger/audit:run"]
    assert stranger["free_for_you"] is False and stranger["price"] == {"morsels": 20, "unit": "per-call"}


def test_list_carries_the_input_schema(node):
    """The model has no other way to learn the call shape, so the listing must hand it over."""
    rows = json.loads(_tools(node)["list_app_tools"].run(query="disruptions"))
    assert rows and rows[0]["input"]["properties"]["limit"]["type"] == "number"


def test_call_runs_a_family_tool_and_returns_its_result(node):
    out = _tools(node)["call_app_tool"].run(sku="app-tool:me/laake.html:disruptions", input_json='{"limit": 1}')
    assert json.loads(out) == {"ok": True, "n": 3}
    # It went to the tool's OWN invoke path, and carried the parsed input.
    post = [c for c in node if c[0] == "POST"][-1]
    assert post[1] == "/v1/apps/me/laake.html/webmcp/tools/disruptions" and post[2] == {"limit": 1}


def test_a_strangers_priced_tool_is_reported_not_faked(node):
    """The honest failure: it did not run, and the reason is payment — not a made-up result."""
    out = _tools(node)["call_app_tool"].run(sku="app-tool:stranger/audit:run", input_json='{"text": "x"}')
    assert "requires payment" in out and "did not run" in out.lower()


def test_bad_input_json_is_caught_before_any_call(node):
    out = _tools(node)["call_app_tool"].run(sku="app-tool:me/laake.html:disruptions", input_json="{not json")
    assert "not valid JSON" in out
    assert not any(c[0] == "POST" for c in node), "nothing may be invoked on malformed input"


def test_an_ambiguous_reference_refuses_rather_than_guess(node):
    # "run" is a bare tool name that could match; here it is unique, but a partial that matches two
    # must return the 'be specific' message rather than pick one.
    out = _tools(node)["call_app_tool"].run(sku="me/", input_json="{}")
    assert "No single app-tool matches" in out


def test_free_for_helper_reads_the_owner_out_of_a_gaii():
    assert at._tool_owner({"ownerName": "session-x#me"}) == "me"
    assert at._tool_owner({"ownerName": "me"}) == "me"
    assert at._free_for({"ownerName": "stranger"}, "me") is False

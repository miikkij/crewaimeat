"""Attached MCP servers as a crew tool: what the model is told, for each way a call can end.

The node's answers below are the ones a local node gave through a real serve daemon on 2026-10-12
(aimeat 3.26.1, /local/call): the success shapes, the tool's own no (`is_error` on a success), and the
refusal envelopes, which arrive with the node's code and sentence and no HTTP status of their own.
"""

from __future__ import annotations

import json

import pytest

from crewaimeat import mcp_tools

SERVERS = {
    "servers": [
        {
            "id": "0af41350",
            "slug": "orders",
            "title": "Warehouse orders",
            "description": "Order status from the warehouse system.",
            "ownership": "owner",
            "status": "active",
            "enabled": True,
            "toolCount": 2,
        }
    ]
}
TOOLS = {
    "server": "orders",
    "tools": [
        {
            "name": "lookup_order",
            "description": "The status of one order of the warehouse, by its number.",
            "inputSchema": {
                "type": "object",
                "properties": {"order_id": {"type": "string", "description": "The order number."}},
                "required": ["order_id"],
                "$schema": "http://json-schema.org/draft-07/schema#",
            },
            "annotations": {"readOnlyHint": True},
        }
    ],
    "cached": True,
}
RAN = {"content": [{"type": "text", "text": "Order 4417: ships 2026-10-19."}], "is_error": False}
SAID_NO = {"content": [{"type": "text", "text": "There is no order 9. Known orders: 4417, 4418."}], "is_error": True}


def _refused(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}, "http_status": 400}


NO_SCOPE = _refused("SCOPE_DENIED", 'Scope "mcp:use" required. Agent scopes: [memory:read, memory:write]')


class Node:
    """Stands where `_aimeat_call` is; answers by tool name and records what it was asked."""

    def __init__(self, **answers):
        self.answers = answers
        self.calls: list[tuple] = []

    def __call__(self, agent, tool, payload, **kw):
        self.calls.append((agent, tool, payload, kw))
        return self.answers.get(tool)


@pytest.fixture
def node(monkeypatch):
    def make(**answers) -> Node:
        n = Node(**answers)
        monkeypatch.setattr(mcp_tools, "_aimeat_call", n)
        return n

    return make


def _tools(agent="a"):
    return {t.name: t for t in mcp_tools.make_mcp_tools(agent)}


# ── the three steps, as the model sees them ─────────────────────────────────────────────────


def test_the_servers_are_listed_by_the_name_a_call_takes(node):
    node(aimeat_mcp_list=SERVERS)
    out = _tools()["list_mcp_servers"].run()
    assert '"orders"' in out and "Warehouse orders" in out and "Order status" in out
    assert "REPAIR" not in out and "SWITCHED OFF" not in out


def test_no_attached_server_is_said_plainly(node):
    node(aimeat_mcp_list={"servers": []})
    assert "No MCP server is attached" in _tools()["list_mcp_servers"].run()


@pytest.mark.parametrize(
    ("row", "said"),
    [
        ({"status": "needs_reauth"}, "NEEDS REPAIR"),
        ({"status": "unreachable"}, "NEEDS REPAIR"),
        ({"status": "disabled"}, "SWITCHED OFF"),
        ({"enabled": False}, "SWITCHED OFF"),
    ],
)
def test_a_server_that_cannot_be_called_says_so_in_the_list(node, row, said):
    node(aimeat_mcp_list={"servers": [{**SERVERS["servers"][0], **row}]})
    assert said in _tools()["list_mcp_servers"].run()


def test_a_servers_tools_come_with_the_arguments_they_take(node):
    n = node(aimeat_mcp_tools=TOOLS)
    out = _tools()["list_mcp_tools"].run(server="orders")
    assert "lookup_order" in out and '"order_id"' in out and '"required"' in out
    assert "$schema" not in out, "the schema dialect is no argument"
    assert "readOnlyHint" in out, "the server's own marks are shown"
    assert n.calls[0][2] == {"server": "orders"}, "the cache is the normal answer; refresh is asked for"
    _tools()["list_mcp_tools"].run(server="orders", refresh=True)
    assert n.calls[1][2] == {"server": "orders", "refresh": True}


def test_a_call_hands_back_the_tools_own_answer(node):
    n = node(aimeat_mcp_call=RAN)
    out = _tools()["call_mcp_tool"].run(server="orders", tool="lookup_order", arguments_json='{"order_id": "4417"}')
    assert out == "Order 4417: ships 2026-10-19."
    agent, tool, payload, kw = n.calls[0]
    assert (agent, tool) == ("a", "aimeat_mcp_call")
    assert payload == {"server": "orders", "tool": "lookup_order", "arguments": {"order_id": "4417"}}
    assert kw["return_error"] is True


def test_a_call_is_sent_once_because_a_far_tool_may_write(node):
    n = node(aimeat_mcp_call=RAN)
    mcp_tools.call_tool("a", "orders", "cancel_order", {"order_id": "4417"})
    assert n.calls[0][3]["retries"] == 1


def test_structured_and_non_text_content_is_kept(node):
    node(
        aimeat_mcp_call={
            "content": [{"type": "text", "text": "two parts"}, {"type": "resource", "uri": "x://1"}],
            "structured_content": {"count": 2},
            "is_error": False,
        }
    )
    out = _tools()["call_mcp_tool"].run(server="orders", tool="t")
    assert "two parts" in out and "x://1" in out and '"count": 2' in out


# ── the endings a model must tell apart ─────────────────────────────────────────────────────


def test_the_tool_saying_no_is_not_the_server_being_broken(node):
    node(aimeat_mcp_call=SAID_NO)
    ending, out = mcp_tools.call_tool("a", "orders", "lookup_order", {"order_id": "9"})
    assert ending == "tool_said_no"
    assert out.startswith(mcp_tools.TOOL_SAID_NO)
    assert "There is no order 9" in out, "its own answer is the explanation"
    assert "the server itself is working" in out
    assert mcp_tools.NEEDS_REPAIR not in out and mcp_tools.NOT_ALLOWED not in out


@pytest.mark.parametrize("code", ["UNREACHABLE", "UPSTREAM_UNAUTHORIZED", "CREDENTIAL_UNREADABLE", "STDIO_DISABLED"])
def test_a_server_out_of_reach_needs_a_person(node, code):
    node(aimeat_mcp_call=_refused(code, '"orders" could not be reached.'))
    ending, out = mcp_tools.call_tool("a", "orders", "lookup_order", {"order_id": "4417"})
    assert ending == "refused"
    assert out.startswith(mcp_tools.NEEDS_REPAIR)
    assert code in out and '"orders" could not be reached.' in out, "the node's own words are in it"
    assert "A person has to repair" in out
    assert mcp_tools.TOOL_SAID_NO not in out


@pytest.mark.parametrize("code", ["SCOPE_DENIED", "NOT_GRANTED", "SERVER_DISABLED"])
def test_a_refused_caller_is_told_it_is_the_owners_decision(node, code):
    node(aimeat_mcp_call=_refused(code, "the node's sentence"))
    ending, out = mcp_tools.call_tool("a", "orders", "lookup_order", {})
    assert ending == "refused" and out.startswith(mcp_tools.NOT_ALLOWED)
    assert code in out and "the node's sentence" in out
    assert "do not report the work as done" in out


def test_the_scope_refusal_reaches_the_model_in_the_nodes_words_on_every_step(node):
    node(aimeat_mcp_list=NO_SCOPE, aimeat_mcp_tools=NO_SCOPE, aimeat_mcp_call=NO_SCOPE)
    t = _tools()
    for out in (
        t["list_mcp_servers"].run(),
        t["list_mcp_tools"].run(server="orders"),
        t["call_mcp_tool"].run(server="orders", tool="lookup_order", arguments_json="{}"),
    ):
        assert out.startswith(mcp_tools.NOT_ALLOWED)
        assert 'Scope "mcp:use" required' in out


def test_an_unknown_server_is_not_a_repair(node):
    node(aimeat_mcp_call=_refused("NOT_FOUND", "No such MCP server."))
    out = mcp_tools.call_tool("a", "ghost", "x", {})[1]
    assert out.startswith(mcp_tools.NO_SUCH_SERVER) and "never guess a name" in out


def test_a_refusal_this_file_has_not_met_is_passed_on_and_not_classified(node):
    node(aimeat_mcp_call=_refused("RESPONSE_TOO_LARGE", '"orders" answered with more than 8 MB at once.'))
    out = mcp_tools.call_tool("a", "orders", "dump", {})[1]
    assert out.startswith(mcp_tools.NOT_DONE)
    assert "RESPONSE_TOO_LARGE" in out and "more than 8 MB" in out
    assert "repair" not in out.lower() and "owner's decision" not in out


def test_no_answer_is_not_called_a_failure_or_a_success(node):
    node()  # every tool answers None: the node could not be asked
    ending, out = mcp_tools.call_tool("a", "orders", "cancel_order", {"order_id": "4417"})
    assert ending == "refused" and out.startswith(mcp_tools.NO_ANSWER)
    assert "not known whether anything ran" in out


@pytest.mark.parametrize("raw", ["{not json", '["a list"]', '"a string"'])
def test_arguments_that_are_not_one_json_object_never_reach_the_node(node, raw):
    n = node(aimeat_mcp_call=RAN)
    out = _tools()["call_mcp_tool"].run(server="orders", tool="lookup_order", arguments_json=raw)
    assert out.startswith(mcp_tools.NOT_DONE) and "arguments_json" in out
    assert n.calls == []


def test_the_call_tools_description_names_the_endings_it_can_answer_with():
    desc = _tools()["call_mcp_tool"].description
    for first_words in (mcp_tools.TOOL_SAID_NO, mcp_tools.NOT_ALLOWED, mcp_tools.NEEDS_REPAIR):
        assert first_words in desc


# ── one server, named in the definition ─────────────────────────────────────────────────────


def test_a_named_server_gives_two_tools_bound_to_it(node):
    n = node(aimeat_mcp_tools=TOOLS, aimeat_mcp_call=RAN)
    t = {x.name: x for x in mcp_tools.make_mcp_server_tools("a", "ware-house")}
    assert set(t) == {"list_ware_house_tools", "call_ware_house_tool"}, "no tool lists or reaches another server"
    assert '"ware-house"' in t["call_ware_house_tool"].description
    t["list_ware_house_tools"].run()
    t["call_ware_house_tool"].run(tool="lookup_order", arguments_json='{"order_id": "4417"}')
    assert [c[2]["server"] for c in n.calls] == ["ware-house", "ware-house"]


def test_two_named_servers_do_not_collide():
    names = [t.name for s in ("orders", "wiki") for t in mcp_tools.make_mcp_server_tools("a", s)]
    assert len(names) == len(set(names)) == 4


# ── the tool id in a crew definition ────────────────────────────────────────────────────────


def _doc(tools: list[str]) -> dict:
    return {
        "agent_name": "warehouse-helper",
        "agents": [{"role": "Helper", "goal": "g", "backstory": "b", "tools": tools}],
        "tasks": [{"id": "run", "agent": "Helper", "description": "Do {{ctx.prompt}}", "expected_output": "x"}],
    }


def test_a_definition_can_name_every_server_or_one():
    from crewaimeat.crew_def import TOOL_PURPOSES, TOOL_REGISTRY, validate_crew_doc

    assert "mcp" in TOOL_REGISTRY and "mcp:<server>" in TOOL_PURPOSES["mcp"]
    assert validate_crew_doc(_doc(["mcp"])) == []
    assert validate_crew_doc(_doc(["mcp:orders", "decline"])) == []


@pytest.mark.parametrize("bad", ["mcp:", "mcp:Not A Slug", "mcp:orders/../x", "mcp:-orders"])
def test_a_selector_that_cannot_name_a_server_is_refused_at_validation(bad):
    from crewaimeat.crew_def import validate_crew_doc

    errors = validate_crew_doc(_doc([bad]))
    assert errors and "unknown tool" in errors[0] and "mcp:orders" in errors[0]


def test_the_interpreter_builds_what_the_definition_names(node):
    from types import SimpleNamespace

    from crewaimeat.crew_def import resolve_tool

    n = node(aimeat_mcp_list=SERVERS, aimeat_mcp_tools=TOOLS)
    ctx = SimpleNamespace(task={"id": "t1"})
    every = resolve_tool("mcp")("warehouse-helper", ctx)
    assert [t.name for t in every] == ["list_mcp_servers", "list_mcp_tools", "call_mcp_tool"]
    one = resolve_tool("mcp:orders")("warehouse-helper", ctx)
    assert [t.name for t in one] == ["list_orders_tools", "call_orders_tool"]
    assert n.calls == [], "building asks the node nothing: its answer to the first call is the check"
    one[0].run()
    assert n.calls[0][:3] == ("warehouse-helper", "aimeat_mcp_tools", {"server": "orders"})


def test_the_crew_menu_offers_it(monkeypatch):
    import crewaimeat.crew_invoke as ci

    # The menu also states the owner's default road, which it reads from the node; not what is tested.
    monkeypatch.setattr("crewaimeat.llm_choice.default_is_node_road", lambda agent_name: False)
    ok, menu = ci.handle("crew.menu", {}, agent_name="concierge")
    assert ok
    row = next(t for t in menu["tools"] if t["id"] == "mcp")
    assert "mcp:use" in row["purpose"]


# ── the proposer asks for what the tool needs ───────────────────────────────────────────────


def test_the_permissions_are_named_once_and_the_proposer_reads_them():
    from crewaimeat import concierge_propose

    assert mcp_tools.USE_SCOPES == ("mcp:use",)
    assert concierge_propose.PROPOSABLE_TOOLS["mcp"] is mcp_tools.USE_SCOPES
    assert concierge_propose.tool_scopes("mcp:orders") == mcp_tools.USE_SCOPES
    assert concierge_propose.tool_scopes("decide:sort-a-message") is None, "a rule is allowed per agent on the node"
    asked, left_out = concierge_propose.compute_scopes(["mcp:orders"], ["*"])
    assert "mcp:use" in asked and left_out == []
    asked, left_out = concierge_propose.compute_scopes(["mcp"], ["memory:read", "memory:write"])
    assert "mcp:use" not in asked and left_out == ["mcp:use"], "capped at what the proposer itself holds"


def test_a_proposed_definition_with_a_named_server_validates_and_tells_the_agent_how():
    from crewaimeat import concierge_propose
    from crewaimeat.crew_def import validate_crew_doc

    d = concierge_propose.build_crew_def(
        name="order-desk",
        display_name="Order desk",
        purpose="Answers where an order is, from the warehouse system.",
        instructions="Look the order up and say where it is.",
        tools=["mcp:orders"],
        workspace=None,
        delivers="",
    )
    assert validate_crew_doc(d) == []
    assert d["agents"][0]["tools"] == ["mcp:orders", "decline"]
    text = d["tasks"][0]["description"]
    assert "MCP servers" in text and "never guess a server or a tool name" in text
    assert json.dumps(d)  # what the node stores is plain JSON

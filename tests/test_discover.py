"""The liaison's tool list: the package's default plus LIAISON_EXTRA_TOOLS, for every crew.

`aimeat_discover` was the first extra, added only when CrewSpec.discover was on. Since 2026-10-12 it,
`aimeat_invoke` and the three attached-MCP-server tools are in every liaison's list.
"""

from __future__ import annotations

from aimeat_crewai.daemon import DAEMON_DEFAULT_TOOL_FILTER

from crewaimeat.aimeat_crew import LIAISON_EXTRA_TOOLS, CrewSpec, _extras_not_exposed, _liaison_tool_filter


def test_the_packages_default_carries_the_extras_since_0_35():
    # aimeat-crewai 0.35.0 added all five to its own list; the composed tuple names each once (below).
    assert set(LIAISON_EXTRA_TOOLS) <= set(DAEMON_DEFAULT_TOOL_FILTER)


def test_every_crew_gets_the_default_list_and_the_extras_once_each():
    f = _liaison_tool_filter()
    assert f[: len(DAEMON_DEFAULT_TOOL_FILTER)] == DAEMON_DEFAULT_TOOL_FILTER, "the package's list, untouched"
    assert set(LIAISON_EXTRA_TOOLS) <= set(f)
    assert len(f) == len(set(f)) == len({*DAEMON_DEFAULT_TOOL_FILTER, *LIAISON_EXTRA_TOOLS})
    assert LIAISON_EXTRA_TOOLS == (
        "aimeat_discover",
        "aimeat_invoke",
        "aimeat_mcp_list",
        "aimeat_mcp_tools",
        "aimeat_mcp_call",
    )


def test_an_extra_can_be_left_out_and_a_default_tool_cannot():
    f = _liaison_tool_filter(without=["aimeat_mcp_call", "aimeat_task_complete"])
    assert "aimeat_mcp_call" not in f and "aimeat_mcp_list" in f
    assert "aimeat_task_complete" in f, "a crew cannot work without its task tools"


def _liaison_refusal(missing: list[str]) -> Exception:
    # The sentence aimeat_crewai.liaison.create_liaison_agent raises with (0.33.0).
    available = sorted({*DAEMON_DEFAULT_TOOL_FILTER, *LIAISON_EXTRA_TOOLS} - set(missing))
    return RuntimeError(
        f"tool_filter requested tools that the MCP server did not expose: {sorted(missing)}. Available: {available}"
    )


def test_an_extra_the_connector_does_not_offer_is_named_from_the_liaisons_refusal():
    f = _liaison_tool_filter()
    assert _extras_not_exposed(_liaison_refusal(["aimeat_mcp_call", "aimeat_mcp_tools"]), f) == [
        "aimeat_mcp_tools",
        "aimeat_mcp_call",
    ]
    # `aimeat_mcp_list` is offered, so it is in "Available" and must not be read as missing.
    assert "aimeat_mcp_list" not in _extras_not_exposed(_liaison_refusal(["aimeat_mcp_call"]), f)


def test_the_refusal_is_read_from_the_packages_own_raise(monkeypatch):
    """Pinned against the REAL raise site, not the sentence copied above: a connector that offers
    everything but two extras makes aimeat_crewai's liaison raise, and what it raises names them."""
    from types import SimpleNamespace

    import aimeat_crewai.liaison as liaison

    offered = [n for n in _liaison_tool_filter() if n not in ("aimeat_invoke", "aimeat_mcp_call")]

    class Connector:
        def __init__(self, params):
            self.tools = [SimpleNamespace(name=n) for n in offered]

        def stop(self):
            pass

    monkeypatch.setattr(liaison, "MCPServerAdapter", Connector)
    f = _liaison_tool_filter()
    try:
        with liaison.create_liaison_agent(
            mcp_server_params={"url": "http://127.0.0.1:1/v1/mcp"}, agent_name="x", skill_path=None, tool_filter=f
        ):
            raise AssertionError("the liaison started although the filter named tools the connector lacks")
    except liaison.AimeatLiaisonError as exc:
        missing = _extras_not_exposed(exc, f)
    assert missing == ["aimeat_invoke", "aimeat_mcp_call"]
    assert set(_liaison_tool_filter(without=missing)) == set(offered), "and the next start asks for what is offered"


def test_the_packages_own_optional_tools_are_dropped_when_not_offered():
    """2026-10-31: aimeat-crewai 0.38.0 put aimeat_goal_get/_work in its default list, connector 4.1.2
    did not offer them, and every crew in the fleet stopped at start."""
    from aimeat_crewai.daemon import DAEMON_TOOLS_IF_OFFERED

    goal = [t for t in DAEMON_TOOLS_IF_OFFERED if t.startswith("aimeat_goal_")]
    assert goal, "the package names its goal tools as optional"
    f = _liaison_tool_filter()
    assert set(goal) <= set(f)
    missing = _extras_not_exposed(_liaison_refusal(goal), f)
    assert sorted(missing) == sorted(goal)
    assert not set(goal) & set(_liaison_tool_filter(without=missing))
    assert "aimeat_task_complete" in _liaison_tool_filter(without=missing)


def test_a_missing_default_tool_or_another_error_drops_nothing():
    f = _liaison_tool_filter()
    assert _extras_not_exposed(_liaison_refusal(["aimeat_task_complete"]), f) == []
    assert _extras_not_exposed(RuntimeError("No tools discovered through the MCP connection."), f) == []
    # An extra already left out is not reported again, so the start loop cannot spin on it.
    assert _extras_not_exposed(_liaison_refusal(["aimeat_mcp_call"]), _liaison_tool_filter(["aimeat_mcp_call"])) == []


def test_crewspec_discover_defaults_off():
    assert CrewSpec(agent_name="x", build_domain=lambda c: ([], [])).discover is False


# ── deterministic shell helpers (crewaimeat.discover, node >=1.32.1) ──
def test_discover_map_builds_payload_and_returns(monkeypatch):
    from crewaimeat import discover

    calls: list = []
    monkeypatch.setattr(
        discover,
        "_aimeat_call",
        lambda a, t, p: calls.append((t, p)) or {"total": 5, "types": [{"value": "app", "count": 5}]},
    )
    out = discover.discover_map("c", scope="public", type="capability,knowledge")
    assert out["total"] == 5
    tool, payload = calls[0]
    assert tool == "aimeat_discover"
    assert payload == {"mode": "map", "scope": "public", "type": "capability,knowledge"}


def test_discover_find_returns_entries(monkeypatch):
    from crewaimeat import discover

    monkeypatch.setattr(
        discover, "_aimeat_call", lambda a, t, p: {"entries": [{"id": "x", "title": "X"}], "total": 1, "facets": {}}
    )
    out = discover.discover_find("c", q="agent", type="memory", limit=3)
    assert out == [{"id": "x", "title": "X"}]


def test_discover_find_tolerates_empty_or_bad(monkeypatch):
    from crewaimeat import discover

    monkeypatch.setattr(discover, "_aimeat_call", lambda a, t, p: None)
    assert discover.discover_find("c", q="z") == []
    monkeypatch.setattr(discover, "_aimeat_call", lambda a, t, p: {"no_entries": 1})
    assert discover.discover_find("c") == []


def test_discover_find_payload_omits_empty(monkeypatch):
    from crewaimeat import discover

    seen: dict = {}
    monkeypatch.setattr(discover, "_aimeat_call", lambda a, t, p: seen.update(p) or {"entries": []})
    discover.discover_find("c", q="", scope="shared", limit=10)
    assert seen == {"mode": "find", "scope": "shared", "limit": 10}  # no empty q/type/tags/segment keys

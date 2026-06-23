"""discover flag — the liaison tool_filter gains aimeat_discover only when CrewSpec.discover is on."""

from __future__ import annotations

from aimeat_crewai.daemon import DAEMON_DEFAULT_TOOL_FILTER

from crewaimeat.aimeat_crew import CrewSpec, _liaison_tool_filter


def test_default_filter_excludes_discover():
    # The package default omits it on purpose (small models cope with ~24 tools).
    assert "aimeat_discover" not in DAEMON_DEFAULT_TOOL_FILTER


def test_filter_off_is_the_default_set():
    assert _liaison_tool_filter(False) == DAEMON_DEFAULT_TOOL_FILTER
    assert "aimeat_discover" not in _liaison_tool_filter(False)


def test_filter_on_adds_discover_once():
    f = _liaison_tool_filter(True)
    assert "aimeat_discover" in f
    assert list(f).count("aimeat_discover") == 1
    # everything from the default set is preserved
    assert set(DAEMON_DEFAULT_TOOL_FILTER) <= set(f)
    assert len(f) == len(DAEMON_DEFAULT_TOOL_FILTER) + 1


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

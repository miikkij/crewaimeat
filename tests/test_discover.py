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

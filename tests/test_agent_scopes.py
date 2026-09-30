"""The permissions an agent asks for at approval.

Three traps these tests hold shut, each one found by reading the node rather than the wish:

  --scopes REPLACES the node's default for a NEW agent (device-auth.ts v1.9.0), so a request that named
    only what the scaffold needs beyond the defaults would approve an agent with no memory access;
  on a RE-approval it replaces what the agent holds, so asking again would cut an agent the owner gave
    `*` down to this list -- forge asks only when it has no credential for the agent yet;
  forge_catalog declared `schedule` and `generator`, which are not scopes the node knows at all.
"""

from __future__ import annotations

import re

from crewaimeat import agent_scopes, forge_catalog

# The shape of every scope word the node's routes check (`requireScope('memory:write')`, ...).
_SCOPE_WORD = re.compile(r"^[a-z]+:[a-z-]+$")


def test_the_request_starts_with_the_node_defaults():
    asked = agent_scopes.requested_scopes()
    assert asked[:4] == ["memory:read", "memory:write", "memory:delete", "catalogue:read"], (
        "without these a NEW agent is approved with no memory access, where every deliverable goes"
    )
    assert set(agent_scopes.REQUIRED_SCOPES) <= set(asked)


def test_extra_scopes_are_appended_once():
    asked = agent_scopes.requested_scopes(["app:write", "task:write", " app:write ", ""])
    assert asked.count("task:write") == 1
    assert asked.count("app:write") == 1 and asked[-1] == "app:write"
    assert "" not in asked


def test_the_argv_is_one_flag_and_one_comma_list():
    args = agent_scopes.scopes_args(["app:write"])
    assert args[0] == "--scopes"
    assert args[1].split(",") == agent_scopes.requested_scopes(["app:write"])


def test_every_scope_asked_for_is_a_word_the_node_knows_the_shape_of():
    for s in agent_scopes.requested_scopes():
        assert _SCOPE_WORD.match(s), s


def test_every_catalog_capability_declares_real_scope_words():
    # `schedule` and `generator` were here: labels, not scopes. The node grants neither, and a node whose
    # operator restricted the maximum would have refused the whole registration over them.
    for cap in forge_catalog.CATALOG:
        for s in cap.scopes:
            assert _SCOPE_WORD.match(s), f"{cap.id} declares {s!r}, which is not a node scope"


def test_the_scopes_a_forged_crew_asks_for_come_from_the_tools_it_wired():
    # forge_catalog's own reader of a forge-emitted `_tools(ctx)` block, the authoritative record of what
    # the crew wired; registration turns those capabilities into the scopes they need.
    source = forge_catalog.emit_tools_function("schedule")[0]
    wired = forge_catalog.capabilities_in_source(source)
    assert wired == ["schedule"], wired
    assert forge_catalog.required_scopes(wired) == ["workflow:read", "task:write"]
    assert forge_catalog.capabilities_in_source("AGENT_NAME = 'x'\n") == []


def test_the_agency_reexports_the_one_list():
    from crewaimeat.agency2 import connect

    assert connect.REQUIRED_SCOPES is agent_scopes.REQUIRED_SCOPES


def test_an_old_bundled_connector_is_not_handed_an_option_it_would_refuse(monkeypatch):
    # An older CLI refuses an undeclared option, and then the whole registration fails. So the flow runs
    # as before and the state row tells the person to tick the scopes themselves.
    from crewaimeat.agency2 import connect, engine

    monkeypatch.setattr(engine, "bundled", lambda: True)
    monkeypatch.setattr(engine, "connector_version", lambda: "3.20.0")
    args, note = connect._scopes_argv()
    assert args == []
    assert note and "3.20.0" in note and "agent:write" in note


def test_a_current_bundled_connector_asks_for_the_scopes(monkeypatch):
    from crewaimeat.agency2 import connect, engine

    monkeypatch.setattr(engine, "bundled", lambda: True)
    monkeypatch.setattr(engine, "connector_version", lambda: "3.21.0")
    args, note = connect._scopes_argv()
    assert args == agent_scopes.scopes_args() and note is None


def test_the_machines_own_connector_asks_for_the_scopes(monkeypatch):
    from crewaimeat.agency2 import connect, engine

    monkeypatch.setattr(engine, "bundled", lambda: False)
    args, _note = connect._scopes_argv()
    assert args[0] == "--scopes"

"""The hard rule: a test must NEVER spawn or kill real processes. Every test that exercises a launch
flow mocks it — these backstops make a FORGOTTEN mock fail loud (or no-op) instead of silently starting
a detached watchdog / killing the live dev fleet on the machine running the suite.

Also covers node_engine: the detection layer the appliance's engine wizard step reads.
"""

from __future__ import annotations

import pytest


def test_serve_guard_refuses_to_run_under_pytest():
    import crewaimeat.serve_guard as sg

    with pytest.raises(RuntimeError, match="pytest"):
        sg.ensure_single_serve()
    with pytest.raises(RuntimeError, match="pytest"):
        sg.restart_serve()


def test_launch_crew_skips_under_pytest():
    from crewaimeat import forge

    pid, log = forge.launch_crew("crews/whatever_crew.py")
    assert pid is None and "pytest" in log


def test_stop_fleet_skips_under_pytest():
    from crewaimeat.tui import actions

    assert "pytest" in actions.stop_fleet()


def test_register_agent_skips_only_when_spawn_would_be_real(tmp_path, monkeypatch):
    """The guard must not break the parsing tests (they mock subprocess.Popen module-wide): it triggers
    ONLY when Popen is the real one."""
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    from crewaimeat import forge

    assert forge.subprocess.Popen is forge._REAL_POPEN  # unmocked here -> the guard must catch it
    ok, msg = forge.register_agent("watch-1", "owner1", "https://aimeat.io")
    assert ok is False and "pytest" in msg


def test_node_engine_status_shape():
    from crewaimeat import node_engine

    st = node_engine.engine_status()
    assert set(st) == {"node", "npx", "connector_cli", "ready"}
    assert all(isinstance(v, bool) for v in st.values())
    # ready is derived, never independently true
    assert st["ready"] == (st["npx"] and st["connector_cli"])


def test_node_engine_serve_command_forms(monkeypatch):
    from crewaimeat import node_engine

    # CLI on PATH -> bare name (ensure_serve's own .cmd shim handling applies)
    monkeypatch.setattr(node_engine, "aimeat_cli", lambda: "aimeat")
    assert node_engine.serve_command() == "aimeat"
    # CLI found only in the npm -g dir (fresh install, no re-login) -> a spawnable argv, not a bare .cmd
    monkeypatch.setattr(node_engine, "aimeat_cli", lambda: r"C:\Users\x\AppData\Roaming\npm\aimeat.cmd")
    monkeypatch.setattr(node_engine.os, "name", "nt", raising=False)
    cmd = node_engine.serve_command()
    assert cmd[:2] == ["cmd", "/c"] and cmd[2].endswith("aimeat.cmd")
    # nothing found -> fall back to the bare name (the spawn then fails with the connector's own error)
    monkeypatch.setattr(node_engine, "aimeat_cli", lambda: None)
    assert node_engine.serve_command() == "aimeat"

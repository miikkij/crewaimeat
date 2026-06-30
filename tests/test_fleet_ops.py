"""fleet_ops.ensure_attached — auto-reload the connector for a newly-approved agent. Isolated; the
serve-daemon restart path is monkeypatched (no real processes touched)."""

from __future__ import annotations


def test_noop_when_already_attached(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    from crewaimeat.agency import fleet_ops

    monkeypatch.setattr(fleet_ops.fleet_state, "collect_serve", lambda: {"agents": [{"agent": "w"}]})
    assert fleet_ops.ensure_attached("w") == {"attached": True, "restarted": False}


def test_skips_restart_for_unapproved_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    monkeypatch.setenv("AIMEAT_OWNER", "owner1")
    from crewaimeat.agency import fleet_ops

    monkeypatch.setattr(fleet_ops.fleet_state, "collect_serve", lambda: {"agents": []})
    # no token file exists under tmp -> _token_exists False -> no serve restart
    assert fleet_ops.ensure_attached("w") == {"attached": False, "restarted": False}


def test_restarts_serve_when_approved_but_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    monkeypatch.setenv("AIMEAT_OWNER", "owner1")
    from crewaimeat.agency import fleet_ops

    monkeypatch.setattr(fleet_ops.fleet_state, "collect_serve", lambda: {"agents": []})
    monkeypatch.setattr(fleet_ops.time, "sleep", lambda *_a: None)
    # approved: a token file exists
    toks = tmp_path / "tokens"
    toks.mkdir()
    (toks / "w@owner1.token").write_text("t", encoding="utf-8")
    import crewaimeat.serve_guard as sg

    monkeypatch.setattr(sg, "this_home_serve_pids", lambda: [111])
    monkeypatch.setattr(fleet_ops, "_kill", lambda pid: None)
    monkeypatch.setattr(sg, "ensure_single_serve", lambda: {"agents": [{"agent": "w"}]})  # fresh daemon loaded it
    assert fleet_ops.ensure_attached("w") == {"attached": True, "restarted": True}


def test_watchdog_never_spawns_under_pytest():
    """The detached serve_watchdog supervisor must NEVER spawn during tests — it outlives the test
    process and leaks real serve daemons onto the machine. pytest sets PYTEST_CURRENT_TEST, which the
    guard checks."""
    from crewaimeat.agency import fleet_ops

    fleet_ops._WATCHDOG_STARTED = False  # reset the per-process latch
    assert fleet_ops.ensure_serve_watchdog() is False

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

    fleet_ops._LAST_ATTACH_RESTART.clear()  # no cooldown carried from a prior test
    monkeypatch.setattr(fleet_ops.fleet_state, "collect_serve", lambda: {"agents": []})
    # approved: a token file exists
    toks = tmp_path / "tokens"
    toks.mkdir()
    (toks / "w@owner1.token").write_text("t", encoding="utf-8")
    import crewaimeat.serve_guard as sg

    # Fix B: ONE coordinated restart (kill+respawn under the spawn lock), not the old kill+ensure dance.
    monkeypatch.setattr(sg, "restart_serve", lambda: {"agents": [{"agent": "w"}]})  # fresh daemon loaded it
    assert fleet_ops.ensure_attached("w") == {"attached": True, "restarted": True}


def test_cooldown_skips_repeat_restart(tmp_path, monkeypatch):
    """Rapid repeated start/restart must not hammer the daemon — a fresh daemon already loaded every token."""
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    monkeypatch.setenv("AIMEAT_OWNER", "owner1")
    from crewaimeat.agency import fleet_ops

    monkeypatch.setattr(fleet_ops.fleet_state, "collect_serve", lambda: {"agents": []})
    toks = tmp_path / "tokens"
    toks.mkdir()
    (toks / "w@owner1.token").write_text("t", encoding="utf-8")
    fleet_ops._LAST_ATTACH_RESTART["_any"] = fleet_ops.time.monotonic()  # just restarted -> within cooldown
    import crewaimeat.serve_guard as sg

    monkeypatch.setattr(
        sg, "restart_serve", lambda: (_ for _ in ()).throw(AssertionError("must not restart within cooldown"))
    )
    assert fleet_ops.ensure_attached("w") == {"attached": False, "restarted": False}


def test_ensure_bridge_fast_path_no_restart(tmp_path, monkeypatch):
    """Steady state (agent already attached): ensure_bridge must NOT reap or restart — no tunnel drop."""
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    from crewaimeat.agency import fleet_ops

    monkeypatch.setattr(fleet_ops.fleet_state, "collect_serve", lambda: {"agents": [{"agent": "w"}]})
    monkeypatch.setattr(
        fleet_ops, "ensure_serve_alive", lambda: (_ for _ in ()).throw(AssertionError("fast path must not reap"))
    )
    assert fleet_ops.ensure_bridge("w") == {"attached": True, "restarted": False}


def test_watchdog_never_spawns_under_pytest():
    """The detached serve_watchdog supervisor must NEVER spawn during tests — it outlives the test
    process and leaks real serve daemons onto the machine. pytest sets PYTEST_CURRENT_TEST, which the
    guard checks."""
    from crewaimeat.agency import fleet_ops

    fleet_ops._WATCHDOG_STARTED = False  # reset the per-process latch
    assert fleet_ops.ensure_serve_watchdog() is False

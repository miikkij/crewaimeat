"""fleet TUI actions — the wrappers delegate to the right control functions and format results.
forge/serve_guard are monkeypatched (no processes spawned, no daemons touched)."""

from crewaimeat.tui import actions


# ── single crew ───────────────────────────────────────────────────────────────
def test_start_crew_delegates_to_forge_start(monkeypatch):
    monkeypatch.setattr("crewaimeat.forge.start_crew", lambda a: f"started {a}")
    assert actions.start_crew("news-fetcher") == "started news-fetcher"


def test_stop_crew_delegates_to_forge_stop(monkeypatch):
    monkeypatch.setattr("crewaimeat.forge.stop_crew", lambda a: f"stopped {a}")
    assert actions.stop_crew("news-fetcher") == "stopped news-fetcher"


def test_restart_crew_delegates_to_forge_recycle(monkeypatch):
    monkeypatch.setattr("crewaimeat.forge.recycle_crew", lambda a: f"recycled {a}")
    assert actions.restart_crew("news-fetcher") == "recycled news-fetcher"


def test_reauth_delegates_to_forge(monkeypatch):
    monkeypatch.setattr("crewaimeat.forge.reauth", lambda a: f"reauth {a}")
    assert actions.reauth_crew("postman") == "reauth postman"


# ── whole fleet ─────────────────────────────────────────────────────────────────
def test_start_fleet_ensures_daemon_then_reconciles(monkeypatch):
    monkeypatch.setattr("crewaimeat.serve_guard.ensure_single_serve",
                        lambda: {"pid": 99648, "port": 52813, "agents": [{}]})
    monkeypatch.setattr("crewaimeat.forge.reconcile_fleet", lambda: "launched 3 crews")
    s = actions.start_fleet()
    assert "pid 99648 port 52813" in s and "launched 3 crews" in s


def test_restart_fleet_stops_then_starts(monkeypatch):
    monkeypatch.setattr(actions, "stop_fleet", lambda: "STOPPED")
    monkeypatch.setattr(actions, "start_fleet", lambda: "STARTED")
    assert actions.restart_fleet() == "STOPPED  |  STARTED"


def test_reap_formats_reaped(monkeypatch):
    monkeypatch.setattr("crewaimeat.serve_guard.ensure_single_serve",
                        lambda: {"pid": 1, "port": 2, "agents": [{}, {}], "_reaped_duplicates": 1})
    s = actions.reap_serve_daemons()
    assert "pid 1 port 2" in s and "2 agents" in s and "reaped 1 duplicate" in s

"""The approve→attach bridge (aimeat_crew._serve_attach_bridge) + serve_guard home alignment.

The shared serve daemon loads its agent set at STARTUP, so an agent approved after it started (every
crew-forge-born agent) was unknown to it and crash-looped on UNKNOWN_AGENT until its watchdog gave up
(founder-lens, 2026-07-02). The bridge does ONE coordinated reload exactly then — and nothing at all
in the steady state. All daemon interactions are mocked; the pytest guard is deleted per-test the same
way the ollama-stop tests do it.
"""

from __future__ import annotations

import crewaimeat.aimeat_crew as ac


def _doc(*agents, pid=111):
    return {"pid": pid, "port": 50000, "agents": [{"agent": a} for a in agents]}


def test_bridge_noops_when_already_attached(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(ac, "ensure_serve", lambda **kw: _doc("watcher", "other"))
    monkeypatch.setattr(
        "crewaimeat.serve_guard.restart_serve",
        lambda: (_ for _ in ()).throw(AssertionError("steady state must NOT restart the daemon")),
    )
    ac._serve_attach_bridge("watcher")  # no raise = no restart


def test_bridge_noops_when_no_live_daemon(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)

    def _no_daemon(**kw):
        raise ac.AimeatServeError("No live serve daemon")

    monkeypatch.setattr(ac, "ensure_serve", _no_daemon)
    monkeypatch.setattr(
        "crewaimeat.serve_guard.restart_serve",
        lambda: (_ for _ in ()).throw(AssertionError("nothing to reload when no daemon lives")),
    )
    ac._serve_attach_bridge("watcher")  # the next spawn loads all tokens — bridge must not act


def test_bridge_reloads_once_when_unattached(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(ac, "ensure_serve", lambda **kw: _doc("other-agent"))
    calls = []
    monkeypatch.setattr(
        "crewaimeat.serve_guard.restart_serve", lambda: calls.append(1) or _doc("other-agent", "watcher", pid=222)
    )
    ac._serve_attach_bridge("watcher")
    assert calls == [1]  # exactly ONE coordinated reload


def test_bridge_guarded_under_pytest(monkeypatch):
    # PYTEST_CURRENT_TEST present (the normal test env) -> the bridge must not even discover
    monkeypatch.setattr(
        ac, "ensure_serve", lambda **kw: (_ for _ in ()).throw(AssertionError("guard must return first"))
    )
    ac._serve_attach_bridge("watcher")


def test_serve_guard_home_matches_single_source(tmp_path, monkeypatch):
    """serve_guard must resolve the SAME home as crewaimeat._home (env wins, else <cwd>/.aimeat) — the
    old ~/.aimeat fallback made env-less restart_serve a silent no-op against a repo's daemon."""
    import crewaimeat.serve_guard as sg
    from crewaimeat._home import aimeat_home

    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path / "pinned"))
    assert sg._aimeat_home() == aimeat_home() == tmp_path / "pinned"
    monkeypatch.delenv("AIMEAT_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    assert sg._aimeat_home() == aimeat_home()
    assert sg._aimeat_home().name == ".aimeat" and sg._aimeat_home().parent == tmp_path

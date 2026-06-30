"""aimeat_crew._wait_for_serve — a crew WAITS for the supervisor's shared serve daemon instead of
hard-crashing on AimeatServeError (the appliance startup crash-loop). It must never spawn (the
single-spawner discipline: only the supervisor / start_fleet spawns) and must time out cleanly."""

from __future__ import annotations

import pytest
from aimeat_crewai.mcp_client import AimeatServeError

import crewaimeat.aimeat_crew as ac


def test_wait_for_serve_returns_once_live(monkeypatch):
    calls: list[bool] = []

    def fake_ensure(*, auto_start):
        calls.append(auto_start)
        if len(calls) < 3:
            raise AimeatServeError("daemon not up yet")
        return {"pid": 42, "port": 1234, "agents": []}

    monkeypatch.setattr(ac, "ensure_serve", fake_ensure)
    monkeypatch.setattr(ac.time, "sleep", lambda *_a: None)
    doc = ac._wait_for_serve("w", max_wait_seconds=60, interval=0)
    assert doc["pid"] == 42
    assert calls == [False, False, False]  # NEVER auto-starts — crews only wait for the supervisor


def test_wait_for_serve_raises_after_timeout(monkeypatch):
    def fake_ensure(*, auto_start):
        raise AimeatServeError("never comes up")

    monkeypatch.setattr(ac, "ensure_serve", fake_ensure)
    monkeypatch.setattr(ac.time, "sleep", lambda *_a: None)
    with pytest.raises(AimeatServeError):
        ac._wait_for_serve("w", max_wait_seconds=0, interval=0)

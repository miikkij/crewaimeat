"""events — the per-agent activity log. Isolated to a tmp AIMEAT_HOME."""

from __future__ import annotations


def test_record_and_activity_newest_first(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    from crewaimeat.agency import events

    assert events.activity("a") == []
    events.record("a", "brain_saved", {"version": 1, "changed": ["created"]})
    events.record("a", "started")
    evs = events.activity("a")
    assert [e["kind"] for e in evs] == ["started", "brain_saved"]  # newest first
    assert evs[1]["detail"] == {"version": 1, "changed": ["created"]}
    # scoping: another agent doesn't see it
    assert events.activity("b") == []


def test_has_kind(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    from crewaimeat.agency import events

    assert events.has_kind("a", "connected") is False
    events.record("a", "connected")
    assert events.has_kind("a", "connected") is True

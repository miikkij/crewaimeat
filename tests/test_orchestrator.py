"""orchestrator.py — the delegating-router substrate. Pure logic, mocked node + session store."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from crewaimeat import orchestrator


def _iso(minutes_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat().replace("+00:00", "Z")


ROSTER = [
    {"name": "jingle-writer", "gaii": "jingle-writer#o@n", "last_seen": _iso(0.2)},
    {"name": "web-researcher", "gaii": "web-researcher#o@n", "last_seen": _iso(2)},
    {"name": "finnish-corporate-researcher", "gaii": "finnish-corporate-researcher#o@n", "last_seen": _iso(120)},
    {"name": "concierge", "gaii": "concierge#o@n", "last_seen": _iso(0.1)},
]
DIRECTORY = {
    "finnish-corporate-researcher": "Finnish company research",
    "web-researcher": "general web research",
    "jingle-writer": "jingles",
    "ghost-agent": "not on the node",
}


def test_services_from_roster_drops_stale_and_missing():
    out = orchestrator.services_from_roster(ROSTER, DIRECTORY, max_age_s=900)
    names = [s["name"] for s in out]
    # jingle-writer + web-researcher are fresh; finnish (120 min) is stale; ghost-agent isn't on the node.
    assert names == ["web-researcher", "jingle-writer"]
    assert all("gaii" in s and "desc" in s for s in out)


def test_services_preserve_directory_order():
    fresh_roster = [{"name": n, "gaii": f"{n}#o@n", "last_seen": _iso(0)} for n in DIRECTORY if n != "ghost-agent"]
    out = orchestrator.services_from_roster(fresh_roster, DIRECTORY)
    assert [s["name"] for s in out] == ["finnish-corporate-researcher", "web-researcher", "jingle-writer"]


def test_in_roster_matches_by_name_from_gaii():
    assert orchestrator.in_roster(ROSTER, "jingle-writer#o@n") is True
    assert orchestrator.in_roster(ROSTER, "jingle-writer") is True
    assert orchestrator.in_roster(ROSTER, "some-human@n") is False
    assert orchestrator.in_roster(ROSTER, None) is False


def test_directory_text_handles_empty():
    assert "no specialists" in orchestrator.directory_text([]).lower()
    txt = orchestrator.directory_text([{"name": "jingle-writer", "desc": "jingles"}])
    assert "jingle-writer" in txt and "jingles" in txt


def test_conv_id_tolerates_shapes():
    assert orchestrator._conv_id({"conversation_id": "c1"}) == "c1"
    assert orchestrator._conv_id({"conversationId": "c2"}) == "c2"
    assert orchestrator._conv_id({"data": {"conversation_id": "c3"}}) == "c3"
    assert orchestrator._conv_id({"nope": 1}) is None
    assert orchestrator._conv_id(None) is None


def test_delegate_sends_and_returns_conv(monkeypatch):
    sent: list = []

    def fake_send(agent, to, body, *, subject=None):
        sent.append((agent, to, body, subject))
        return {"data": {"conversation_id": "cv-9"}}

    monkeypatch.setattr(orchestrator, "dm_send", fake_send)
    conv = orchestrator.delegate("concierge", "jingle-writer#o@n", "write a jingle for coffee")
    assert conv == "cv-9"
    assert sent and sent[0][1] == "jingle-writer#o@n"


def test_delegation_roundtrip_match_and_clear(monkeypatch):
    """record_delegation then match_delegation returns the pending once, and clears it (no double-relay)."""
    store: dict = {}

    def fake_set(agent, conv, key, val):
        store[(agent, conv, key)] = val

    def fake_get(agent, conv, key):
        return store.get((agent, conv, key))

    def fake_clear(agent, conv, key):
        store.pop((agent, conv, key), None)

    monkeypatch.setattr(orchestrator, "session_set", fake_set)
    monkeypatch.setattr(orchestrator, "session_get", fake_get)
    monkeypatch.setattr(orchestrator, "session_clear", fake_clear)

    orchestrator.record_delegation(
        "concierge", "cv-9", user_to="alice@n", user_conv="u-1", specialist="jingle-writer", request="jingle"
    )
    # Wrong sender on the right conv -> no match.
    assert orchestrator.match_delegation("concierge", "cv-9", "someone-else#o@n") is None
    # Right specialist -> match, returns payload, and clears.
    p = orchestrator.match_delegation("concierge", "cv-9", "jingle-writer#o@n")
    assert p and p["user_to"] == "alice@n" and p["user_conv"] == "u-1"
    # Second call -> already cleared, no double-relay.
    assert orchestrator.match_delegation("concierge", "cv-9", "jingle-writer#o@n") is None

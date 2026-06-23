"""session_store — local SQLite per-(agent, conversation) state. Isolated to a tmp AIMEAT_HOME."""

from __future__ import annotations


def test_session_store_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    from crewaimeat import session_store as ss

    assert ss.session_get("a", "c", "k", "DEF") == "DEF"  # miss -> default
    ss.session_set("a", "c", "k", {"x": [1, 2]})
    assert ss.session_get("a", "c", "k") == {"x": [1, 2]}  # JSON round-trips

    ss.session_set("a", "c", "k2", "v2")
    ss.session_clear("a", "c", "k")  # one key
    assert ss.session_get("a", "c", "k") is None
    assert ss.session_get("a", "c", "k2") == "v2"

    # scoping: a different conversation / agent doesn't see it
    assert ss.session_get("a", "other", "k2", "DEF") == "DEF"
    assert ss.session_get("other", "c", "k2", "DEF") == "DEF"

    ss.session_clear("a", "c")  # whole conversation
    assert ss.session_get("a", "c", "k2") is None

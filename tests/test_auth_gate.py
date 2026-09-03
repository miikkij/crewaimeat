"""The approval gate must judge an Agent v2 key by the right question.

Deterministic — no node, no daemon, no LLM. What is pinned is the CHOICE of probe: a v1 bearer is
probed directly, a v2 key is probed through the daemon that can sign for it, and a key with no daemon
is 'unknown' rather than 'rejected'.
"""

from __future__ import annotations

import pytest

import crewaimeat.aimeat_crew as ac


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    (tmp_path / "keys").mkdir()
    (tmp_path / "tokens").mkdir()
    return tmp_path


def _key(home, name="web-researcher", owner="happydude500001"):
    (home / "keys" / f"{name}@{owner}.key").write_text("{}", encoding="utf-8")


def _token(home, name="activity-reporter", owner="happydude500001"):
    (home / "tokens" / f"{name}@{owner}.token").write_text("x", encoding="utf-8")


class _Resp:
    def __init__(self, status):
        self.status_code = status  # noqa: E701


def test_credential_kind_prefers_the_key_the_migration_left(home):
    """The migration writes the key and does NOT delete the token it replaced."""
    _key(home)
    (home / "tokens" / "web-researcher@happydude500001.token").write_text("expired", encoding="utf-8")
    assert ac._credential_kind("web-researcher#happydude500001@a-node", None) == "key"

    _token(home)
    assert ac._credential_kind("activity-reporter", "happydude500001") == "token"
    assert ac._credential_kind("nobody", None) is None


def test_a_v2_key_is_probed_through_the_daemon_not_as_a_bearer(home, monkeypatch):
    """A key is not a bearer, so a direct Authorization header answers a question nobody asked.

    Measured 2026-09-03, minutes after web-researcher migrated: the node had accepted it —
    identity_version 2, last_seen current, attached to the shared socket — while the direct probe
    answered 401 and the crew sat at "the token is not accepted" forever.
    """
    _key(home)
    monkeypatch.setattr(ac, "_aimeat_read_token", lambda *a, **k: (_ for _ in ()).throw(AssertionError("bearer probe")))

    class _S:
        def get(self, url, headers=None, timeout=None):
            assert headers["X-Aimeat-Agent"] == "web-researcher#happydude500001@a-node"
            return _Resp(200)

    monkeypatch.setattr(ac, "_serve_api", lambda: ("http://127.0.0.1:1", _S()))
    assert ac._auth_alive("web-researcher#happydude500001@a-node", None) is True


def test_a_key_with_no_daemon_is_unknown_not_rejected(home, monkeypatch):
    """Unknown lets the crew proceed; False would park a fully authorised agent forever."""
    _key(home)
    monkeypatch.setattr(ac, "_serve_api", lambda: None)
    assert ac._auth_alive("web-researcher#happydude500001@a-node", None) is None


def test_a_refused_key_still_reads_as_refused(home, monkeypatch):
    """The gate must keep working: a real 403 through the daemon is still a rejection."""
    _key(home)

    class _S:
        def get(self, url, headers=None, timeout=None):
            return _Resp(403)

    monkeypatch.setattr(ac, "_serve_api", lambda: ("http://127.0.0.1:1", _S()))
    assert ac._auth_alive("web-researcher#happydude500001@a-node", None) is False

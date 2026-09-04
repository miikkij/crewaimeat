"""The boundary check that refuses a write the node would record under another agent.

Deterministic — no daemon, no node. What is pinned is the ASYMMETRY (a write refuses, a read
continues), and that only PROOF blocks: no daemon, a transport error or an unreadable answer all
mean 'unknown', and unknown proceeds.
"""

from __future__ import annotations

import pytest

import crewaimeat.aimeat_crew as ac


@pytest.fixture(autouse=True)
def _clean():
    ac._IDENTITY_VERDICT.clear()
    ac._IDENTITY_SAID.clear()
    yield
    ac._IDENTITY_VERDICT.clear()
    ac._IDENTITY_SAID.clear()


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload


def _api(monkeypatch, resp=None, boom=False, calls=None):
    class _S:
        def get(self, url, headers=None, timeout=None):
            if calls is not None:
                calls.append(headers.get("X-Aimeat-Agent"))
            if boom:
                raise OSError("connection refused")
            return resp

    monkeypatch.setattr(ac, "_serve_api", lambda: ("http://127.0.0.1:1", _S()))


def test_the_node_answering_as_us_lets_everything_through(monkeypatch):
    _api(monkeypatch, _Resp(200, {"data": {"gaii": "news-fetcher#alice@node-a"}}))
    assert ac._check_identity("news-fetcher") is True
    assert ac._identity_guard("news-fetcher", "POST") is True
    assert ac._identity_guard("news-fetcher", "GET") is True


def test_a_write_under_the_wrong_name_is_refused_and_a_read_is_not(monkeypatch, capsys):
    """Measured 2026-09-04: a fleet of 62 had every REST call attributed to the socket's opener.

    A DELETE on somebody else's memory key cannot be argued with afterwards, so a proven mismatch
    refuses. Refusing reads too would take a fleet down over a diagnosis.
    """
    _api(monkeypatch, _Resp(200, {"data": {"gaii": "activity-reporter#alice@node-a"}}))
    assert ac._check_identity("news-fetcher") is False

    for method in ("POST", "PUT", "PATCH", "DELETE", "delete"):
        assert ac._identity_guard("news-fetcher", method) is False, method
    for method in ("GET", "HEAD"):
        assert ac._identity_guard("news-fetcher", method) is True, method

    err = capsys.readouterr().err
    assert "IDENTITY MISMATCH" in err
    assert err.count("IDENTITY MISMATCH") == 1, "said once per agent, not once per call"


def test_only_proof_blocks(monkeypatch):
    """No daemon, a transport error, a non-200 and an unreadable body are all 'unknown' — and
    unknown proceeds. A guard that blocked on weather would be worse than the bug it watches for."""
    monkeypatch.setattr(ac, "_serve_api", lambda: None)
    assert ac._check_identity("a") is None
    assert ac._identity_guard("a", "DELETE") is True

    ac._IDENTITY_VERDICT.clear()
    _api(monkeypatch, boom=True)
    assert ac._check_identity("b") is None
    assert ac._identity_guard("b", "DELETE") is True

    ac._IDENTITY_VERDICT.clear()
    _api(monkeypatch, _Resp(503))
    assert ac._check_identity("c") is None

    ac._IDENTITY_VERDICT.clear()
    _api(monkeypatch, _Resp(200, {"data": {}}))
    assert ac._check_identity("d") is None


def test_the_check_runs_once_per_agent(monkeypatch):
    """A worker is one agent for its whole life; the boundary costs one call, not one per call."""
    calls: list[str] = []
    _api(monkeypatch, _Resp(200, {"data": {"gaii": "x#alice@node-a"}}), calls=calls)
    for _ in range(5):
        ac._identity_guard("x", "GET")
        ac._identity_guard("x", "POST")
    assert calls == ["x"]


def test_a_gaii_caller_matches_its_own_local_name(monkeypatch):
    """The daemon answers with a GAII; callers name agents both ways."""
    _api(monkeypatch, _Resp(200, {"data": {"gaii": "news-fetcher#alice@node-a"}}))
    assert ac._check_identity("news-fetcher#alice@node-a") is True
    ac._IDENTITY_VERDICT.clear()
    assert ac._check_identity("news-fetcher") is True

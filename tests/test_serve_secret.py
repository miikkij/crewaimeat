"""Every call to the serve daemon carries the secret it wrote into serve.json.

`aimeat connect serve --http` from aimeat-protocol 65db1a88a on (serve.json schema 3) answers 401
LOOPBACK_SECRET_REQUIRED to a request without it. These tests pin each client this repo builds against
the daemon; the live proof against a real daemon is in the wish's claim, not here (no test touches a
daemon).
"""

import json

import pytest
import requests
from aimeat_crewai import serve_auth_headers as package_headers

from crewaimeat import aimeat_crew as ac
from crewaimeat import spawn_state
from crewaimeat.transport import _daemon_refused_secret


@pytest.mark.parametrize(
    "doc",
    [
        {"port": 1, "secret": "s3"},
        {"port": 1},  # schema 2: writes none, checks none
        {"port": 1, "secret": ""},
        {"port": 1, "secret": 7},
        {},
        None,
    ],
)
def test_the_crewai_free_copy_answers_exactly_like_the_package(doc):
    """spawn_state carries its own copy so the idle spawner never imports crewai. This is what keeps it
    from drifting from the package's answer."""
    assert spawn_state.serve_auth_headers(doc) == package_headers(doc)


def test_the_shared_session_carries_the_secret(monkeypatch):
    monkeypatch.setattr(ac, "ensure_serve", lambda **kw: {"port": 40999, "secret": "s3"})
    ac._serve_reset()
    try:
        base, session = ac._serve_api()
        assert base == "http://127.0.0.1:40999"
        assert session.headers["Authorization"] == "Bearer s3"
    finally:
        ac._serve_reset()


def test_a_daemon_without_a_secret_gets_no_authorization_header(monkeypatch):
    monkeypatch.setattr(ac, "ensure_serve", lambda **kw: {"port": 40999})
    ac._serve_reset()
    try:
        _base, session = ac._serve_api()
        assert "Authorization" not in session.headers
    finally:
        ac._serve_reset()


def test_a_refused_secret_re_discovers_the_daemon():
    """A restarted daemon has a new secret; the cached session's old one is retried after a reset."""
    envelope = {"code": "LOOPBACK_SECRET_REQUIRED", "message": "Send the secret from serve.json ..."}
    assert ac._is_transient_error(envelope)


def _response(status: int, challenge: str | None) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    if challenge:
        r.headers["WWW-Authenticate"] = challenge
    return r


def test_only_the_daemons_own_401_counts_as_a_stale_secret():
    """A node 401 through the proxy is the node's verdict on the agent, and stays a failure."""
    assert _daemon_refused_secret(_response(401, 'Bearer realm="aimeat connect serve"'))
    assert not _daemon_refused_secret(_response(401, None))
    assert not _daemon_refused_secret(_response(401, 'Bearer realm="aimeat"'))
    assert not _daemon_refused_secret(_response(403, 'Bearer realm="aimeat connect serve"'))


def _write_serve(tmp_path, monkeypatch, doc: dict) -> None:
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    (tmp_path / "serve.json").write_text(json.dumps(doc), encoding="utf-8")


def test_the_spawner_reads_the_current_secret_on_every_call(tmp_path, monkeypatch):
    from crewaimeat.spawner import Spawner

    _write_serve(tmp_path, monkeypatch, {"port": 40999, "secret": "first"})
    sp = Spawner.__new__(Spawner)  # only the header builder is under test; it holds no state
    assert sp._daemon_headers("postman") == {"X-Aimeat-Agent": "postman", "Authorization": "Bearer first"}
    _write_serve(tmp_path, monkeypatch, {"port": 40999, "secret": "after-restart"})
    assert sp._daemon_headers("postman")["Authorization"] == "Bearer after-restart"


def test_agency2_sends_the_secret(tmp_path, monkeypatch):
    from crewaimeat.agency2 import node

    _write_serve(tmp_path, monkeypatch, {"port": 40999, "secret": "s3"})
    assert node.daemon_headers("uutiset#teemu@n") == {
        "X-Aimeat-Agent": "uutiset#teemu@n",
        "Authorization": "Bearer s3",
    }

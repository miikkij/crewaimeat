"""concierge_crew — the SSRF guard on the file-fetch tool. Negative cases only (no external network)."""

from __future__ import annotations

import importlib.util as u

import pytest

_spec = u.spec_from_file_location("concierge_crew", "crews/concierge_crew.py")
cc = u.module_from_spec(_spec)
_spec.loader.exec_module(cc)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/secret",  # loopback (resolves locally, no DNS)
        "http://127.0.0.1/x",  # loopback literal
        "http://[::1]/x",  # ipv6 loopback
        "file:///etc/passwd",  # non-http scheme
        "ftp://example.com/x",  # non-http scheme
        "not-a-url",  # no host
        "",  # empty
    ],
)
def test_is_safe_url_blocks_unsafe(url):
    assert cc._is_safe_url(url) is False


def test_dm_request_uses_event_message_not_stale_thread(monkeypatch):
    """Read-after-write: the thread may not yet contain the just-arrived DM. The request must be THIS
    event's message (via preview / id-match), never the previous inbound — and not leak into context."""
    import crewaimeat.dm as dmmod

    stale_thread = {
        "messages": [
            {"id": "a", "direction": "inbound", "body": "find 4 cosy cabins"},
            {"id": "b", "direction": "outbound", "body": "here are cabins"},
        ]
    }
    monkeypatch.setattr(dmmod, "dm_thread", lambda agent, conv, **k: stale_thread)
    event = {"id": "c", "conversationId": "x", "senderGhii": "u@n", "preview": "make an image of a neon fox"}
    req, ctx = cc._dm_request_and_context(event)
    assert req == "make an image of a neon fox"  # the EVENT's message, not inbound[-1] == "find 4 cosy cabins"
    assert "neon fox" not in ctx  # the current request never leaks into context


def test_concierge_tools_present():
    # 5 concierge tools + however many web tools — at least find_images/fetch_file/generate_image/help.
    names = {getattr(t, "name", "") for t in cc._concierge_tools({"attachments": []})}
    assert {"find_images", "fetch_file", "find_file", "generate_image", "describe_capabilities"} <= names

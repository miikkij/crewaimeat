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


def test_ask_user_tool_only_with_dm_context():
    # the clarify + offer tools need a recipient + thread; absent on the task path, present for a DM
    assert "ask_user" not in {getattr(t, "name", "") for t in cc._concierge_tools({"attachments": []})}
    with_ctx = {getattr(t, "name", "") for t in cc._concierge_tools({"attachments": []}, ask_to="u@n", ask_conv="c1")}
    assert {"ask_user", "offer_documents"} <= with_ctx


def test_deliver_picked_docs(monkeypatch):
    """The user ticked offered docs -> download + attach exactly those from the store (no LLM, no re-search)."""
    pending = {
        "ext": "pdf",
        "items": [
            {"id": "d0", "label": "Form A", "url": "https://x.org/a.pdf"},
            {"id": "d1", "label": "Form B", "url": "https://x.org/b.pdf"},
        ],
    }
    monkeypatch.setattr(
        cc.session_store, "session_get", lambda a, c, k, default=None: pending if k == "doc_candidates" else default
    )
    monkeypatch.setattr(cc.session_store, "session_clear", lambda *a, **k: None)
    monkeypatch.setattr(
        cc, "_fetch_url_bytes", lambda url, **k: (b"%PDF-1.4", "application/pdf", url.rsplit("/", 1)[-1])
    )
    monkeypatch.setattr(cc.dm, "dm_attach_bytes", lambda a, d, *, name, mime: {"name": name, "mime": mime})

    out = cc._deliver_picked_docs("conv", {"pick_docs": {"selected": ["d1"], "other": None}})
    assert out and len(out["attachments"]) == 1 and "Form B" in out["text"] and "Form A" not in out["text"]

    # nothing pending -> None (let the LLM path handle a generic clarify answer)
    monkeypatch.setattr(cc.session_store, "session_get", lambda a, c, k, default=None: None)
    assert cc._deliver_picked_docs("conv", {"pick_docs": {"selected": ["d1"]}}) is None

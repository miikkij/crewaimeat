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


def test_concierge_tools_present():
    # 5 concierge tools + however many web tools — at least find_images/fetch_file/generate_image/help.
    names = {getattr(t, "name", "") for t in cc._concierge_tools({"attachments": []})}
    assert {"find_images", "fetch_file", "generate_image", "describe_capabilities"} <= names

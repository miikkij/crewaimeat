"""Links for PEOPLE use the place's public address, never the crew's own connection to the node.

A crew reaches its node through the address in its credential (`node_url`). On a hosted place that is the
container's loopback, `http://127.0.0.1:40050`, which is right for the crew's own calls and useless to
anyone else. Measured 2026-10-03 on a sold place: asked for an image, the concierge answered
"Kuvan osoite: http://127.0.0.1:40050/v1/pub/<owner>/images/<key>" -- a link no customer can open.

So a link handed to a person goes through `person_link`, and its base comes from `public_base`:
  1. AIMEAT_BASE_URL -- the place's public address, which the fleet passes to every crew
     (https://<name>.aimeat.io; aimeat-commercial 07b7f9d). Set by the operator, so taken as given.
  2. The node's own answer: GET /v1/spec names its configured base in a `Link: <base>/v1/docs;
     rel="canonical"` header -- used when that base is not an internal address.
  3. The credential's node address, when it is not an internal address (aimeat.io and other real nodes).
  4. Nothing. Then there is no link, and the caller says why instead of printing an internal address.

A URL the CREW fetches itself (the browser verify gates, re-downloading its own image to attach it) keeps
the internal address: that is the one that works from inside the container.
"""

from __future__ import annotations

import ipaddress
import os
import re
import sys
import urllib.parse

_CACHE: dict[str, str | None] = {}
_CANONICAL = re.compile(r"<([^>]+)/v1/docs>\s*;\s*rel=\"?canonical\"?", re.IGNORECASE)

NO_PUBLIC_ADDRESS = (
    "the place's public address is unknown (AIMEAT_BASE_URL is not set and the node names no public "
    "address), so no link a person can open can be given"
)


def is_internal(url: str | None) -> bool:
    """True for an address only this machine or its network can reach: loopback, private and link-local
    IPs, the unspecified address, `localhost`, single-label hosts (a container's service name) and
    .local/.internal names. An address that is not a URL at all counts as internal."""
    try:
        host = (urllib.parse.urlparse(url or "").hostname or "").lower()
    except ValueError:
        return True
    if not host:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host == "localhost" or host.endswith((".localhost", ".local", ".internal")) or "." not in host
    return ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified


def _node_url(agent: str) -> str | None:
    try:
        from crewaimeat.aimeat_crew import _aimeat_read_token
        from crewaimeat.generator_tool import _discover_owner

        if _aimeat_read_token is None:
            return None
        _tok, url = _aimeat_read_token(agent, owner=_discover_owner(agent))
        return url or None
    except Exception:  # noqa: BLE001 -- no credential is an answer: no node address
        return None


def _node_canonical(agent: str) -> str | None:
    """The base the node itself is configured with, from GET /v1/spec's canonical Link header, asked as
    `agent` through the shared transport (the response's headers are what is read)."""
    try:
        from crewaimeat.aimeat_crew import _aimeat_request

        r = _aimeat_request(agent, "GET", "/v1/spec", timeout=10)
    except Exception:  # noqa: BLE001 -- no answer is no canonical address
        return None
    m = _CANONICAL.search((getattr(r, "headers", None) or {}).get("Link") or "")
    return m.group(1).rstrip("/") if m else None


def public_base(agent: str | None = None) -> str | None:
    """The place's public address for links given to people, or None when none is known (see module)."""
    env = (os.getenv("AIMEAT_BASE_URL") or "").strip()
    if env.startswith(("http://", "https://")):
        return env.rstrip("/")
    key = agent or ""
    if key in _CACHE:
        return _CACHE[key]
    found: str | None = None
    node = _node_url(agent) if agent else None
    if node:
        canonical = _node_canonical(agent)
        if canonical and not is_internal(canonical):
            found = canonical
        elif not is_internal(node):
            found = node.rstrip("/")
    if found is None:
        print(f"[links] {agent or '?'}: {NO_PUBLIC_ADDRESS}", file=sys.stderr)
    _CACHE[key] = found
    return found


def person_link(url_or_path: str | None, agent: str | None = None) -> str | None:
    """`url_or_path` as a link a person can open: a path gets the public base, an internal address has its
    origin replaced by the public base, a public address is left alone. None when no public base is known
    and one is needed."""
    if not url_or_path:
        return None
    if url_or_path.startswith("/"):
        base = public_base(agent)
        return base + url_or_path if base else None
    if not is_internal(url_or_path):
        return url_or_path
    base = public_base(agent)
    if not base:
        return None
    p = urllib.parse.urlparse(url_or_path)
    rest = p.path + (f"?{p.query}" if p.query else "") + (f"#{p.fragment}" if p.fragment else "")
    return base + rest


def internal_url(url_or_path: str, agent: str) -> str | None:
    """The address the CREW uses to fetch `url_or_path` itself: its own node address in front of a path.
    A whole URL is returned as it is."""
    if not url_or_path.startswith("/"):
        return url_or_path
    node = _node_url(agent)
    return node.rstrip("/") + url_or_path if node else None

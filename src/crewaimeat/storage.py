"""Authed binary download from AIMEAT node storage — the CLEAN-bytes path.

The serve loopback proxy (and `GET /v1/storage/<key>` taken over it) serialize a file as a JSON/UTF-8
STRING, which corrupts binary (non-UTF-8 bytes become U+FFFD replacement chars — irreversible). A DIRECT
authed node GET streams the raw bytes with the right Content-Type, so we fetch the object straight from
the node with the agent's bearer token, bypassing the loopback. This works both for the agent's OWN keys
and for a received DM attachment's key the agent holds a federation read grant on (granted on accept).
"""

from __future__ import annotations

import sys
import urllib.parse

import requests

from crewaimeat.generator_tool import _discover_owner, _token

_MAX_BYTES = 25 * 1024 * 1024  # 25 MB cap — matches the concierge fetch guard


def fetch_bytes(agent: str, key: str, *, max_bytes: int = _MAX_BYTES) -> tuple[bytes, str] | None:
    """Download a storage object by `key` straight from the node (clean binary). Returns (data, mime) or
    None on any failure. Caller/grant-scoped: the node authorizes by the agent's token, so an agent can
    read its own keys and any DM attachment it was granted. The loopback path is deliberately NOT used
    (it text-corrupts binary)."""
    if not key:
        return None
    owner = _discover_owner(agent)
    tok, url = _token(agent, owner)
    if not tok or not url:
        print(f"[{agent}] storage fetch: no token/url", file=sys.stderr)
        return None
    # Encode the key for the path but keep '/' separators; the stored key is raw (spaces become %20).
    safe_key = urllib.parse.quote(key, safe="/")
    try:
        with requests.get(
            f"{url.rstrip('/')}/v1/storage/{safe_key}",
            headers={"Authorization": f"Bearer {tok}"},
            stream=True,
            timeout=120,
        ) as r:
            if r.status_code != 200:
                print(f"[{agent}] storage fetch {key}: HTTP {r.status_code}", file=sys.stderr)
                return None
            mime = (r.headers.get("Content-Type") or "application/octet-stream").split(";")[0].strip()
            buf = bytearray()
            for chunk in r.iter_content(64 * 1024):
                buf += chunk
                if len(buf) > max_bytes:
                    print(f"[{agent}] storage fetch {key}: exceeds {max_bytes} bytes", file=sys.stderr)
                    return None
            return bytes(buf), mime
    except Exception as exc:  # noqa: BLE001
        print(f"[{agent}] storage fetch {key} failed: {exc!r}", file=sys.stderr)
        return None

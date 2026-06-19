"""seedream-gen — DETERMINISTIC text→image via OpenRouter ByteDance Seedream 4.5.

Not an LLM-reasoning task: the prompt IS the brief. ONE OpenRouter `/chat/completions` call with
`modalities:["image"]` (image-only — NOT ["image","text"]; that 404s) returns the image as a base64
data URI in `choices[0].message.images[0].image_url.url` (the mime can be jpeg or png — read it from
the URI). ~$0.04/image. We decode it and upload to the agent's PUBLIC storage (presigned — the binary
never base64s back through MCP/the tunnel), returning a `GET /v1/pub/<gaii>/<key>` URL anyone can
render. Mirrors image_contract's upload path. Seedream is an IMAGE model, called directly here — NOT
via get_llm/llm_providers (those route text LLMs).
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import os
import re
import sys
import urllib.parse

import requests

from crewaimeat.aimeat_crew import _aimeat_call, _serve_api
from crewaimeat.generator_tool import _discover_owner, _token

_MODEL = os.getenv("SEEDREAM_MODEL", "bytedance-seed/seedream-4.5")
_IMAGE_MIMES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
_GAII_CACHE: dict[str, str] = {}


def _own_gaii(agent: str) -> str | None:
    """The agent's GAII (for /v1/pub/<gaii>/<key> URLs). Discovered once via agents_list."""
    if agent in _GAII_CACHE:
        return _GAII_CACHE[agent]
    data = _aimeat_call(agent, "aimeat_agents_list", {}) or {}
    for a in data.get("agents") or []:
        if a.get("name") == agent and a.get("gaii"):
            _GAII_CACHE[agent] = a["gaii"]
            return a["gaii"]
    return None


def _upload_public(agent: str, key: str, image: bytes, mime: str) -> bool:
    """Upload bytes to the agent's storage with visibility=public via the PRESIGNED flow (binary stays
    binary): POST /v1/storage {key, mime_type, visibility, mode:'presigned'} → PUT raw bytes to upload_url.
    Same as image_contract._upload_public."""
    presign = {"key": key, "mime_type": mime, "visibility": "public", "mode": "presigned"}
    try:
        api = _serve_api()
        if api is not None:
            base, session = api
            r = session.post(f"{base}/v1/storage", json=presign, headers={"X-Aimeat-Agent": agent}, timeout=60)
        else:
            tok, url = _token(agent, _discover_owner(agent))
            if not tok or not url:
                return False
            r = requests.post(
                f"{url.rstrip('/')}/v1/storage", json=presign, headers={"Authorization": f"Bearer {tok}"}, timeout=60
            )
        upload_url = ((r.json() or {}).get("data") or {}).get("upload_url") if r.status_code == 200 else None
        if not upload_url:
            print(f"[seedream] presign {key} failed: HTTP {r.status_code} {r.text[:160]}", file=sys.stderr)
            return False
        put = requests.put(upload_url, data=image, headers={"Content-Type": mime}, timeout=180)
        return put.status_code in (200, 201)
    except Exception as exc:  # noqa: BLE001
        print(f"[seedream] upload {key} failed: {exc!r}", file=sys.stderr)
        return False


def _pub_url(agent: str, gaii: str, key: str) -> str:
    _tok, url = _token(agent, _discover_owner(agent))
    base = (url or "https://aimeat.io").rstrip("/")
    return f"{base}/v1/pub/{urllib.parse.quote(gaii, safe='')}/{key}"


def generate_image(agent_name: str, prompt: str, *, size: str = "2K", aspect_ratio: str | None = None) -> dict:
    """Generate ONE image from `prompt` (Seedream 4.5), upload it public, and return
    {ok, url, mime, bytes} or {ok:False, error}. Fails soft (returns the error string) — never raises,
    so a crew tool can report it cleanly."""
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        return {"ok": False, "error": "OPENROUTER_API_KEY not set"}
    prompt = (prompt or "").strip()
    if not prompt:
        return {"ok": False, "error": "empty prompt"}
    image_config: dict = {"size": size}
    if aspect_ratio:
        image_config["aspect_ratio"] = aspect_ratio
    body = {
        "model": _MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "modalities": ["image"],
        "image_config": image_config,
    }
    try:
        r = requests.post(
            os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/") + "/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "https://crewaimeat.local",
                "X-Title": "crewaimeat image-maker",
            },
            json=body,
            timeout=180,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"request failed: {exc!r}"}
    if r.status_code != 200:
        return {"ok": False, "error": f"OpenRouter HTTP {r.status_code}: {r.text[:300]}"}
    try:
        msg = r.json()["choices"][0]["message"]
        url = (msg.get("images") or [])[0]["image_url"]["url"]
    except (KeyError, IndexError, TypeError, ValueError):
        return {"ok": False, "error": f"no image in response: {str(r.text)[:300]}"}
    m = re.match(r"^data:(image/\w+);base64,(.+)$", url, re.DOTALL)
    if not m:
        return {"ok": False, "error": "unexpected image url (not a base64 data URI)"}
    mime, b64 = m.group(1), m.group(2)
    ext = _IMAGE_MIMES.get(mime, "img")
    data = base64.b64decode(b64)
    gaii = _own_gaii(agent_name)
    if not gaii:
        return {"ok": False, "error": "could not resolve the agent's GAII (is it registered + on the tunnel?)"}
    h = hashlib.sha256(data).hexdigest()[:10]
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    key = f"images/{stamp}-{h}.{ext}"
    if not _upload_public(agent_name, key, data, mime):
        return {"ok": False, "error": "upload to public storage failed"}
    pub = _pub_url(agent_name, gaii, key)
    # Record the deliverable (task-runner convention) so the offer's sample + a history exist.
    _aimeat_call(
        agent_name,
        "aimeat_memory_write",
        {
            "key": f"crews.{agent_name}.images.{stamp}-{h}",
            "value": {"prompt": prompt, "url": pub, "mime": mime, "bytes": len(data)},
            "visibility": "public",
        },
    )
    return {"ok": True, "url": pub, "mime": mime, "bytes": len(data)}


def make_image_tools(agent_name: str) -> list:
    """The single image-generation tool for the crew (deterministic; the LLM only crafts the prompt)."""
    from crewai.tools import tool

    @tool("generate_image")
    def generate_image_tool(prompt: str, size: str = "2K", aspect_ratio: str = "") -> str:
        """Generate ONE image from a vivid text prompt (ByteDance Seedream 4.5) and return its public URL.
        Call this ONCE with a rich, specific prompt (subject, style, lighting, composition, mood). `size`:
        0.5K | 1K | 2K | 4K (default 2K). `aspect_ratio` optional (e.g. 1:1, 16:9, 9:16). Returns the image
        URL, or an error string to report. Costs ~$0.04 per image — generate once, don't retry on a good result."""
        res = generate_image(agent_name, prompt, size=(size or "2K"), aspect_ratio=(aspect_ratio or None))
        if res.get("ok"):
            return f"Image generated: {res['url']}  ({res.get('mime')}, {res.get('bytes')} bytes)"
        return f"Image generation FAILED: {res.get('error')}"

    generate_image_tool.cache_function = lambda *_a, **_k: False
    return [generate_image_tool]

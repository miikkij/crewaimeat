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

from crewaimeat.aimeat_crew import _aimeat_call
from crewaimeat.ledger_report import report_llm_usage

_MODEL = os.getenv("SEEDREAM_MODEL", "bytedance-seed/seedream-4.5")
_IMAGE_MIMES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
_GAII_CACHE: dict[str, str] = {}

# Images generated since the last drain. The crew's clean_deliverable drains this at task-completion
# to guarantee the public URL(s) land IN the published deliverable text — the LLM's final answer
# sometimes echoes only the prompt and drops the URL, leaving the Tasks deliverable view with no
# thumbnail even though the image exists. Serial task-runner (max_concurrent=1), so a module-level
# accumulator maps cleanly onto one task.
_RECENT_IMAGES: list[dict] = []


def drain_recent_images() -> list[dict]:
    """Return the images generated since the last call and clear the buffer. Each item is
    {url, prompt, mime}. Empty when the task generated no image."""
    global _RECENT_IMAGES
    out = _RECENT_IMAGES
    _RECENT_IMAGES = []
    return out


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
        from crewaimeat.aimeat_crew import _aimeat_request

        r = _aimeat_request(agent, "POST", "/v1/storage", json=presign, timeout=60)
        upload_url = ((r.json() or {}).get("data") or {}).get("upload_url") if r.status_code == 200 else None
        if not upload_url:
            print(f"[seedream] presign {key} failed: HTTP {r.status_code} {r.text[:160]}", file=sys.stderr)
            return False
        put = requests.put(upload_url, data=image, headers={"Content-Type": mime}, timeout=180)
        return put.status_code in (200, 201)
    except Exception as exc:  # noqa: BLE001
        print(f"[seedream] upload {key} failed: {exc!r}", file=sys.stderr)
        return False


def _pub_path(gaii: str, key: str) -> str:
    return f"/v1/pub/{urllib.parse.quote(gaii, safe='')}/{key}"


def _pub_url(agent: str, gaii: str, key: str) -> str | None:
    """The link a PERSON opens: the place's public address in front (crewaimeat.public_url), never the
    crew's own loopback connection to the node. None when no public address is known."""
    from crewaimeat.public_url import person_link

    return person_link(_pub_path(gaii, key), agent)


def _generate_on_node(agent_name: str, prompt: str) -> dict:
    """THE NODE ROAD: POST /v1/ai/image as the agent. The node picks the model (the owner's image
    choice, not Seedream by name), its key pays, and the picture lands public in storage; the
    Seedream-specific `size`/`aspect_ratio` are not sent, because they mean nothing to another model."""
    from crewaimeat import node_ai

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    h = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:10]
    try:
        res = node_ai.generate_image(agent_name, prompt, public=True, storage_key=f"images/{stamp}-{h}")
    except node_ai.NodeAiError as exc:
        return {"ok": False, "error": f"the node did not generate the image ({exc})"}
    from crewaimeat.public_url import NO_PUBLIC_ADDRESS, internal_url, person_link

    answered, key, mime = res.get("url"), res.get("storage_key"), res.get("mime_type") or "image/png"
    if not answered or not key:
        return {"ok": False, "error": f"the node's answer carried no image address: {str(res)[:300]}"}
    # The node answers with a path ("/v1/pub/<owner>/<key>", measured 2026-10-03). TWO addresses come of
    # it: the link a PERSON opens, on the place's public address -- a sold place's concierge once gave a
    # customer http://127.0.0.1:40050/... -- and the one the CREW fetches the bytes from itself (the
    # concierge attaches the picture), on its own node address, which works from inside the container.
    url = person_link(answered, agent_name)
    fetch_url = internal_url(answered, agent_name)
    if not url:
        return {"ok": False, "error": f"the image is stored at {key}, but {NO_PUBLIC_ADDRESS}", "key": key}
    print(f"[seedream] {agent_name}: image generated on the node by {res.get('model')}", file=sys.stderr)
    _aimeat_call(
        agent_name,
        "aimeat_memory_write",
        {
            "key": f"crews.{agent_name}.images.{stamp}-{h}",
            "value": {"prompt": prompt, "url": url, "mime": mime, "bytes": res.get("size"), "model": res.get("model")},
            "visibility": "public",
        },
    )
    _RECENT_IMAGES.append({"url": url, "prompt": prompt, "mime": mime})
    return {
        "ok": True,
        "url": url,
        "fetch_url": fetch_url,
        "key": key,
        "gaii": node_ai.pub_owner_of(url),
        "mime": mime,
        "bytes": res.get("size"),
        "model": res.get("model"),
    }


def generate_image(agent_name: str, prompt: str, *, size: str = "2K", aspect_ratio: str | None = None) -> dict:
    """Generate ONE image from `prompt`, upload it public, and return {ok, url, mime, bytes} or
    {ok:False, error}. Fails soft (returns the error string) — never raises, so a crew tool can report
    it cleanly. On the node road the node generates it (crewaimeat.node_ai); otherwise Seedream 4.5
    with this machine's OPENROUTER_API_KEY; with neither, the error says so."""
    from crewaimeat import node_ai

    prompt = (prompt or "").strip()
    if not prompt:
        return {"ok": False, "error": "empty prompt"}
    # A picture nobody can open is not worth paying for: with no public address known, stop BEFORE the
    # generation rather than after it (crewaimeat.public_url).
    from crewaimeat.public_url import NO_PUBLIC_ADDRESS, public_base

    if public_base(agent_name) is None:
        return {"ok": False, "error": f"no image was generated: {NO_PUBLIC_ADDRESS}"}
    route = node_ai.road(agent_name)
    if route == node_ai.NODE:
        return _generate_on_node(agent_name, prompt)
    if route == node_ai.NONE:
        msg = node_ai.no_route("image generation", agent_name)
        print(f"[seedream] {msg}", file=sys.stderr)
        return {"ok": False, "error": msg}
    api_key = os.getenv("OPENROUTER_API_KEY")
    image_config: dict = {"size": size}
    if aspect_ratio:
        image_config["aspect_ratio"] = aspect_ratio
    body = {
        "model": _MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "modalities": ["image"],
        "image_config": image_config,
        # Ask OpenRouter to include the authoritative cost in `usage` so we can report it to the
        # AIMEAT ledger (this direct call bypasses CrewAI's LLM path, so the event-bus hook can't).
        "usage": {"include": True},
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
        resp_json = r.json()
        msg = resp_json["choices"][0]["message"]
        url = (msg.get("images") or [])[0]["image_url"]["url"]
    except (KeyError, IndexError, TypeError, ValueError):
        return {"ok": False, "error": f"no image in response: {str(r.text)[:300]}"}
    # This direct requests.post bypasses CrewAI's LLM path, so aimeat-crewai's event-bus usage
    # hook never sees it -- report the tokens+cost to the AIMEAT ledger ourselves (best-effort).
    report_llm_usage(_MODEL, resp_json.get("usage"), agent=agent_name)
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
    if not pub:
        from crewaimeat.public_url import NO_PUBLIC_ADDRESS

        return {"ok": False, "error": f"the image is stored at {key}, but {NO_PUBLIC_ADDRESS}", "key": key}
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
    # Remember it so clean_deliverable can guarantee the URL reaches the published deliverable text.
    _RECENT_IMAGES.append({"url": pub, "prompt": prompt, "mime": mime})
    # `key` + `gaii` travel with the URL: a consumer that ATTACHES the image (the julkaisupöytä app
    # attaches by storage key, and a URL alone cannot be attached) needs the address, not just the
    # link. They were computed here and thrown away, which made every caller re-derive or give up.
    from crewaimeat.public_url import internal_url

    return {
        "ok": True,
        "url": pub,
        "fetch_url": internal_url(_pub_path(gaii, key), agent_name),
        "key": key,
        "gaii": gaii,
        "mime": mime,
        "bytes": len(data),
    }


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

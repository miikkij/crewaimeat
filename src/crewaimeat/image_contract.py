"""image-scout: a DETERMINISTIC workspace-contract that turns image briefs into moodboards.

Contract:
  inputs : `moodboard-request` (records) — trigger: status == 'requested'
             { id, brief(required), n_images?(default 6, max 12), mode?('moodboard'|'upload-only'),
               status, requested_by?, result_ref?, error? }
  outputs: `moodboard` (DOCUMENT) — a gallery page (id = mood-<request id>): one section per image
             with the image itself (public storage URL), a vision-model description, style/color/tag
             metadata and the source link. 'upload-only' skips the document and just stores the images.
  lifecycle: requested -> in-progress (claim) -> done (+result_ref) | failed (+error)

Pipeline (plain code, per the canon — the LLM only looks at pictures):
  SearXNG image search (categories=images) -> download with guards (content-type, size caps,
  sha-256 dedup) -> vision metadata (qwen-vl via OpenRouter: subject/description/style/colors/tags
  + relevance) -> keep the top n by relevance -> upload to agent storage (visibility=public, so the
  document renders for every viewer via GET /v1/pub/<gaii>/<key>) -> write the moodboard document.

All use is INTERNAL (reference/moodboard); the agent never posts anywhere external. Uploads go
through the loopback serve proxy's /v1/* surface (POST /v1/storage with {key, data, mime_type,
visibility}) because the aimeat_storage_upload shell-tool mapping is broken (sends `content`,
drops `visibility` — reported), with a direct-REST fallback when no daemon is available.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
import os
import re
import sys
import urllib.parse

import requests
from crewai.tools import tool

from crewaimeat.aimeat_crew import _aimeat_call, _serve_api
from crewaimeat.generator_tool import _discover_owner, _token

AGENT = "image-scout"
IN_SPACE, IN_NS = "moodboard-request", "shared.moodboard_requests"
OUT_SPACE, OUT_NS = "moodboard", "shared.moodboards"  # a DOCUMENT space

_DEFAULT_VISION_MODEL = "qwen/qwen3-vl-30b-a3b-instruct"  # same default as browser_tool
_MAX_IMAGE_BYTES = 4 * 1024 * 1024   # tunnel WS frames are capped; base64 inflates ~1.37x
_MIN_IMAGE_BYTES = 5 * 1024          # skip icons/trackers
_IMAGE_MIMES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif"}

# Runaway guard (canon rule 5): ids handled THIS run; the OUTPUT-dedup below is the primary,
# restart-surviving guard.
_PROCESSED: set[str] = set()

_GAII_CACHE: dict[str, str] = {}


def _call(tool_name: str, payload: dict):
    return _aimeat_call(AGENT, tool_name, payload)


def _member_workspaces() -> list[tuple[str, str]]:
    """(organism_id, ws_id) for every workspace this agent can list (is a member of)."""
    data = _call("aimeat_organism_list", {}) or {}
    orgs = data.get("organisms") or (data if isinstance(data, list) else [])
    pairs: list[tuple[str, str]] = []
    for o in orgs:
        oid = o.get("id") if isinstance(o, dict) else None
        if not oid:
            continue
        wl = _call("aimeat_workspace_list", {"organism_id": oid}) or {}
        pairs.extend((oid, w["id"]) for w in (wl.get("workspaces") or []) if w.get("id"))
    return pairs


def _own_gaii() -> str | None:
    """This agent's GAII (for /v1/pub/<gaii>/<key> URLs). Discovered once via agents_list."""
    if AGENT in _GAII_CACHE:
        return _GAII_CACHE[AGENT]
    data = _call("aimeat_agents_list", {}) or {}
    for a in data.get("agents") or []:
        if a.get("name") == AGENT and a.get("gaii"):
            _GAII_CACHE[AGENT] = a["gaii"]
            return a["gaii"]
    return None


def _searxng_images(query: str, n: int) -> list[dict]:
    """SearXNG image search -> [{img_src, url(source page), title}]."""
    base = os.getenv("SEARXNG_URL", "http://localhost:21333").rstrip("/")
    try:
        r = requests.get(base + "/search",
                         params={"q": query, "format": "json", "categories": "images"},
                         timeout=20)
        out = []
        for it in (r.json().get("results") or []):
            src = it.get("img_src") or ""
            if src.startswith("http"):
                out.append({"img_src": src, "url": it.get("url") or src, "title": it.get("title") or ""})
            if len(out) >= n:
                break
        return out
    except Exception:  # noqa: BLE001
        return []


def _download_image(url: str) -> tuple[bytes, str] | None:
    """Fetch one image with guards: image/* content-type, known format, size window. (bytes, mime)."""
    try:
        r = requests.get(url, timeout=25, headers={"User-Agent": "Mozilla/5.0 (image-scout)"})
        mime = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if mime not in _IMAGE_MIMES:
            return None
        b = r.content
        if not (_MIN_IMAGE_BYTES <= len(b) <= _MAX_IMAGE_BYTES):
            return None
        return b, mime
    except Exception:  # noqa: BLE001
        return None


def _vision_meta(image: bytes, mime: str, brief: str) -> dict | None:
    """Describe one image with the vision model -> {subject, description, style, colors, tags,
    relevance(0-10)}. Same OpenRouter wiring as browser_tool's screenshot describe."""
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        return None
    model = os.getenv("VISION_MODEL", _DEFAULT_VISION_MODEL).removeprefix("openrouter/")
    data_uri = f"data:{mime};base64," + base64.b64encode(image).decode()
    prompt = (
        f'The moodboard brief is: "{brief}".\n'
        "Look at the image and answer with STRICT JSON only (no prose, no code fence):\n"
        '{"subject": "<3-8 words>", "description": "<1-2 factual sentences>", '
        '"style": "<visual style>", "colors": ["<dominant>", "..."], '
        '"tags": ["<5-8 short tags>"], "relevance": <0-10 fit to the brief>}'
    )
    try:
        r = requests.post(
            os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "messages": [{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_uri}},
            ]}], "max_tokens": 400},
            timeout=90,
        )
        text = (r.json()["choices"][0]["message"]["content"] or "").strip()
        m = re.search(r"\{.*\}", text, re.DOTALL)
        meta = json.loads(m.group(0)) if m else None
        if isinstance(meta, dict) and meta.get("subject"):
            meta["relevance"] = max(0, min(10, int(meta.get("relevance") or 0)))
            return meta
        return None
    except Exception as exc:  # noqa: BLE001
        print(f"[{AGENT}] vision describe failed: {exc!r}", file=sys.stderr)
        return None


def _upload_public(key: str, image: bytes, mime: str) -> bool:
    """Upload to this agent's storage with visibility=public via the loopback /v1/* proxy
    (direct REST with the agent token as the no-daemon fallback)."""
    body = {"key": key, "data": base64.b64encode(image).decode(), "mime_type": mime, "visibility": "public"}
    try:
        api = _serve_api()
        if api is not None:
            base, session = api
            r = session.post(f"{base}/v1/storage", json=body,
                             headers={"X-Aimeat-Agent": AGENT}, timeout=120)
        else:
            tok, url = _token(AGENT, _discover_owner(AGENT))
            if not tok or not url:
                return False
            r = requests.post(f"{url.rstrip('/')}/v1/storage", json=body,
                              headers={"Authorization": f"Bearer {tok}"}, timeout=120)
        return r.status_code in (200, 201)
    except Exception as exc:  # noqa: BLE001
        print(f"[{AGENT}] upload {key} failed: {exc!r}", file=sys.stderr)
        return False


def _pub_url(gaii: str, key: str) -> str:
    _tok, url = _token(AGENT, _discover_owner(AGENT))
    base = (url or "https://aimeat.io").rstrip("/")
    return f"{base}/v1/pub/{urllib.parse.quote(gaii, safe='')}/{key}"


def build_moodboard(rid: str, brief: str, n_images: int = 6) -> tuple[list[dict], str | None]:
    """Search -> download -> vision -> top-n -> upload. Returns (kept_items, error)."""
    gaii = _own_gaii()
    if not gaii:
        return [], "could not resolve this agent's GAII for public image URLs"
    candidates = _searxng_images(brief, n_images * 3)
    if not candidates:
        return [], "image search returned nothing (is SEARXNG_URL up?)"
    seen_hashes: set[str] = set()
    rated: list[dict] = []
    for c in candidates:
        if len(rated) >= n_images * 2:  # enough rated candidates to choose from
            break
        got = _download_image(c["img_src"])
        if not got:
            continue
        image, mime = got
        h = hashlib.sha256(image).hexdigest()
        if h in seen_hashes:
            continue
        seen_hashes.add(h)
        meta = _vision_meta(image, mime, brief)
        if not meta:
            continue
        rated.append({**c, "image": image, "mime": mime, "hash": h, "meta": meta})
    rated.sort(key=lambda x: x["meta"]["relevance"], reverse=True)
    kept = []
    for i, item in enumerate(rated[:n_images], start=1):
        key = f"moodboards/{rid}/{i:02d}-{item['hash'][:8]}.{_IMAGE_MIMES[item['mime']]}"
        if not _upload_public(key, item["image"], item["mime"]):
            continue
        kept.append({**{k: item[k] for k in ("url", "title", "meta")}, "pub": _pub_url(gaii, key)})
    if not kept:
        return [], "no image survived download/vision/upload"
    return kept, None


def _moodboard_markdown(brief: str, items: list[dict], today: str) -> str:
    head = (f"*{len(items)} images · curated by image-scout · {today} · internal reference use only "
            f"(sourced from the open web)*\n")
    parts = [head]
    for i, it in enumerate(items, start=1):
        m = it["meta"]
        parts.append(
            f"## {i} · {m.get('subject', 'Untitled')}\n\n"
            f"![{m.get('subject', '')}]({it['pub']})\n\n"
            f"{m.get('description', '')}\n\n"
            f"**Style:** {m.get('style', '?')} · **Colors:** {', '.join(m.get('colors') or [])} · "
            f"**Tags:** {', '.join(m.get('tags') or [])}\n\n"
            f"Source: {it['url']} · relevance {m.get('relevance')}/10\n"
        )
    return "\n".join(parts)


def _advance(oid: str, wid: str, req: dict, **changes) -> None:
    rec = {k: v for k, v in {**req, **changes}.items() if not k.startswith("_")}
    if _call("aimeat_workspace_write", {"organism_id": oid, "ws": wid, "space": IN_SPACE, "id": rec["id"], "value": rec}):
        _call("aimeat_workspace_publish", {"organism_id": oid, "ws": wid, "namespace": IN_NS, "id": rec["id"]})


def process_moodboards(max_items: int = 2, targets: list[tuple[str, str]] | None = None) -> dict:
    """Fulfil pending `moodboard-request` records across the agent's member workspaces.

    Deterministic: discover -> claim -> search/download/vision/upload -> write the moodboard
    document -> advance. Bounded (max_items per pass — images are heavy); output-dedup settles a
    request whose document already exists. Returns counts."""
    pairs = targets if targets is not None else _member_workspaces()
    today = datetime.date.today().isoformat()
    processed = failed = 0
    for oid, wid in pairs:
        if processed + failed >= max_items:
            break
        data = _call("aimeat_workspace_read", {"organism_id": oid, "ws": wid})
        if not data or data.get("manifest") is None:
            continue
        reqs = (data.get("objects", {}) or {}).get(IN_SPACE) or []
        existing = {r.get("id") for r in ((data.get("objects", {}) or {}).get(OUT_SPACE) or [])}
        for req in reqs:
            rid = req.get("id")
            if req.get("status") != "requested" or not rid:
                continue
            if rid in _PROCESSED:  # per-run guard against a stale 'requested' read
                continue
            if f"mood-{rid}" in existing:  # output-dedup: already fulfilled -> settle
                _PROCESSED.add(rid)
                _advance(oid, wid, req, status="done", result_ref=f"mood-{rid}")
                continue
            if processed + failed >= max_items:
                break
            _PROCESSED.add(rid)
            _advance(oid, wid, req, status="in-progress")  # CLAIM
            n = max(1, min(12, int(req.get("n_images") or 6)))
            items, err = build_moodboard(rid, req.get("brief", ""), n)
            if err:
                _advance(oid, wid, req, status="failed", error=err[:300])
                failed += 1
                print(f"[{AGENT}] moodboard FAILED for {rid}: {err}", file=sys.stderr)
                continue
            if (req.get("mode") or "moodboard") == "upload-only":
                keys = "\n".join(f"- {it['pub']}" for it in items)
                _advance(oid, wid, req, status="done", result_ref=f"{len(items)} images uploaded:\n{keys}"[:900])
                processed += 1
                continue
            out_id = f"mood-{rid}"
            title = f"Moodboard · {(req.get('brief') or '').strip()[:70]}"
            wrote = _call("aimeat_workspace_write",
                          {"organism_id": oid, "ws": wid, "space": OUT_SPACE, "id": out_id,
                           "value": {"title": title, "markdown": _moodboard_markdown(req.get("brief", ""), items, today)}})
            pub = _call("aimeat_workspace_publish",
                        {"organism_id": oid, "ws": wid, "namespace": OUT_NS, "id": out_id}) if wrote else None
            if wrote and pub:
                _advance(oid, wid, req, status="done", result_ref=out_id)
                processed += 1
            else:
                _advance(oid, wid, req, status="failed", error="moodboard write failed")
                failed += 1
    return {"processed": processed, "failed": failed}


def make_image_tools(agent_name: str) -> list:
    """The single contract-processing tool: fulfil moodboard-requests; never posts externally."""

    @tool("process_moodboards")
    def _process(max_items: int = 2) -> str:
        """Fulfil pending `moodboard-request` records in the workspaces this agent belongs to:
        search images for the brief (SearXNG), analyse each with a vision model (subject, style,
        colors, tags, relevance), upload the best to storage and write a `moodboard` document with
        the images embedded. Deterministic; internal use only. Returns the counts."""
        res = process_moodboards(max_items=max_items)
        return f"image-scout: processed {res['processed']} request(s), {res['failed']} failed."

    return [_process]

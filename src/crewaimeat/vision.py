"""Vision + document analysis for the concierge — "read this attachment and pull everything out of it".

Three layers:
  - analyze_image: ONE OpenRouter /chat/completions call to a VISION model with the image inlined as a
    base64 data URI -> a thorough text read (what's in it, any visible/printed text, notable details).
  - extract_document_text: pull text from a document — PDF via pypdf, or any decodable text/* file.
  - analyze_attachment: download an attachment's CLEAN bytes (storage.fetch_bytes) and route by MIME to
    the right reader, returning a labelled analysis block the crew can reason over.

The vision call mirrors seedream_gen's direct OpenRouter usage (OPENROUTER_API_KEY). The model is
overridable via CONCIERGE_VISION_MODEL; the default is a strong, widely-available vision model.
"""

from __future__ import annotations

import base64
import io
import os

import requests

from crewaimeat import storage
from crewaimeat.ledger_report import report_llm_usage

_VISION_MODEL = os.getenv("CONCIERGE_VISION_MODEL", "qwen/qwen-2.5-vl-72b-instruct")
_VISION_PROMPT = (
    "You are analysing an image a user sent. Describe it thoroughly and extract EVERYTHING useful: the "
    "subject and scene, any visible or printed text (transcribe it verbatim), names, numbers, dates, "
    "logos, charts/tables (give their values), and anything notable. Be concrete and complete; if the "
    "image is a document, screenshot, or form, read it like OCR. Then give a one-line summary."
)
_MAX_DOC_CHARS = 12000  # cap extracted document text fed downstream (keep the crew prompt bounded)
_IMG_PREFIXES = ("image/",)


def analyze_image(image_bytes: bytes, mime: str, *, prompt: str | None = None, agent: str | None = None) -> str:
    """Send one image to a vision model and return its text read. Fails soft -> a short error string.
    `agent` (when known) attributes the direct OpenRouter call to the AIMEAT usage ledger."""
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        return "(vision unavailable: OPENROUTER_API_KEY not set)"
    if not image_bytes:
        return "(empty image)"
    b64 = base64.b64encode(image_bytes).decode("ascii")
    body = {
        "model": _VISION_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt or _VISION_PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ],
            }
        ],
        # Ask OpenRouter for the authoritative cost so report_llm_usage can send it to the ledger
        # (this direct call bypasses CrewAI, so aimeat-crewai's event-bus hook can't meter it).
        "usage": {"include": True},
    }
    try:
        r = requests.post(
            os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/") + "/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "https://crewaimeat.local",
                "X-Title": "crewaimeat concierge vision",
            },
            json=body,
            timeout=180,
        )
    except Exception as exc:  # noqa: BLE001
        return f"(vision request failed: {exc!r})"
    if r.status_code != 200:
        return f"(vision HTTP {r.status_code}: {r.text[:200]})"
    try:
        resp_json = r.json()
        report_llm_usage(_VISION_MODEL, resp_json.get("usage"), agent=agent)
        content = resp_json["choices"][0]["message"]["content"]
        if isinstance(content, list):  # some providers return content parts
            content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
        return (content or "").strip() or "(vision returned no text)"
    except (KeyError, IndexError, TypeError, ValueError):
        return f"(unexpected vision response: {str(r.text)[:200]})"


def extract_document_text(data: bytes, mime: str, name: str = "") -> str:
    """Best-effort text from a document. PDF -> pypdf; text/json/csv/markdown -> decode. Returns the text
    (capped) or a short note. A PDF with no text layer (scanned) yields ~nothing -> say so."""
    m = (mime or "").lower()
    nm = (name or "").lower()
    if "pdf" in m or nm.endswith(".pdf"):
        try:
            from pypdf import PdfReader

            reader = PdfReader(io.BytesIO(data))
            parts = []
            for page in reader.pages:
                parts.append(page.extract_text() or "")
                if sum(len(p) for p in parts) > _MAX_DOC_CHARS:
                    break
            text = "\n".join(parts).strip()
            if not text:
                return "(this PDF has no extractable text layer — likely a scanned/image PDF)"
            return text[:_MAX_DOC_CHARS] + ("\n…(truncated)" if len(text) > _MAX_DOC_CHARS else "")
        except Exception as exc:  # noqa: BLE001
            return f"(could not parse PDF: {exc!r})"
    if m.startswith("text/") or m in ("application/json", "application/xml") or nm.endswith((".txt", ".md", ".csv")):
        try:
            text = data.decode("utf-8", "replace").strip()
            return text[:_MAX_DOC_CHARS] + ("\n…(truncated)" if len(text) > _MAX_DOC_CHARS else "")
        except Exception as exc:  # noqa: BLE001
            return f"(could not decode text file: {exc!r})"
    return f"(no reader for {mime or 'unknown type'} — I can read images, PDFs, and text files)"


def _att_field(att: dict, *names: str):
    for n in names:
        if att.get(n):
            return att[n]
    return None


def analyze_attachment(agent: str, att: dict, *, prompt: str | None = None) -> str:
    """Download an attachment's clean bytes and analyse it: images -> vision, documents -> text extract.
    Returns a labelled block ('[Attachment: name (mime)] …') or a short failure note. Never raises."""
    key = _att_field(att, "storageKey", "storage_key")
    name = _att_field(att, "name") or "attachment"
    mime = (_att_field(att, "mime", "mime_type") or "").lower()
    if not key:
        return f"[Attachment: {name}] (no storage key — cannot fetch)"
    got = storage.fetch_bytes(agent, key)
    if not got:
        return f"[Attachment: {name}] (could not download it — the file may not be shared with me)"
    data, dl_mime = got
    mime = mime or dl_mime
    if any(mime.startswith(p) for p in _IMG_PREFIXES):
        analysis = analyze_image(data, mime, prompt=prompt, agent=agent)
        return f"[Attachment: {name} (image, {len(data)} bytes)]\n{analysis}"
    analysis = extract_document_text(data, mime, name)
    return f"[Attachment: {name} ({mime}, {len(data)} bytes)]\n{analysis}"

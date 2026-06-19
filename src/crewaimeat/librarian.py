"""Librarian: a deterministic index over the fleet's deliverables, with durability/junk
classification and a reuse-before-redo lookup.

The librarian (an owner's agent) reads ALL same-owner deliverables via owner_scope memory and
answers "do we already have something relevant?" — so a coordinator can reuse prior work instead
of re-running expensive crews. Retrieval is deterministic (key-slug term overlap + recency); only
the top candidates are classified/reranked by a cheap LLM pass that also judges shelf-life:

  classify -> {keep, topic, durability, ttl_days, confidence, summary}
    durability: permanent (keeps for years) | slow (months) | fast (days) | ephemeral (junk -> drop)

See docs/librarian-design.md. v1: deterministic index + consult_librarian + classification.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from crewai.tools import tool

from crewaimeat.aimeat_crew import _aimeat_call
from crewaimeat.llm import get_llm
from crewaimeat.workflow import _items_of  # owner_scope list -> [{key, value}, ...]

# crews.<agent>.<slug>-<short>.latest_output  /  research.<agent>.<slug>-<short>.latest_output
_DELIVERABLE_KEY = re.compile(r"^(?:crews|research)\.([^.]+)\.(.+)\.latest_output$")

# Deterministic junk pre-filter — drop before spending an LLM call.
_JUNK_MARKERS = (
    "no results",
    "not found",
    "ei tuloksia",
    "ei julkista tietoa",
    "ei löytynyt",
    "no public information",
    "(empty)",
    "n/a",
)
_STOPWORDS = {
    "the",
    "a",
    "an",
    "of",
    "for",
    "and",
    "or",
    "to",
    "in",
    "on",
    "is",
    "it",
    "this",
    "that",
    "ja",
    "tai",
    "se",
    "joka",
    "mitä",
    "miten",
    "kuinka",
}

# Shelf-life in days per durability class (None = never decays).
DURABILITY_HALFLIFE_DAYS = {"permanent": None, "slow": 180, "fast": 7, "ephemeral": 0}


def _as_text(v) -> str:
    """Memory values may be stored as JSON (dict/list) or plain strings — coerce to text."""
    if isinstance(v, str):
        return v
    if v is None:
        return ""
    try:
        return json.dumps(v, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        return str(v)


def _tokens(text: str) -> set:
    return {w for w in re.split(r"[^a-z0-9äö]+", (text or "").lower()) if len(w) > 2 and w not in _STOPWORDS}


def _slug_to_text(slug: str) -> str:
    """A key slug like 'rate-the-feasibility-of-opening' -> readable terms."""
    return re.sub(r"-[0-9a-f]{6,}$", "", slug).replace("-", " ")


def _looks_like_junk(value: str) -> bool:
    v = (value or "").strip()
    if len(v) < 40:
        return True
    low = v.lower()
    return any(m in low for m in _JUNK_MARKERS) and len(v) < 200


def gather_deliverables(agent_name: str, prefix: str | None = None) -> list[dict]:
    """Every owner-visible deliverable entry: [{key, agent, slug, value}]. Reads with owner_scope so
    one call spans all same-owner agents (values included in the listing)."""
    payload: dict = {"owner_scope": True}
    if prefix:
        payload["prefix"] = prefix
    items = _items_of(_aimeat_call(agent_name, "aimeat_memory_list", payload))
    out = []
    for it in items:
        key = it.get("key") or ""
        m = _DELIVERABLE_KEY.match(key)
        if not m:
            continue
        out.append({"key": key, "agent": m.group(1), "slug": m.group(2), "value": _as_text(it.get("value"))})
    return out


def _parse_json(text: str) -> dict | None:
    """Extract the first {...} object from an LLM reply (tolerant of code fences/preamble)."""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return None


def classify_entry(content: str, llm=None, need: str | None = None) -> dict:
    """One cheap LLM pass: condense + classify shelf-life (+ judge fit to `need` if given).

    Returns {keep, topic, durability, ttl_days, confidence, summary, relevant?}. Junk is pre-filtered
    deterministically (keep=false) so it never costs an LLM call or enters the index."""
    if _looks_like_junk(content):
        return {
            "keep": False,
            "topic": "",
            "durability": "ephemeral",
            "ttl_days": 0,
            "confidence": 0.0,
            "summary": "",
            "relevant": "no",
        }
    llm = llm or get_llm(for_tool_use=False)
    fit_line = f'Also judge relevance to this need: "{need}". Add "relevant": "yes"|"partial"|"no".\n' if need else ""
    prompt = (
        "Classify this deliverable for a knowledge index. Reply with ONLY a JSON object:\n"
        '{"keep": bool, "topic": "<short kebab topic>", "durability": "permanent|slow|fast|ephemeral", '
        '"ttl_days": <int or null>, "confidence": <0.0-1.0>, "summary": "<=160 chars"}\n'
        "durability: permanent = a lasting fact (founding year, who won X in YEAR); slow = changes over "
        "months (board, headcount, current CEO); fast = stale in days (prices, latest news/funding); "
        "ephemeral = noise/'not found'/off-topic/made-up -> keep=false. ttl_days null for permanent.\n"
        + fit_line
        + "Content:\n"
        + content[:4000]
    )
    out = _parse_json(_safe_llm(llm, prompt)) or {}
    return {
        "keep": bool(out.get("keep", True)),
        "topic": str(out.get("topic", "") or ""),
        "durability": out.get("durability", "slow") if out.get("durability") in DURABILITY_HALFLIFE_DAYS else "slow",
        "ttl_days": out.get("ttl_days"),
        "confidence": float(out.get("confidence", 0.5) or 0.0),
        "summary": str(out.get("summary", "") or "")[:200],
        "relevant": out.get("relevant", "partial"),
    }


def _safe_llm(llm, prompt: str) -> str:
    try:
        return llm.call([{"role": "user", "content": prompt}]) or ""
    except Exception:  # noqa: BLE001
        return ""


def search_index(agent_name: str, need: str, top_k: int = 5, prefix: str | None = None) -> list[dict]:
    """Deterministic candidate scoring (slug term overlap) + LLM classify/rerank of the top few.
    Returns ranked [{key, agent, topic, durability, confidence, relevant, summary}], best first,
    dropping junk (keep=false) and clearly-irrelevant (relevant=no)."""
    need_t = _tokens(need)
    cands = gather_deliverables(agent_name, prefix=prefix)
    scored = []
    for c in cands:
        # Score on slug + value: the slug (first chars of the task) often omits topic words, but the
        # deliverable value carries them — and the value is already in the owner_scope listing (free).
        blob_t = _tokens(_slug_to_text(c["slug"]) + " " + (c.get("value") or ""))
        overlap = len(need_t & blob_t)
        if overlap:
            scored.append((overlap, c))
    scored.sort(key=lambda x: x[0], reverse=True)
    results = []
    for _ovl, c in scored[: max(top_k * 2, 8)]:
        meta = classify_entry(c.get("value") or _slug_to_text(c["slug"]), need=need)
        if not meta["keep"] or meta.get("relevant") == "no":
            continue
        results.append(
            {
                "key": c["key"],
                "agent": c["agent"],
                "topic": meta["topic"],
                "durability": meta["durability"],
                "confidence": meta["confidence"],
                "relevant": meta["relevant"],
                "summary": meta["summary"],
            }
        )
        if len(results) >= top_k:
            break
    rank = {"yes": 0, "partial": 1, "no": 2}
    results.sort(key=lambda r: (rank.get(r["relevant"], 1), -r["confidence"]))
    return results


LIBRARY_KEY = "agents.{agent}.library"
LIBRARY_CAP = 200  # keep the per-agent library compact (pointers, not payloads): newest N entries


def contribute_deliverable(agent_name: str, deliverable_key: str, content: str, llm=None) -> bool:
    """Classify a deliverable and append a compact pointer-entry to agents.<agent>.library
    (dedupe by key, cap to newest LIBRARY_CAP). Junk (keep=false) is skipped. The entry stores a
    summary + shelf-life + a pointer to the full deliverable key — never the payload. Returns True
    if an entry was written. Used by the scaffold's contribute_to_library hook."""
    meta = classify_entry(_as_text(content), llm=llm)
    if not meta.get("keep"):
        return False
    entry = {
        "key": deliverable_key,
        "topic": meta["topic"],
        "sum": meta["summary"],
        "durability": meta["durability"],
        "ttl_days": meta["ttl_days"],
        "confidence": meta["confidence"],
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }
    lib_key = LIBRARY_KEY.format(agent=agent_name)
    r = _aimeat_call(agent_name, "aimeat_memory_read", {"key": lib_key})
    cur = r.get("value") if isinstance(r, dict) else None
    arr = [e for e in cur if isinstance(e, dict) and e.get("key") != deliverable_key] if isinstance(cur, list) else []
    arr.append(entry)
    arr = arr[-LIBRARY_CAP:]
    _aimeat_call(agent_name, "aimeat_memory_write", {"key": lib_key, "value": arr, "visibility": "owner"})
    return True


def make_librarian_tools(agent_name: str) -> list:
    """Tools for a coordinator/librarian: check the index before doing expensive work."""

    @tool("consult_librarian")
    def consult_librarian(need: str) -> str:
        """Before running expensive work, check whether the fleet has ALREADY produced something
        relevant. `need` = a short description of what you are about to do / want. Returns the best
        existing deliverables (with key, owning crew, topic, shelf-life and a one-line summary) so you
        can reuse instead of redo. If nothing relevant exists, says so — then proceed with the work.
        Note shelf-life: a 'fast' item that is old should be re-verified, not trusted blindly."""
        hits = search_index(agent_name, need, top_k=5)
        if not hits:
            return "Nothing relevant in the index — no prior work to reuse; proceed with the task."
        lines = ["Existing relevant work (reuse if it fits; re-verify 'fast' items if old):"]
        for h in hits:
            lines.append(
                f"- [{h['relevant']}] {h['summary']}  (crew: {h['agent']}, topic: {h['topic']}, "
                f"shelf-life: {h['durability']}, key: {h['key']})"
            )
        return "\n".join(lines)

    return [consult_librarian]

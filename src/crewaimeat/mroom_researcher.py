"""mroom-researcher: the M-ROOM's purpose-built deep researcher for POI research-briefs.

Fulfils `research-request` records (rich, operator-perspective POI briefs) in the MACHINE ROOM and
writes a structured, sourced, bilingual (FI + markdown_en) operator brief into `research-result`.
Own GAII; runs as a scheduled SWEEP (deterministic read of the room every run) so a request is never
stranded by a missed push — the exact gap that left web-researcher's request at 'requested'.

vs the generic web-researcher contract it: (1) DERIVES real search queries from the brief's topic
instead of dumping the 200-word instruction into the search box, (2) grounds on the POI's own signals
in the room + the brief's primary source, and (3) composes the exact structure the brief asks for
(cold machine voice, sourced, FI + EN) rather than a fixed Summary/Key-findings shape.

Two `llm.call()`s per brief (plan queries + compose) — no CrewAI executor loop. Writing to the live
room is gated: DRY-RUN (default) reads + researches + BUILDS the result but writes NOTHING; set
MROOM_RESEARCHER_PUBLISH=1 to actually write + advance the lifecycle.

Run:  uv run python -m crewaimeat.mroom_researcher        # one DRY-RUN sweep, prints what it would write
"""

from __future__ import annotations

import datetime
import json
import os
import sys

from crewaimeat.article_extract import _MIN_CHARS, _trafilatura_text
from crewaimeat.fetch_pipeline import _searxng_urls
from crewaimeat.mroom import _extract_json, _room_read, _room_write

AGENT_NAME = "mroom-researcher"

IN_SPACE = "research-request"  # ns shared.research_requests (in NS)
OUT_SPACE = "research-result"  # ns shared.research_docs (a DOCUMENT space)
_MAX_SOURCES = 6
_SRC_CHARS = 3500
_MAX_ITEMS = 3
_CLAIM_STALE_MIN = 15  # an `in-progress` claim idle this long = a run the fleet/server restart killed -> re-pick-up


def _live() -> bool:
    return (os.getenv("MROOM_RESEARCHER_PUBLISH") or "").strip().lower() in ("1", "true", "yes", "on")


def _is_stale(rec: dict, minutes: int = _CLAIM_STALE_MIN) -> bool:
    """True if the record hasn't moved in `minutes` (server `_updatedAt`) — a claimed run that died mid-way.
    Missing/unparseable timestamp -> treat as stale (re-pick-up; output-existence dedup keeps that safe)."""
    ts = rec.get("_updatedAt") or rec.get("updated_at") or rec.get("claimed_at")
    if not isinstance(ts, str):
        return True
    try:
        t = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return True
    return (datetime.datetime.now(datetime.timezone.utc) - t) > datetime.timedelta(minutes=minutes)


def _requested(room: dict) -> list[dict]:
    """Requests to fulfil: no research-result yet AND either status=='requested' OR a STALE 'in-progress'
    claim — a run a fleet/server restart killed mid-way, which otherwise sits 'in-progress' forever (the
    AIMEAT dev's 'a lost run must never require a human nudge' directive; doc-qld5qo5). OUTPUT-EXISTENCE
    DEDUP stays the primary guard: a request whose res-<id> already exists is treated as fulfilled + skipped,
    so re-claiming a stale in-progress can never double-write."""
    objs = room.get("objects", {}) or {}
    done = {r.get("id") for r in (objs.get(OUT_SPACE) or []) if isinstance(r, dict) and r.get("id")}
    out: list[dict] = []
    for r in objs.get(IN_SPACE) or []:
        if not isinstance(r, dict) or not r.get("id") or f"res-{r['id']}" in done:
            continue
        status = r.get("status")
        if status == "requested" or (status == "in-progress" and _is_stale(r)):
            out.append(r)
    return out


def _poi_signals(room: dict, poi_id: str | None) -> list[dict]:
    if not poi_id:
        return []
    return [s for s in (room.get("objects", {}) or {}).get("signal") or [] if s.get("poi_id") == poi_id][:8]


# --------------------------------------------------------------------------- #
# plan (LLM #1) -> gather (deterministic) -> compose (LLM #2)
# --------------------------------------------------------------------------- #
def _plan(llm, brief: str) -> tuple[list[str], str | None]:
    """Turn the rich brief into 3-5 SHORT search queries + the primary-source URL. One llm.call()."""
    prompt = (
        "You are planning web research for an AIMEAT operator brief. From the brief below, extract two things:\n"
        "- 3-5 SHORT web-search queries (5-9 words each) that will surface the primary source + recent developments\n"
        "- the single most authoritative primary-source URL the brief names or implies (or null)\n\n"
        f"BRIEF:\n{brief}\n\n"
        'Return STRICT JSON only: {"queries": ["...", "..."], "primary_url": "https://..." | null}'
    )
    try:
        obj = _extract_json(str(llm.call([{"role": "user", "content": prompt}]) or "")) or {}
    except Exception as exc:  # noqa: BLE001
        print(f"[{AGENT_NAME}] plan call failed: {exc!r}", file=sys.stderr)
        obj = {}
    queries = [q for q in (obj.get("queries") or []) if isinstance(q, str) and q.strip()][:5]
    primary = obj.get("primary_url") if isinstance(obj.get("primary_url"), str) else None
    return (queries or [brief[:80]]), primary


def _gather(queries: list[str], primary_url: str | None) -> list[dict]:
    """Deterministic (NO LLM): fetch the primary source + top domain-diverse results, extract main text."""
    urls: list[str] = []
    if primary_url and primary_url.startswith("http"):
        urls.append(primary_url)
    for q in queries:
        urls.extend(_searxng_urls(q, "en", "month", n=4) or [])
    docs: list[dict] = []
    seen: set[str] = set()
    seen_domains: set[str] = set()
    for u in urls:
        if not isinstance(u, str) or not u.startswith("http") or u in seen:
            continue
        seen.add(u)
        dom = u.split("/")[2] if "/" in u[8:] else u
        if dom in seen_domains and len(docs) >= 2:  # keep some diversity once we have a couple
            continue
        if len(docs) >= _MAX_SOURCES:
            break
        try:
            txt = _trafilatura_text(u)
        except Exception:  # noqa: BLE001
            txt = ""
        if txt and len(txt) >= _MIN_CHARS:
            docs.append({"url": u, "text": txt[:_SRC_CHARS]})
            seen_domains.add(dom)
    return docs


def _compose(llm, brief: str, signals: list[dict], docs: list[dict]) -> dict:
    """Compose the FI + markdown_en operator brief the request asks for, grounded in the sources. One llm.call()."""
    src_block = "\n\n".join(f"[{i + 1}] {d['url']}\n{d['text']}" for i, d in enumerate(docs))
    sig_block = "\n".join(f"- {s.get('headline')} ({s.get('url')})" for s in signals) or "(none)"
    prompt = (
        "You are the M-ROOM machine researcher for AIMEAT. FOLLOW THE BRIEF'S INSTRUCTIONS EXACTLY — obey the "
        "structure and answer the specific questions it asks. Ground EVERY factual claim in the numbered sources "
        "and cite them as [n]. Cold, factual machine voice — state, never sell. If the sources do not answer part "
        "of the brief, say so plainly rather than inventing.\n\n"
        f"BRIEF (the operator's instructions):\n{brief}\n\n"
        f"THIS POI'S RECENT SIGNALS (context, not sources):\n{sig_block}\n\n"
        f"SOURCES (numbered — cite as [n]; use ONLY facts present here):\n{src_block}\n\n"
        'Return STRICT JSON only: {"title": "<=90 chars", '
        '"markdown": "<Finnish: the full brief the operator asked for, ending with a ## Lähteet section that lists '
        'every source URL used>", '
        '"markdown_en": "<the same brief in English, ending with a ## Sources section>"}\n'
        "Output JSON only."
    )
    try:
        return _extract_json(str(llm.call([{"role": "user", "content": prompt}]) or "")) or {}
    except Exception as exc:  # noqa: BLE001
        print(f"[{AGENT_NAME}] compose call failed: {exc!r}", file=sys.stderr)
        return {}


def _advance(req: dict, **changes) -> None:
    """Write+publish the research-request record with a status change (drops server metadata)."""
    rec = {k: v for k, v in {**req, **changes}.items() if not k.startswith("_")}
    _room_write(IN_SPACE, rec["id"], rec, publish=True, agent=AGENT_NAME)


# --------------------------------------------------------------------------- #
# the sweep
# --------------------------------------------------------------------------- #
def run_research(llm, *, dry_run: bool = True, max_items: int = _MAX_ITEMS) -> dict:
    """One sweep: fulfil pending `research-request` briefs. dry_run writes NOTHING to the room."""
    room = _room_read(AGENT_NAME)
    if not room:
        note = "MACHINE ROOM not accessible to mroom-researcher (org membership / write access?) — nothing done."
        print(f"[{AGENT_NAME}] {note}", file=sys.stderr)
        return {"processed": 0, "failed": 0, "note": note, "no_access": True, "dry_run": dry_run}

    reqs = _requested(room)
    if not reqs:
        return {"processed": 0, "failed": 0, "note": "no requested briefs", "dry_run": dry_run}

    processed = failed = 0
    would_write: list[dict] = []
    for req in reqs[:max_items]:
        rid = req["id"]
        brief = req.get("brief") or ""
        if not dry_run:
            _advance(req, status="in-progress")  # CLAIM
        try:
            queries, primary = _plan(llm, brief)
            docs = _gather(queries, primary)
            if not docs:
                if not dry_run:
                    _advance(req, status="failed", error="no usable sources found")
                failed += 1
                print(f"[{AGENT_NAME}] {rid}: no usable sources (queries={queries})", file=sys.stderr)
                continue
            out = _compose(llm, brief, _poi_signals(room, req.get("poi_id")), docs)
            md = out.get("markdown")
            if not md:
                if not dry_run:
                    _advance(req, status="failed", error="composition returned empty")
                failed += 1
                continue
            result = {
                "title": (out.get("title") or f"Brief · {req.get('poi_id') or rid}")[:120],
                "markdown": md,
                "markdown_en": out.get("markdown_en") or "",
            }
            out_id = f"res-{rid}"
            if dry_run:
                would_write.append(
                    {"space": OUT_SPACE, "id": out_id, "sources": [d["url"] for d in docs], "value": result}
                )
                would_write.append({"space": IN_SPACE, "id": rid, "change": "status=done"})
                processed += 1
            else:
                ok, _ = _room_write(OUT_SPACE, out_id, result, publish=True, agent=AGENT_NAME)
                if ok:
                    _advance(req, status="done", result_ref=out_id)
                    processed += 1
                else:
                    _advance(req, status="failed", error="result write failed")
                    failed += 1
                    print(f"[{AGENT_NAME}] result write FAILED for {out_id}", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 — surface, never a silent green
            if not dry_run:
                _advance(req, status="failed", error=repr(exc)[:300])
            failed += 1
            print(f"[{AGENT_NAME}] research FAILED for {rid}: {exc!r}", file=sys.stderr)

    return {
        "processed": processed,
        "failed": failed,
        "note": (
            "DRY RUN — nothing written to the room" if dry_run else f"{processed} brief(s) written, {failed} failed"
        ),
        "dry_run": dry_run,
        **({"would_write": would_write} if dry_run else {}),
    }


def main() -> None:
    from crewaimeat.llm import get_llm

    dry = not _live()
    print(f"[{AGENT_NAME}] {'DRY RUN' if dry else 'LIVE'} — researching pending M-ROOM briefs…", file=sys.stderr)
    s = run_research(get_llm(for_tool_use=False, agent_name=AGENT_NAME), dry_run=dry)
    print(json.dumps(s, ensure_ascii=False, indent=2)[:8000])


if __name__ == "__main__":
    main()

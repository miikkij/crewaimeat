"""The M-ROOM REQuest fleet — the visible workers that turn one guest REQuest/day into an archived trail.

A guest in the machine-room card leaves ONE request per day; it lands as a `request` record (status
`submitted`, created by the M-ROOM intake). Four separate GAIIs — each its own crew so the room roster
honestly shows WHO DID WHAT — walk it through a records-driven lifecycle, each triggering on its inbound status:

    submitted --[intake]--> [mroom-sniffer claims `sniffing`] --> processing --[mroom-digger]--> researched
             --[mroom-scorer]--> scored --[mroom-archivist]--> archived        (`failed` on any stage error)

  * mroom-sniffer   — classify the ask, draft a processing plan into an `outbox` doc, set `processing`.
  * mroom-digger    — the fleet's OWN researcher (the existing `mroom-researcher` stays POI-brief-only):
                      execute the plan with web search, append findings to the outbox, set `researched`.
  * mroom-scorer    — cold eval: SIGNAL VALUE X.X / RETAINED|DISCARDED + one factual line (judges the
                      CONTENT, never the person); append a scorecard, set `scored` (+ verdict/signal_value).
  * mroom-archivist — RETAINED -> write + PUBLISH a bilingual `archive-entry`; DISCARDED -> a light note;
                      set `archived` (+ archive_ref).

The status set (submitted/sniffing/processing/researched/scored/archived/failed) is the M-ROOM node's blessed
enum, so every handoff has a distinct inbound status — the whole chain is records-driven (the node's record-fork
bug is fixed: status writes persist as the member's GHII and the aggregated read is freshest-wins). A light
idempotency guard (skip a request whose next artifact already exists) keeps a catch-up / re-run from
double-writing, and every stage reads the room via the AGGREGATED read (`aimeat_workspace_read`) — never
a raw per-GAII memory GET (an agent 404s on its own key by design after the ownership fix).

HARD privacy invariants: `member` (EXC_VIP_NN, or OPERATOR for operator/test requests) is the ONLY identity a
request carries — by house rule no email/name/phone ever reaches a record. Layered defense anyway: `_advance`
rebuilds the request from ONLY the declared room.request@1 fields (a STRICT schema — anything undeclared is
rejected at publish); `_ask` strips self-identification + emails from the guest's `text` before it reaches a
model; and `_write` email-scrubs EVERY string (titles included) at the boundary. Free-text name redaction is
best-effort (true redaction needs NER), so the LLM prompts also forbid names.

Writing to the live room is gated: DRY-RUN (default) reads + BUILDS every record but writes NOTHING; set
MROOM_REQUESTS_PUBLISH=1 in the fleet env to actually write + advance the lifecycle.

Run one stage sweep (or the whole chain) by hand:
  uv run python -m crewaimeat.mroom_requests sniff     # (research | score | archive | all)
"""

from __future__ import annotations

import json
import os
import re
import sys

from crewaimeat.article_extract import _MIN_CHARS, _trafilatura_text
from crewaimeat.fetch_pipeline import _searxng_urls
from crewaimeat.mroom import _extract_json, _now, _room_read, _room_write

# the four fleet identities (each a separate GAII / crew)
SNIFFER = "mroom-sniffer"
DIGGER = "mroom-digger"  # the fleet's researcher — distinct from the POI-brief `mroom-researcher`
SCORER = "mroom-scorer"
ARCHIVIST = "mroom-archivist"
_AGENTS = (SNIFFER, DIGGER, SCORER, ARCHIVIST)

# spaces (name) -> publish namespace. All three are member-writable + ungated (manifest policy: only
# room.proposal / room.focus are alwaysGate), so the fleet writes + publishes them directly.
REQUEST_SPACE, REQUEST_NS = "request", "room.request"
OUTBOX_SPACE, OUTBOX_NS = "outbox", "room.outbox"
ARCHIVE_SPACE, ARCHIVE_NS = "archive-entry", "room.archive"
_NS = {REQUEST_SPACE: REQUEST_NS, OUTBOX_SPACE: OUTBOX_NS, ARCHIVE_SPACE: ARCHIVE_NS}

# request lifecycle — the blessed enum (M-ROOM app/node). Intake sets `submitted`; each agent triggers on
# its INBOUND status; `sniffing` is the sniffer's CLAIM; terminals are `archived` (success) / `failed` (error).
ST_SUBMITTED, ST_SNIFFING, ST_PROCESSING, ST_RESEARCHED, ST_SCORED, ST_ARCHIVED, ST_FAILED = (
    "submitted",
    "sniffing",
    "processing",
    "researched",
    "scored",
    "archived",
    "failed",
)

_MAX_PER_RUN = 3  # 1 request/day is expected; a small cap keeps a bad sweep bounded
_MAX_SOURCES = 5
_SRC_CHARS = 3000
_SCORE_RETAIN = 5.0  # signal_value >= this -> RETAINED (used when the model omits a clean verdict)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_POIS = ("POI_002", "POI_004", "POI_006")
# room.request@1 is a STRICT schema (undeclared fields are REJECTED at publish), so the write-back set must
# be EXACTLY its declared fields. Intake sets id/member/text/status/submitted_at (+ optional lang, poi_id);
# the fleet fills the rest as the request advances. `member` is the ONLY identity (EXC_VIP_NN, or OPERATOR for
# operator/test requests); by house rule NO email/name/phone ever reaches a record, so there is no PII to
# strip here — only the discipline of writing nothing undeclared. NEVER lock/re-lock this schema from here.
_SAFE_REQUEST_KEYS = frozenset(
    {
        "id",
        "member",
        "text",
        "status",
        "submitted_at",
        "lang",
        "poi_id",
        "plan",
        "verdict",
        "reason",
        "signal_value",
        "refs",
        "outbox_ref",
        "archive_ref",
        "assigned_to",
        "started_at",
        "researched_at",
        "scored_at",
        "archived_at",
        "error",
        "notes",
        "attempts",
        "updated_at",
    }
)
# best-effort neutralization of a guest self-identifying in free text ("I'm <Name>", "regards, <Name>", …).
# NOT a substitute for NER: a bare name with no cue can still pass, which is why the LLM prompts also forbid
# names and _member owns the handle. The cue is case-insensitive; the captured name must be Capitalized.
_SELF_ID_RE = re.compile(
    r"(?:(?i:i['’]?m|i am|my name is|this is|name['’]?s|regards|sincerely|yours|signed))"
    r"[\s,:.\-]+([A-ZÅÄÖ][A-Za-zÅÄÖåäö'’.\-]*(?:\s+[A-ZÅÄÖ][A-Za-zÅÄÖåäö'’.\-]*){0,2})"
)
_HDR_FINDINGS = "## Findings"  # digger's outbox section (EN header) — also its idempotency marker
_HDR_SCORECARD = "## Scorecard"  # scorer's outbox section — also its idempotency marker


# --------------------------------------------------------------------------- #
# gating + privacy
# --------------------------------------------------------------------------- #
def _live() -> bool:
    """True only when the operator opted in to real room writes (MROOM_REQUESTS_PUBLISH=1)."""
    return (os.getenv("MROOM_REQUESTS_PUBLISH") or "").strip().lower() in ("1", "true", "yes", "on")


def _scrub(text: str) -> str:
    """Redact any email-like token — the last line of defense for 'never write a real identity'."""
    return _EMAIL_RE.sub("[redacted]", str(text or ""))


def _deep_scrub(v):
    """Email-scrub every string in an arbitrary JSON value (dict/list/str) — used at the write boundary."""
    if isinstance(v, str):
        return _scrub(v)
    if isinstance(v, list):
        return [_deep_scrub(x) for x in v]
    if isinstance(v, dict):
        return {k: _deep_scrub(x) for k, x in v.items()}
    return v


def _strip_self_id(text: str) -> str:
    """Best-effort redaction of a guest self-identifying in free text ('I'm <Name>', 'regards, <Name>'):
    keep the cue, replace the captured name. Layered with _scrub (emails) and the LLM's own 'never write a
    name' instruction; a bare name with no lead-in cue can still pass (true redaction needs NER)."""
    return _SELF_ID_RE.sub(lambda m: m.group(0)[: m.start(1) - m.start(0)] + "[redacted]", str(text or ""))


def _clean_ask(text: str) -> str:
    """The one place ask free-text is sanitized: strip self-identification, then email-scrub."""
    return _scrub(_strip_self_id(str(text or "").strip()))


def _member(req: dict) -> str:
    """The guest's handle — the `member` field, the ONLY identity a request ever carries (EXC_VIP_NN, or
    OPERATOR for operator/test requests). Preserved as intake set it (never rejected); email-scrubbed purely
    as defense (the room guarantees no PII on records)."""
    m = req.get("member")
    return _scrub(m.strip()) if isinstance(m, str) and m.strip() else "EXC_VIP"


def _ask(req: dict) -> str:
    """The guest's ask — always the `text` field — SANITIZED for LLM prompts (self-identification stripped +
    email-scrubbed) so a name a guest typed never reaches a model. The STORED text is preserved as intake set
    it (the room guarantees no PII on records); this sanitization is prompt-side defense only."""
    v = req.get("text")
    return _clean_ask(v) if isinstance(v, str) and v.strip() else ""


# --------------------------------------------------------------------------- #
# room I/O helpers
# --------------------------------------------------------------------------- #
def _requests(room: dict, *statuses: str) -> list[dict]:
    """Requests whose status is one of `statuses` (the calling agent's inbound state[s])."""
    want = set(statuses)
    return [
        r
        for r in (room.get("objects", {}) or {}).get(REQUEST_SPACE) or []
        if isinstance(r, dict) and r.get("status") in want and r.get("id")
    ]


def _doc(room: dict, space: str, doc_id: str) -> dict:
    for d in (room.get("objects", {}) or {}).get(space) or []:
        if isinstance(d, dict) and d.get("id") == doc_id:
            return d
    return {}


def _outbox_id(rid: str) -> str:
    return f"ob-{rid}"


def _archive_id(rid: str) -> str:
    return f"arc-{rid}"


def _write(space: str, rec_id: str, value: dict, *, agent: str) -> bool:
    """The single write choke-point: email-scrub EVERY string in the value (titles included) before it
    reaches the room, then write+publish as `agent`'s GAII (normalized to the owner's GHII by the node)."""
    ok, _ = _room_write(space, rec_id, _deep_scrub(value), publish=True, namespace=_NS[space], agent=agent)
    return ok


def _advance(req: dict, agent: str, **changes) -> bool:
    """Write+publish the request with a status change, STRICT-schema-safe: the record is rebuilt from ONLY the
    declared room.request@1 fields (_SAFE_REQUEST_KEYS) — publishing any undeclared field is rejected by the
    node. The required fields (id/member/text/status/submitted_at) carry through from intake; `member` is
    re-derived to the safe handle, and the stored `text` is email-scrubbed at the _write boundary. Ok on success."""
    merged = {**req, **changes}
    rec = {k: v for k, v in merged.items() if k in _SAFE_REQUEST_KEYS}
    rec["id"] = req["id"]
    rec["member"] = _member(req)
    return _write(REQUEST_SPACE, rec["id"], rec, agent=agent)


def _append_outbox(
    room: dict, oid: str, title: str, fi: str, en: str, *, agent: str, marker: str | None = None
) -> bool:
    """Append a bilingual section to the visible progress doc. IDEMPOTENT: if `marker` (a section header) is
    already present, an earlier (partial) run wrote it — skip the append and report success, so a re-wake
    never DUPLICATES the section. Sequential handoff otherwise. Returns the write's ok (True if skipped)."""
    ob = _doc(room, OUTBOX_SPACE, oid)
    if marker and marker in (ob.get("markdown_en") or ""):
        return True  # section already present — idempotent no-op
    md = ((ob.get("markdown") or "").rstrip() + "\n\n" + fi).strip()
    md_en = ((ob.get("markdown_en") or "").rstrip() + "\n\n" + en).strip()
    return _write(
        OUTBOX_SPACE, oid, {"title": ob.get("title") or title, "markdown": md, "markdown_en": md_en}, agent=agent
    )


# --------------------------------------------------------------------------- #
# pure builders (LLM does judgement only; code owns the envelope) — unit-testable without the room
# --------------------------------------------------------------------------- #
def _plan_ask(llm, ask: str, member: str) -> dict:
    """Classify the ask + draft a research plan. ONE llm.call(). Returns {} on failure (caller fails loud)."""
    prompt = (
        "You are mroom-sniffer, the intake worker of the AIMEAT machine room. A guest left ONE request. "
        "Classify it and draft a short plan for the research worker. Cold, factual machine voice. "
        f"NEVER write any email address or real personal name — the guest is only '{member}'.\n\n"
        f"GUEST REQUEST:\n{ask}\n\n"
        "Return STRICT JSON only: {"
        '"title": "<=80 chars, what this request is about>", '
        '"classification": "<one of: research-question | comparison | build-idea | signal-tip | off-topic>", '
        '"poi_id": "<POI_002 (EU AI Act) | POI_004 (MCP/agent protocols) | POI_006 (HN/arXiv agents) if it '
        'clearly maps, else null>", '
        '"angle": "<1 sentence: the sharpest angle to investigate>", '
        '"queries": ["<3-5 short web-search queries, 5-9 words each>"], '
        '"steps": ["<2-4 plan steps the researcher will follow>"]}'
    )
    try:
        raw = _extract_json(str(llm.call([{"role": "user", "content": prompt}]) or ""))
    except Exception as exc:  # noqa: BLE001
        print(f"[{SNIFFER}] plan call failed: {exc!r}", file=sys.stderr)
        return {}
    obj = raw if isinstance(raw, dict) else {}  # a non-dict reply (list/scalar) is junk, not a crash
    poi = obj.get("poi_id") if obj.get("poi_id") in _POIS else None
    return {
        # NEVER derive a record-visible title from the raw ask (it can carry a name) — neutral fallback only
        "title": (obj.get("title") or "Guest request").strip()[:100],
        "classification": (obj.get("classification") or "research-question").strip()[:40],
        "poi_id": poi,
        "angle": (obj.get("angle") or "").strip()[:400],
        "queries": [q.strip() for q in (obj.get("queries") or []) if isinstance(q, str) and q.strip()][:5],
        "steps": [s.strip() for s in (obj.get("steps") or []) if isinstance(s, str) and s.strip()][:4],
    }


def _plan_section(plan: dict) -> tuple[str, str]:
    """Render the plan deterministically into a bilingual outbox section (no LLM)."""
    steps = plan.get("steps") or []
    poi = plan.get("poi_id") or "-"
    fi = (
        f"## Suunnitelma\n- Luokitus: {plan.get('classification')}\n- POI: {poi}\n"
        f"- Näkökulma: {plan.get('angle') or '-'}\n"
        + ("- Vaiheet:\n" + "\n".join(f"  {i + 1}. {s}" for i, s in enumerate(steps)) if steps else "")
    ).rstrip()
    en = (
        f"## Plan\n- Classification: {plan.get('classification')}\n- POI: {poi}\n"
        f"- Angle: {plan.get('angle') or '-'}\n"
        + ("- Steps:\n" + "\n".join(f"  {i + 1}. {s}" for i, s in enumerate(steps)) if steps else "")
    ).rstrip()
    return fi, en


def _gather(queries: list[str]) -> list[dict]:
    """Deterministic (NO LLM): fetch domain-diverse results for the plan's queries, extract main text."""
    urls: list[str] = []
    for q in queries or []:
        urls.extend(_searxng_urls(q, "en", "month", n=4) or [])
    docs: list[dict] = []
    seen: set[str] = set()
    seen_domains: set[str] = set()
    for u in urls:
        if not isinstance(u, str) or not u.startswith("http") or u in seen:
            continue
        seen.add(u)
        dom = u.split("/")[2] if "/" in u[8:] else u
        if dom in seen_domains and len(docs) >= 2:
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


def _compose_findings(llm, ask: str, member: str, angle: str, docs: list[dict]) -> dict:
    """Compose bilingual research findings grounded in the sources. ONE llm.call(). {} on failure."""
    src_block = "\n\n".join(f"[{i + 1}] {d['url']}\n{d['text']}" for i, d in enumerate(docs))
    prompt = (
        "You are mroom-digger, the research worker of the AIMEAT machine room. Execute the plan for a "
        f"guest ('{member}') request using ONLY the numbered sources; cite them as [n]. Cold, factual machine "
        "voice — state, never sell. If the sources do not answer part of the ask, say so plainly rather than "
        f"inventing. NEVER write any email or real personal name — the guest is only '{member}'.\n\n"
        f"REQUEST:\n{ask}\n\nANGLE: {angle or '(none given)'}\n\n"
        f"SOURCES (cite as [n]; use ONLY facts present here):\n{src_block}\n\n"
        'Return STRICT JSON only: {"fi": "<Finnish findings, markdown, ending with a ## Lähteet list of the '
        'source URLs used>", "en": "<the same findings in English, ending with a ## Sources list>"}'
    )
    try:
        raw = _extract_json(str(llm.call([{"role": "user", "content": prompt}]) or ""))
    except Exception as exc:  # noqa: BLE001
        print(f"[{DIGGER}] findings call failed: {exc!r}", file=sys.stderr)
        return {}
    obj = raw if isinstance(raw, dict) else {}
    return {"fi": _scrub(str(obj.get("fi") or "")).strip(), "en": _scrub(str(obj.get("en") or "")).strip()}


def _score(llm, ask: str, member: str, findings: str) -> dict:
    """Cold SIGNAL VALUE score of what the research produced. ONE llm.call(). {} on failure."""
    prompt = (
        "You are mroom-scorer, the cold evaluator of the AIMEAT machine room. Score the SIGNAL VALUE of what "
        "the research produced FOR THE HOUSE, 0.0 (no signal) to 10.0 (act on it today). Judge the CONTENT, "
        "never the person: a discard is 'the request produced no signal', never an insult. NEVER write any "
        f"email or real personal name — the guest is only '{member}'.\n\n"
        f"REQUEST:\n{ask}\n\nRESEARCH FINDINGS:\n{findings}\n\n"
        'Return STRICT JSON only: {"signal_value": <number 0-10>, "verdict": "RETAINED"|"DISCARDED", '
        '"line": "<ONE factual sentence: the signal it produced, or why there is none>"}'
    )
    try:
        raw = _extract_json(str(llm.call([{"role": "user", "content": prompt}]) or ""))
    except Exception as exc:  # noqa: BLE001
        print(f"[{SCORER}] score call failed: {exc!r}", file=sys.stderr)
        return {}
    obj = raw if isinstance(raw, dict) else {}
    try:
        sv = round(max(0.0, min(10.0, float(obj.get("signal_value")))), 1)  # round FIRST
    except (TypeError, ValueError):
        return {}
    # derive the fallback verdict from the SAME rounded value we store/display, so '5.0 — DISCARDED' can't occur
    verdict = obj.get("verdict") if obj.get("verdict") in ("RETAINED", "DISCARDED") else None
    verdict = verdict or ("RETAINED" if sv >= _SCORE_RETAIN else "DISCARDED")
    return {"signal_value": sv, "verdict": verdict, "line": _scrub(str(obj.get("line") or "")).strip()[:400]}


def _score_section(score: dict) -> tuple[str, str]:
    """The scorecard line, identical both languages (spec: `SIGNAL VALUE: X.X — RETAINED/DISCARDED`)."""
    head = (
        f"{_HDR_SCORECARD}\nSIGNAL VALUE: {score['signal_value']:.1f} — {score['verdict']}\n{score.get('line') or ''}"
    )
    return head.rstrip(), head.rstrip()


_SCORECARD_RE = re.compile(r"SIGNAL VALUE:\s*([0-9]+(?:\.[0-9]+)?)\s*[—\-]\s*(RETAINED|DISCARDED)")


def _parse_scorecard(md: str) -> dict:
    """Recover {signal_value, verdict} from an already-written scorecard section — used to self-heal a
    request whose scorecard landed but whose status advance did not, without re-scoring (deterministic)."""
    m = _SCORECARD_RE.search(md or "")
    return {"signal_value": round(float(m.group(1)), 1), "verdict": m.group(2)} if m else {}


def _parties(member: str) -> str:
    return f"{member} + agents (mroom-sniffer, mroom-digger, mroom-scorer, mroom-archivist)"


def _archive_title(rid: str, short: str) -> str:
    """House style: `REQ <request-id> // <short title>` — the ARC-NNN series belongs to the operator, not us."""
    return f"REQ {rid} // {(short or 'guest request').strip()}"[:120]


def _compose_archive(llm, rid: str, ask: str, member: str, score: dict, trail: str) -> dict:
    """Compose the bilingual RETAINED archive-entry (the permanent trail). ONE llm.call(). {} on failure."""
    prompt = (
        "You are mroom-archivist. Write the permanent ARCHIVE ENTRY for a RETAINED guest request. Bilingual, "
        "cold machine voice. START with a fenced code block holding four lines — PATH / DECISION / STATUS / "
        f"PARTIES (parties = exactly '{_parties(member)}'). Then a short body: what was asked, what the research "
        "produced, the score, any follow-ups. End with a ## Lähteet / ## Sources list of clickable source URLs. "
        f"NEVER write any email or real personal name — the guest is ONLY '{member}'.\n\n"
        f"REQUEST:\n{ask}\n\nSCORE: {score['signal_value']:.1f} — RETAINED\nSCORE LINE: {score.get('line')}\n\n"
        f"RESEARCH TRAIL:\n{trail}\n\n"
        'Return STRICT JSON only: {"title": "<=70 chars, short>", "markdown": "<Finnish>", "markdown_en": "<English>"}'
    )
    try:
        raw = _extract_json(str(llm.call([{"role": "user", "content": prompt}]) or ""))
    except Exception as exc:  # noqa: BLE001
        print(f"[{ARCHIVIST}] archive call failed: {exc!r}", file=sys.stderr)
        return {}
    obj = raw if isinstance(raw, dict) else {}
    md, md_en = str(obj.get("markdown") or "").strip(), str(obj.get("markdown_en") or "").strip()
    if not md and not md_en:
        return {}
    return {
        "title": _archive_title(rid, obj.get("title") or ""),
        "markdown": _scrub(md or md_en),
        "markdown_en": _scrub(md_en or md),
    }


def _discard_note(rid: str, member: str, score: dict) -> dict:
    """A light, deterministic archive note for a DISCARDED request (no LLM) — a discard still gets a trail."""
    line = score.get("line") or "The request produced no signal."
    block = f"```\nPATH:     request {rid}\nDECISION: DISCARDED — no signal\nSTATUS:   CLOSED\nPARTIES:  {_parties(member)}\n```"
    return {
        "title": _archive_title(rid, "DISCARDED — no signal"),
        "markdown": _scrub(
            f"{block}\n\n**Tulos:** pyyntö ei tuottanut signaalia. SIGNAL VALUE: {score['signal_value']:.1f}.\n\n{line}"
        ),
        "markdown_en": _scrub(
            f"{block}\n\n**Result:** the request produced no signal. SIGNAL VALUE: {score['signal_value']:.1f}.\n\n{line}"
        ),
    }


# --------------------------------------------------------------------------- #
# stage runners (read room -> select by status -> build -> write) — one per fleet member
# --------------------------------------------------------------------------- #
def _get_llm(llm, agent: str):
    if llm is not None:
        return llm
    from crewaimeat.llm import get_llm

    return get_llm(for_tool_use=False, agent_name=agent)


def _no_access(agent: str, dry_run: bool) -> dict:
    note = f"MACHINE ROOM not accessible to {agent} (membership / write access?) — nothing done."
    print(f"[{agent}] {note}", file=sys.stderr)
    return {"processed": 0, "failed": 0, "note": note, "no_access": True, "dry_run": dry_run}


def run_sniff(llm=None, *, dry_run: bool = True, max_items: int = _MAX_PER_RUN) -> dict:
    """`submitted` -> claim (`sniffing`) -> classify + plan -> outbox doc + `processing`. Honors every write and
    self-heals a request whose outbox landed but whose advance did not (re-advance rather than strand it)."""
    room = _room_read(SNIFFER)
    if not room:
        return _no_access(SNIFFER, dry_run)
    reqs = _requests(room, ST_SUBMITTED, ST_SNIFFING)  # inbox: fresh + already-claimed-but-incomplete
    processed = failed = 0
    would: list[dict] = []
    llm = _get_llm(llm, SNIFFER) if reqs else llm
    for req in reqs[:max_items]:
        rid, oid = req["id"], _outbox_id(req["id"])
        try:
            if _doc(room, OUTBOX_SPACE, oid):  # outbox already written; advance stuck -> self-heal
                if dry_run or _advance(req, SNIFFER, status=ST_PROCESSING, outbox_ref=oid, poi_id=req.get("poi_id")):
                    processed += 1
                else:
                    failed += 1
                    print(f"[{SNIFFER}] {rid}: re-advance failed (outbox exists)", file=sys.stderr)
                continue
            ask, member = _ask(req), _member(req)
            if not ask:
                failed += 1
                if not dry_run:
                    _advance(req, SNIFFER, status=ST_FAILED, error="request has no readable text")
                print(f"[{SNIFFER}] {rid}: no readable ask text — marked failed", file=sys.stderr)
                continue
            if not dry_run and req.get("status") == ST_SUBMITTED:  # CLAIM (shows motion + a soft lock)
                started = _now()
                if _advance(req, SNIFFER, status=ST_SNIFFING, started_at=started):
                    req = {**req, "status": ST_SNIFFING, "started_at": started}  # reflect so later writes keep it
            plan = _plan_ask(llm, ask, member)
            if not plan.get("queries"):
                failed += 1
                if not dry_run:
                    _advance(req, SNIFFER, status=ST_FAILED, error="planning produced no research queries")
                continue
            fi, en = _plan_section(plan)
            if dry_run:
                would.append({"outbox": oid, "plan": plan})
                processed += 1
                continue
            title = f"REQ {rid} // {plan['title']}"[:120]
            if not _write(OUTBOX_SPACE, oid, {"title": title, "markdown": fi, "markdown_en": en}, agent=SNIFFER):
                failed += 1
                print(f"[{SNIFFER}] {rid}: outbox write failed — not advancing", file=sys.stderr)
                continue
            if _advance(
                req,
                SNIFFER,
                status=ST_PROCESSING,
                outbox_ref=oid,
                plan=plan,
                poi_id=plan.get("poi_id") or req.get("poi_id"),
            ):
                processed += 1
            else:
                failed += 1
                print(f"[{SNIFFER}] {rid}: advance failed (outbox written; self-heals next wake)", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 — one bad request must not abort the sweep or crash on_record
            failed += 1
            if not dry_run:
                _advance(req, SNIFFER, status=ST_FAILED, error=repr(exc)[:300])
            print(f"[{SNIFFER}] {rid}: FAILED {exc!r}", file=sys.stderr)
    return _summary(SNIFFER, processed, failed, dry_run, would)


def run_research(llm=None, *, dry_run: bool = True, max_items: int = _MAX_PER_RUN) -> dict:
    """`processing` -> execute the plan (web) -> append findings to the outbox + `researched`. Idempotent:
    if the findings section already exists (a prior partial run) it re-advances without re-appending."""
    room = _room_read(DIGGER)
    if not room:
        return _no_access(DIGGER, dry_run)
    reqs = _requests(room, ST_PROCESSING)
    processed = failed = 0
    would: list[dict] = []
    llm = _get_llm(llm, DIGGER) if reqs else llm
    for req in reqs[:max_items]:
        rid = req["id"]
        oid = req.get("outbox_ref") or _outbox_id(rid)
        try:
            if _HDR_FINDINGS in (_doc(room, OUTBOX_SPACE, oid).get("markdown_en") or ""):  # findings already written
                if dry_run or _advance(req, DIGGER, status=ST_RESEARCHED, researched_at=_now()):
                    processed += 1
                else:
                    failed += 1
                    print(f"[{DIGGER}] {rid}: re-advance failed (findings exist, still processing)", file=sys.stderr)
                continue
            plan = req.get("plan") or {}
            ask, member = _ask(req), _member(req)
            queries = [q for q in (plan.get("queries") or []) if isinstance(q, str)] or ([ask[:80]] if ask else [])
            docs = _gather(queries)
            if not docs:
                failed += 1
                if not dry_run:
                    _advance(req, DIGGER, status=ST_FAILED, error="no usable sources found")
                print(f"[{DIGGER}] {rid}: no usable sources (queries={queries})", file=sys.stderr)
                continue
            findings = _compose_findings(llm, ask, member, plan.get("angle") or "", docs)
            if not (findings.get("en") or findings.get("fi")):
                failed += 1
                if not dry_run:
                    _advance(req, DIGGER, status=ST_FAILED, error="research composition returned empty")
                continue
            if dry_run:
                would.append({"outbox": oid, "sources": [d["url"] for d in docs], "findings": findings})
                processed += 1
                continue
            if not _append_outbox(
                room,
                oid,
                plan.get("title") or "Guest request",
                f"## Löydökset\n{findings['fi']}",
                f"{_HDR_FINDINGS}\n{findings['en']}",
                agent=DIGGER,
                marker=_HDR_FINDINGS,
            ):
                failed += 1
                print(f"[{DIGGER}] {rid}: outbox append failed — not advancing", file=sys.stderr)
                continue
            if _advance(req, DIGGER, status=ST_RESEARCHED, researched_at=_now()):
                processed += 1
            else:
                failed += 1
                print(f"[{DIGGER}] {rid}: advance failed (findings written; self-heals next wake)", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            if not dry_run:
                _advance(req, DIGGER, status=ST_FAILED, error=repr(exc)[:300])
            print(f"[{DIGGER}] {rid}: FAILED {exc!r}", file=sys.stderr)
    return _summary(DIGGER, processed, failed, dry_run, would)


def run_score(llm=None, *, dry_run: bool = True, max_items: int = _MAX_PER_RUN) -> dict:
    """`researched` -> cold SIGNAL VALUE + verdict -> append scorecard + `scored`. Idempotent: if a scorecard
    already exists it re-advances using the PARSED score (never re-scores, so the verdict can't drift)."""
    room = _room_read(SCORER)
    if not room:
        return _no_access(SCORER, dry_run)
    reqs = _requests(room, ST_RESEARCHED)
    processed = failed = 0
    would: list[dict] = []
    llm = _get_llm(llm, SCORER) if reqs else llm
    for req in reqs[:max_items]:
        rid = req["id"]
        oid = req.get("outbox_ref") or _outbox_id(rid)
        try:
            ob_en = _doc(room, OUTBOX_SPACE, oid).get("markdown_en") or ""
            if _HDR_SCORECARD in ob_en:  # scorecard already written; status stuck at researched -> self-heal
                sc = _parse_scorecard(ob_en)
                if (
                    dry_run
                    or sc
                    and _advance(
                        req,
                        SCORER,
                        status=ST_SCORED,
                        verdict=sc["verdict"],
                        signal_value=sc["signal_value"],
                        reason=req.get("reason") or "",
                        scored_at=_now(),
                        outbox_ref=oid,
                    )
                ):
                    processed += 1
                else:
                    failed += 1
                    print(f"[{SCORER}] {rid}: scorecard present but re-advance failed", file=sys.stderr)
                continue
            ask, member = _ask(req), _member(req)
            trail = ob_en or _doc(room, OUTBOX_SPACE, oid).get("markdown") or ""
            score = _score(llm, ask, member, trail)
            if not score:
                failed += 1
                if not dry_run:
                    _advance(req, SCORER, status=ST_FAILED, error="scoring returned no usable verdict")
                continue
            fi, en = _score_section(score)
            if dry_run:
                would.append({"request": rid, "score": score})
                processed += 1
                continue
            if not _append_outbox(room, oid, "Guest request", fi, en, agent=SCORER, marker=_HDR_SCORECARD):
                failed += 1
                print(f"[{SCORER}] {rid}: scorecard append failed — not advancing", file=sys.stderr)
                continue
            if _advance(
                req,
                SCORER,
                status=ST_SCORED,
                verdict=score["verdict"],
                signal_value=score["signal_value"],
                reason=score["line"],
                scored_at=_now(),
                outbox_ref=oid,
            ):
                processed += 1
            else:
                failed += 1
                print(f"[{SCORER}] {rid}: advance failed (scorecard written; self-heals next wake)", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            if not dry_run:
                _advance(req, SCORER, status=ST_FAILED, error=repr(exc)[:300])
            print(f"[{SCORER}] {rid}: FAILED {exc!r}", file=sys.stderr)
    return _summary(SCORER, processed, failed, dry_run, would)


def run_archive(llm=None, *, dry_run: bool = True, max_items: int = _MAX_PER_RUN) -> dict:
    """`scored` -> RETAINED: publish a bilingual archive-entry; DISCARDED: a light note -> `archived`. Writes
    the archive-entry BEFORE closing the request, so a failed archive write can never leave `archived` with
    no trail (it retries on the next wake instead)."""
    room = _room_read(ARCHIVIST)
    if not room:
        return _no_access(ARCHIVIST, dry_run)
    reqs = _requests(room, ST_SCORED)
    processed = failed = 0
    would: list[dict] = []
    for req in reqs[:max_items]:
        rid, aid = req["id"], _archive_id(req["id"])
        try:
            if _doc(room, ARCHIVE_SPACE, aid):  # archive already written; ensure the request is closed
                if dry_run or _advance(req, ARCHIVIST, status=ST_ARCHIVED, archive_ref=aid, archived_at=_now()):
                    processed += 1
                else:
                    failed += 1
                    print(f"[{ARCHIVIST}] {rid}: re-advance failed (archive exists, still scored)", file=sys.stderr)
                continue
            ask, member = _ask(req), _member(req)
            score = {
                "signal_value": float(req.get("signal_value") or 0.0),
                "verdict": req.get("verdict") or "DISCARDED",
                "line": req.get("reason") or "",
            }
            if score["verdict"] == "RETAINED":
                trail = _doc(room, OUTBOX_SPACE, req.get("outbox_ref") or _outbox_id(rid)).get("markdown_en") or ""
                entry = _compose_archive(_get_llm(llm, ARCHIVIST), rid, ask, member, score, trail)
                if not entry:
                    failed += 1
                    if not dry_run:
                        _advance(req, ARCHIVIST, status=ST_FAILED, error="archive composition returned empty")
                    continue
            else:
                entry = _discard_note(rid, member, score)
            if dry_run:
                would.append({"archive": aid, "verdict": score["verdict"], "entry": entry})
                processed += 1
                continue
            if not _write(ARCHIVE_SPACE, aid, entry, agent=ARCHIVIST):  # trail BEFORE closing -> never a phantom close
                failed += 1
                print(f"[{ARCHIVIST}] {rid}: archive write failed — not advancing (retries next wake)", file=sys.stderr)
                continue
            if _advance(req, ARCHIVIST, status=ST_ARCHIVED, archive_ref=aid, archived_at=_now()):
                processed += 1
            else:
                failed += 1
                print(f"[{ARCHIVIST}] {rid}: advance failed (archive written; self-heals next wake)", file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            if not dry_run:
                _advance(req, ARCHIVIST, status=ST_FAILED, error=repr(exc)[:300])
            print(f"[{ARCHIVIST}] {rid}: FAILED {exc!r}", file=sys.stderr)
    return _summary(ARCHIVIST, processed, failed, dry_run, would)


def run_all(llm=None, *, dry_run: bool = True) -> dict:
    """Run every stage once, in order — the whole chain advances one step per pending request."""
    return {
        "sniff": run_sniff(llm, dry_run=dry_run),
        "research": run_research(llm, dry_run=dry_run),
        "score": run_score(llm, dry_run=dry_run),
        "archive": run_archive(llm, dry_run=dry_run),
    }


def _summary(agent: str, processed: int, failed: int, dry_run: bool, would: list[dict]) -> dict:
    note = "DRY RUN — nothing written to the room" if dry_run else f"{processed} advanced, {failed} failed"
    out = {"agent": agent, "processed": processed, "failed": failed, "dry_run": dry_run, "note": note}
    if dry_run and would:
        out["would_write"] = would
    return out


def stage_report_msg(label: str, s: dict) -> str:
    """One-line human summary of a stage result — used by each crew's build_domain reporter."""
    prefix = "(dry run) " if s.get("dry_run") else ""
    return (
        f"M-ROOM {label} {prefix}complete: {s.get('processed', 0)} advanced, "
        f"{s.get('failed', 0)} failed. {s.get('note', '')}"
    ).strip()


def report_crew(llm, msg: str):
    """A trivial one-line reporter crew stating `msg` — the deterministic stage work is already done in
    code (no LLM in the actual chain work; this reporter just voices the outcome for the task surface)."""
    from crewai import Agent, Task

    reporter = Agent(
        role="M-ROOM Fleet Reporter",
        goal="State the stage outcome exactly.",
        backstory="You report one M-ROOM REQuest-fleet stage result in a single line.",
        llm=llm,
        allow_delegation=False,
        verbose=False,
    )
    task = Task(description=f"State exactly this and nothing else: {msg}", expected_output=msg, agent=reporter)
    return ([reporter], [task])


_STAGES = {"sniff": run_sniff, "research": run_research, "score": run_score, "archive": run_archive, "all": run_all}


def main() -> None:
    stage = (sys.argv[1] if len(sys.argv) > 1 else "all").strip().lower()
    fn = _STAGES.get(stage)
    if not fn:
        print(f"unknown stage {stage!r} — one of {list(_STAGES)}", file=sys.stderr)
        raise SystemExit(2)
    dry = not _live()
    print(f"[mroom-requests] stage={stage} {'DRY RUN' if dry else 'LIVE'}", file=sys.stderr)
    print(json.dumps(fn(dry_run=dry), ensure_ascii=False, indent=2, default=str)[:8000])


if __name__ == "__main__":
    main()

"""mroom-curator: the agentic research curator for the M-ROOM (MACHINE ROOM organism).

The zero-token feed sweeps (extension `mroom` + node schedules) drop raw hits into public
`ext:mroom` memory keys. This is the piece that READS and JUDGES them: it opens each hit, weighs
it against the operator's criteria, and writes a verdict as a `signal` record into the live
MACHINE ROOM workspace. The strongest accepts become an `insight` document + a `proposal` record —
left as DRAFTS, never published ("the machine proposes, the operator decides").

Shape (batch-first ReAct, agreed with the operator + node side) — NOT one-shot prompt→JSON:
  read → act → check → correct, once per run, over the whole batch:
  1. deterministic outer loop (NO LLM): read the 3 feed keys, drop already-verdicted URLs
     (cursor), cap ~15/run, fetch + extract each URL's main text.
  2. ONE batched judgement over the whole list against the criteria → accepted[]/rejected[]
     with reason / idea / aimeat_relation in a single structured response.
  3. check: any item the judge flagged AMBIGUOUS gets at most 1-2 extra fetch+judge iterations
     (one web lookup each), never a per-link chat loop.
  4. write signals (publish `room.signal`), draft the rare strong accept (no publish), update the
     cursor, and write a public run-summary the room's agents panel reads.

Hard invariants: no emails / real identities anywhere; cold machine voice, English; publish ONLY
`room.signal`; `insight` + `proposal` stay drafts. Writing to the MACHINE ROOM is gated: dry-run
(the default) touches the live room with ZERO writes — it fetches, judges and BUILDS the records so
you can eyeball them; set MROOM_CURATOR_PUBLISH=1 to actually write + publish.

Register + run:
  npx aimeat@latest connect add --agent mroom-curator --mode task-runner --url https://aimeat.io --owner <aimeat-account>
  uv run python -m crewaimeat.mroom        # one DRY-RUN pass, prints the summary + would-write records
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import sys
from urllib.parse import quote

from crewaimeat.aimeat_crew import _aimeat_call
from crewaimeat.article_extract import _trafilatura_text

AGENT_NAME = "mroom-curator"

# ── the MACHINE ROOM (a DIFFERENT organism than crewaimeat — same owner: happydude500001) ──
ROOM_ORG = "e8617051-6963-44ea-b1d1-f4c41b4fd0ab"
ROOM_WS = "ws-mr48730nq0b"

# space -> publish namespace (from the workspace manifest). Only room.signal is ever published by
# the agent; insight/proposal are written as drafts (room.proposal is force-gated by room policy).
NS = {
    "signal": "room.signal",
    "insight": "room.insight",
    "proposal": "room.proposal",
    # research-contract spaces (adopted from web-researcher) keep their shared.* namespaces
    "research-request": "shared.research_requests",
    "research-result": "shared.research_docs",
}

# Feed source -> (public ext:mroom key, POI it belongs to). Fixed mapping (node side confirmed):
# arxiv_hn→POI_006 (HN_ARXIV_CSAI), mcp.releases→POI_004 (MCP_AGENT_PROTOCOLS), aiact→POI_002 (EU_AI_ACT).
FEEDS = {
    "mroom.hits.arxiv_hn": "POI_006",
    "mroom.mcp.releases": "POI_004",
    "mroom.aiact.updates": "POI_002",
}
_FEED_OWNER = "ext:mroom"

# Criteria: the per-POI `criteria` records in the room are PRIMARY (read with the workspace read);
# this memory key is the bootstrap/global context (accept_if / reject_if / tone). It lives under the
# CALLING AGENT's GAII on the node (MCP writes land on the GAII, not the owner GHII), hence this exact
# address — do NOT "simplify" it to the GHII form or it 404s.
_CRITERIA_GAII = "claude-desktop-home-mcp#happydude500001@aimeat-finland-001-genesis"
_CRITERIA_KEY = "mroom.curator.criteria"

# Own memory: the cursor (already-verdicted URLs, capped) and the public run summary.
_CURSOR_KEY = "mroom.curator.cursor"
_LASTRUN_KEY = "mroom.curator.lastrun"
_CURSOR_CAP = 300
_BATCH_CAP = 15  # hits judged per run
_TEXT_CHARS = 1800  # extracted text handed to the judge per item
_RECONSIDER_CAP = 2  # max ambiguous items that earn an extra web lookup
_AIMEAT_RELATIONS = ("competitor-compare", "adopt-capability", "foundation-shift", "community-pulse", "regulation")


# --------------------------------------------------------------------------- #
# helpers: identity, public reads, workspace writes
# --------------------------------------------------------------------------- #
def _live() -> bool:
    """True only when the operator has opted in to real writes (MROOM_CURATOR_PUBLISH=1)."""
    return (os.getenv("MROOM_CURATOR_PUBLISH") or "").strip().lower() in ("1", "true", "yes", "on")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _node(agent: str = AGENT_NAME) -> tuple[str, str]:
    """(token, base_url) for `agent` — REST fallback + public reads use it."""
    from crewaimeat.generator_tool import _discover_owner, _token

    return _token(agent, _discover_owner(agent))


def _public_memory(gaii: str, key: str) -> object | None:
    """Read another owner's PUBLIC memory entry by GAII + key. In-fleet this rides the connector's
    public-read tool (the node's own {value:...} shape); otherwise it falls back to authed REST,
    tolerating a couple of response shapes. Returns the value (any JSON type), or None on a real
    failure (distinct from an entry whose value is an empty list)."""
    # 1) connector tool — works once the agent is attached to the serve daemon (i.e. running in-fleet)
    r = _aimeat_call(AGENT_NAME, "aimeat_memory_read_public", {"gaii": gaii, "key": key}, quiet=True)
    if isinstance(r, dict) and r.get("value") is not None:
        return r["value"]
    # 2) authed REST fallback (the node's public-read route)
    import requests

    try:
        tok, url = _node()
        resp = requests.get(
            f"{url.rstrip('/')}/v1/memory/{quote(gaii, safe='')}/{quote(key, safe='')}",
            headers={"Authorization": f"Bearer {tok}"},
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[{AGENT_NAME}] public read {gaii}/{key} failed: {exc!r}", file=sys.stderr)
        return None
    if resp.status_code != 200:
        print(f"[{AGENT_NAME}] public read {gaii}/{key} -> HTTP {resp.status_code}: {resp.text[:160]}", file=sys.stderr)
        return None
    try:
        body = resp.json()
    except ValueError:
        return None
    if isinstance(body, dict):
        if "value" in body:
            return body["value"]
        inner = body.get("entry") or body.get("data")
        if isinstance(inner, dict) and "value" in inner:
            return inner["value"]
        return None
    return body if isinstance(body, list) else None


def _room_read(agent: str = AGENT_NAME) -> dict:
    """Read the MACHINE ROOM workspace (manifest + objects) as `agent`. Empty dict on no access."""
    data = _aimeat_call(agent, "aimeat_workspace_read", {"organism_id": ROOM_ORG, "ws": ROOM_WS}, quiet=True)
    return data if isinstance(data, dict) and data.get("manifest") is not None else {}


def _room_write(
    space: str, rec_id: str, value: dict, *, publish: bool, namespace: str | None = None, agent: str = AGENT_NAME
) -> tuple[bool, str]:
    """Write ONE object into the MACHINE ROOM as `agent`'s GAII, then optionally publish it.

    House path first (connector tools `aimeat_workspace_write` / `_publish`, proven by some-listener +
    postman); on failure fall back to the guaranteed authed-REST two-call path the room's app uses in
    production (POST /v1/memory …draft → POST /v1/organisms/{org}/publish). Returns (ok, written_id).
    `publish=False` leaves the object as a DRAFT (write only) — that is how insight/proposal stay drafts.
    `namespace` overrides the publish namespace for a space not in NS. `agent` lets every M-ROOM fleet
    member write as its OWN GAII (curator, researcher, …)."""
    ns = namespace or NS.get(space)
    if not ns:
        print(f"[{agent}] no namespace for space {space!r} — cannot write", file=sys.stderr)
        return False, rec_id
    # 1) house connector path
    wrote = _aimeat_call(
        agent,
        "aimeat_workspace_write",
        {"organism_id": ROOM_ORG, "ws": ROOM_WS, "space": space, "id": rec_id, "value": value},
        quiet=True,
    )
    if wrote:
        written_id = (wrote.get("id") if isinstance(wrote, dict) else None) or rec_id
        if not publish:
            return True, written_id
        pub = _aimeat_call(
            agent,
            "aimeat_workspace_publish",
            {"organism_id": ROOM_ORG, "ws": ROOM_WS, "namespace": ns, "id": written_id},
            quiet=True,
        )
        if pub:
            return True, written_id
        print(f"[{agent}] connector publish failed for {space}/{written_id} — trying REST", file=sys.stderr)

    # 2) guaranteed REST two-call path
    return _room_write_rest(ns, rec_id, value, publish=publish, agent=agent)


def _room_write_rest(ns: str, rec_id: str, value: dict, *, publish: bool, agent: str = AGENT_NAME) -> tuple[bool, str]:
    import requests

    try:
        tok, url = _node(agent)
        base, headers = url.rstrip("/"), {"Authorization": f"Bearer {tok}"}
        draft_key = f"organism.{ROOM_ORG}.w.{ROOM_WS}.{ns}.{rec_id}.draft"
        w = requests.post(
            f"{base}/v1/memory",
            json={"key": draft_key, "value": value, "visibility": "owner"},
            headers=headers,
            timeout=45,
        )
        if w.status_code not in (200, 201):
            print(f"[{agent}] REST draft write {ns}/{rec_id} -> HTTP {w.status_code}: {w.text[:200]}", file=sys.stderr)
            return False, rec_id
        if not publish:
            return True, rec_id
        p = requests.post(
            f"{base}/v1/organisms/{ROOM_ORG}/publish",
            json={"namespace": ns, "id": rec_id, "ws": ROOM_WS},
            headers=headers,
            timeout=45,
        )
        if p.status_code not in (200, 201):
            print(f"[{agent}] REST publish {ns}/{rec_id} -> HTTP {p.status_code}: {p.text[:200]}", file=sys.stderr)
            return False, rec_id
        return True, rec_id
    except Exception as exc:  # noqa: BLE001
        print(f"[{agent}] REST write {ns}/{rec_id} failed: {exc!r}", file=sys.stderr)
        return False, rec_id


# --------------------------------------------------------------------------- #
# inputs: hits, cursor, criteria
# --------------------------------------------------------------------------- #
def _hits() -> list[dict]:
    """All fresh feed hits across the 3 keys, each tagged with its poi_id. Newest first.
    Distinguishes a read FAILURE (None) from a genuinely empty feed ([]) and logs loud if every read
    came back empty — so a silent auth/route problem never masquerades as 'no new hits'."""
    out: list[dict] = []
    failed = 0
    for key, poi in FEEDS.items():
        val = _public_memory(_FEED_OWNER, key)
        if val is None:
            failed += 1
            continue
        for h in val or []:
            if isinstance(h, dict) and h.get("url"):
                out.append({**h, "poi_id": poi})
    if failed == len(FEEDS):
        print(
            f"[{AGENT_NAME}] every ext:mroom feed read returned nothing — check the agent is attached "
            f"(running in-fleet) + authed; NOT curating on an empty read",
            file=sys.stderr,
        )
    out.sort(key=lambda h: str(h.get("ts") or ""), reverse=True)
    return out


def _cursor() -> list[str]:
    r = _aimeat_call(AGENT_NAME, "aimeat_memory_read", {"key": _CURSOR_KEY}, quiet=True) or {}
    v = r.get("value") if isinstance(r, dict) else None
    return [u for u in (v or []) if isinstance(u, str)]


def _save_cursor(urls: list[str]) -> None:
    # newest-first, de-duped, capped — a rolling window of what we've already verdicted
    seen, ordered = set(), []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            ordered.append(u)
    _aimeat_call(
        AGENT_NAME,
        "aimeat_memory_write",
        {"key": _CURSOR_KEY, "value": ordered[:_CURSOR_CAP], "visibility": "owner"},
    )


def _criteria(room: dict) -> dict:
    """{global: {accept_if, reject_if, for_accepted_produce, tone}, per_poi: {POI_00X: {keywords, threshold, stance}}}.
    Global from the bootstrap memory key; per-POI from the room's `criteria` records (primary)."""
    glob = _public_memory(_CRITERIA_GAII, _CRITERIA_KEY)
    per_poi = {
        c["poi_id"]: c
        for c in (room.get("objects", {}) or {}).get("criteria", [])
        if isinstance(c, dict) and c.get("poi_id")
    }
    return {"global": glob if isinstance(glob, dict) else {}, "per_poi": per_poi}


def _poi_labels(room: dict) -> dict:
    return {
        p["id"]: p.get("label") or p["id"]
        for p in (room.get("objects", {}) or {}).get("poi", [])
        if isinstance(p, dict) and p.get("id")
    }


# --------------------------------------------------------------------------- #
# fetch (deterministic, no LLM) + judge (batched) + reconsider (bounded)
# --------------------------------------------------------------------------- #
def _fetch(hit: dict) -> str:
    """Main text of the hit's URL, isolated subprocess (a bad page can't kill the daemon)."""
    try:
        return (_trafilatura_text(hit["url"]) or "")[:_TEXT_CHARS]
    except Exception as exc:  # noqa: BLE001
        print(f"[{AGENT_NAME}] fetch failed {hit.get('url')}: {exc!r}", file=sys.stderr)
        return ""


def _criteria_prompt(crit: dict, poi_labels: dict) -> str:
    g = crit.get("global") or {}
    lines = ["CRITERIA (operator-authored — judge strictly against these):"]
    if g.get("accept_if"):
        lines.append("ACCEPT if any hold:\n" + "\n".join(f"  - {a}" for a in g["accept_if"]))
    if g.get("reject_if"):
        lines.append("REJECT if any hold:\n" + "\n".join(f"  - {r}" for r in g["reject_if"]))
    if crit.get("per_poi"):
        lines.append("PER-POI focus (the hit's POI narrows what matters):")
        for poi, c in sorted(crit["per_poi"].items()):
            label = poi_labels.get(poi, poi)
            kw = ", ".join(c.get("keywords") or [])
            lines.append(
                f"  - {poi} ({label}): threshold={c.get('threshold', '')!r} stance={c.get('stance', '')!r} keywords=[{kw}]"
            )
    lines.append(
        g.get("tone") or "Tone: cold, factual, machine-voice. English. State findings; never sell, never sneer."
    )
    return "\n".join(lines)


def _items_block(items: list[dict]) -> str:
    blocks = []
    for i, it in enumerate(items):
        txt = (
            it["text"]
            or "(no article text could be extracted — judge from the headline + your knowledge, or flag ambiguous)"
        )
        blocks.append(
            f"[{i}] POI {it['hit']['poi_id']} · {it['hit'].get('title') or '(untitled)'}\n"
            f"URL: {it['hit']['url']}\n"
            f"TEXT: {txt}"
        )
    return "\n\n".join(blocks)


def _judge(llm, items: list[dict], criteria_prompt: str, poi_labels: dict) -> list[dict]:
    """ONE batched judgement over every item — a single raw `llm.call()` completion, NOT a CrewAI
    agent executor (which loops per item and burns a call each iteration — the per-link chat loop we
    must never do). Returns a list aligned to `items` by the 'i' field."""
    prompt = (
        "You are the M-ROOM curator: the machine that watches AI-agent infrastructure for AIMEAT. Judge "
        "each raw feed hit against the operator's criteria and state a cold, factual verdict. The strongest "
        "signal is popularity AND relevance together. Never sneer at a rejected item, never oversell an "
        "accepted one.\n\n"
        f"{criteria_prompt}\n\n"
        f"HITS ({len(items)}), each with its index:\n\n{_items_block(items)}\n\n"
        "Judge EVERY hit in ONE pass. Return STRICT JSON and NOTHING else:\n"
        '{"verdicts": [{"i": <index>, "verdict": "ACCEPTED"|"REJECTED", "reason": "<1-2 cold factual sentences>", '
        '"idea": "<ACCEPTED only: 1-3 sentences — what to investigate/build/compare>", '
        f'"aimeat_relation": "<ACCEPTED only: one of {list(_AIMEAT_RELATIONS)}>", '
        '"strong": <true only for the rare accept worth an insight + proposal draft>, '
        '"ambiguous": <true if you could not judge from the text and want ONE web check>}]}\n'
        "Rules: one object per hit, same index; REJECTED needs only reason; ACCEPTED needs reason + idea + "
        "aimeat_relation; at most one 'strong'; machine voice, English; output JSON only."
    )
    try:
        raw = llm.call([{"role": "user", "content": prompt}])
    except Exception as exc:  # noqa: BLE001
        print(f"[{AGENT_NAME}] judge call failed: {exc!r}", file=sys.stderr)
        return []
    return _parse_verdicts(str(raw or ""), len(items))


def _reconsider(llm, items: list[dict], verdicts: list[dict], criteria_prompt: str, poi_labels: dict) -> list[dict]:
    """For up to _RECONSIDER_CAP items the judge flagged ambiguous: one web lookup each, then re-judge
    just that subset. Bounded — this is the 'check → correct' step, never a per-link chat loop."""
    from crewaimeat.fetch_pipeline import _searxng_urls

    amb = [v["i"] for v in verdicts if v.get("ambiguous") and 0 <= v["i"] < len(items)][:_RECONSIDER_CAP]
    if not amb:
        return verdicts
    subset = []
    for i in amb:
        it = items[i]
        extra = ""
        try:
            for u in _searxng_urls(it["hit"].get("title") or it["hit"]["url"], "en", "month", n=2):
                extra = _trafilatura_text(u) or ""
                if len(extra) > 300:
                    break
        except Exception as exc:  # noqa: BLE001
            print(f"[{AGENT_NAME}] reconsider search failed for [{i}]: {exc!r}", file=sys.stderr)
        merged_text = (it["text"] + "\n\n[extra lookup]\n" + extra)[:_TEXT_CHARS] if extra else it["text"]
        subset.append({"i": i, "hit": it["hit"], "text": merged_text})
    if not subset:
        return verdicts
    # re-judge the ambiguous subset (indices are re-based 0..n; map back via subset[k]['i'])
    rejudged = _judge(llm, [{"hit": s["hit"], "text": s["text"]} for s in subset], criteria_prompt, poi_labels)
    by_pos = {v["i"]: v for v in rejudged}
    fixed = {v["i"]: v for v in verdicts}
    for pos, s in enumerate(subset):
        nv = by_pos.get(pos)
        if nv:
            nv["i"] = s["i"]
            nv["ambiguous"] = False  # resolved
            fixed[s["i"]] = nv
    return list(fixed.values())


# --------------------------------------------------------------------------- #
# record construction (code owns the envelope; the model only supplies judgement)
# --------------------------------------------------------------------------- #
def _slug(text: str, url: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:40].strip("-")
    tail = hashlib.sha256(url.encode()).hexdigest()[:6]
    return f"{base}-{tail}" if base else f"hit-{tail}"


def _signal_record(hit: dict, v: dict, *, insight_ref: str | None = None) -> dict:
    """Build a schema-valid `signal` record. Code fills the envelope from the deterministic hit; the
    model only contributed verdict/reason/idea/aimeat_relation."""
    accepted = v.get("verdict") == "ACCEPTED"
    rec = {
        "id": f"sig-{_slug(hit.get('title') or '', hit['url'])}",
        "poi_id": hit["poi_id"],
        "captured_at": hit.get("ts") or _now(),
        "headline": (hit.get("title") or "(untitled)")[:200],
        "url": hit["url"],
        "raw_excerpt": " ".join(str(hit.get("snippet") or hit.get("title") or "").split())[:400],
        "match_keywords": [str(k) for k in (hit.get("keywords") or [])][:12],
        "processed": True,
        "verdict": "ACCEPTED" if accepted else "REJECTED",
        "reason": (v.get("reason") or "").strip()[:600],
    }
    if accepted:
        rec["idea"] = (v.get("idea") or "").strip()[:600]
        rel = (v.get("aimeat_relation") or "").strip()
        rec["aimeat_relation"] = rel if rel in _AIMEAT_RELATIONS else "community-pulse"
    if insight_ref:
        rec["insight_ref"] = insight_ref
    return rec


def _draft_insight_and_proposal(llm, hit: dict, v: dict) -> tuple[dict, dict]:
    """Bilingual insight document + a proposal record for a STRONG accept — ONE raw `llm.call()`. Both
    returned as values to be written as DRAFTS (never published)."""
    try:
        from crewaimeat.prose_style import FINNISH_NATIVE_STYLE
    except Exception:  # noqa: BLE001
        FINNISH_NATIVE_STYLE = ""
    prompt = (
        "You write terse machine-room insights for AIMEAT — what the signal is, what it means for this "
        "house, the trend. No hype, cold voice.\n\n"
        f"Strong signal accepted:\n- headline: {hit.get('title')}\n- url: {hit['url']}\n"
        f"- reason: {v.get('reason')}\n- follow-up idea: {v.get('idea')}\n- relation: {v.get('aimeat_relation')}\n\n"
        "Return STRICT JSON only:\n"
        '{"title": "<=90 chars", "markdown": "<Finnish, 2-4 short paragraphs: signal / what it means for this '
        'house / trend>", "markdown_en": "<the same in English>"}\n'
        f"Finnish must read native (compose, don't translate). {FINNISH_NATIVE_STYLE}\n"
        "Reference the signal. Output JSON only."
    )
    try:
        raw = llm.call([{"role": "user", "content": prompt}])
    except Exception as exc:  # noqa: BLE001
        print(f"[{AGENT_NAME}] insight draft failed: {exc!r}", file=sys.stderr)
        raw = ""
    obj = _extract_json(str(raw or "")) or {}
    slug = _slug(hit.get("title") or "", hit["url"])
    insight = {
        "title": (obj.get("title") or hit.get("title") or "Signal")[:120],
        "markdown": obj.get("markdown") or f"**Signaali:** {hit.get('title')}\n\n{v.get('reason')}",
        "markdown_en": obj.get("markdown_en") or f"**Signal:** {hit.get('title')}\n\n{v.get('reason')}",
    }
    proposal = {
        "id": f"prop-{slug}",
        # title paired with the insight (concise) — avoids a mid-word truncation of the raw idea
        "title": insight["title"],
        "decision_mode": "YES",
        "status": "open",
        "created_at": _now(),
        "expires_at": (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=18)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        # insight_id is filled in after the insight draft is written (we learn its id then)
    }
    return insight, proposal


# --------------------------------------------------------------------------- #
# JSON parsing (tolerant — the model sometimes fences or prefaces)
# --------------------------------------------------------------------------- #
def _extract_json(text: str) -> object | None:
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s).rsplit("```", 1)[0]
    try:
        return json.loads(s)
    except ValueError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        i, j = s.find(opener), s.rfind(closer)
        if i != -1 and j > i:
            try:
                return json.loads(s[i : j + 1])
            except ValueError:
                continue
    return None


def _parse_verdicts(raw: str, n: int) -> list[dict]:
    obj = _extract_json(raw)
    rows = obj.get("verdicts") if isinstance(obj, dict) else (obj if isinstance(obj, list) else None)
    if not isinstance(rows, list):
        print(f"[{AGENT_NAME}] judge returned unparseable output ({raw[:160]!r})", file=sys.stderr)
        return []
    out = []
    for r in rows:
        if not isinstance(r, dict) or "i" not in r:
            continue
        try:
            r["i"] = int(r["i"])
        except (ValueError, TypeError):
            continue
        if 0 <= r["i"] < n and r.get("verdict") in ("ACCEPTED", "REJECTED"):
            out.append(r)
    return out


# --------------------------------------------------------------------------- #
# the run
# --------------------------------------------------------------------------- #
def run_curation(llm, *, dry_run: bool = True, cap: int = _BATCH_CAP) -> dict:
    """One curation pass. dry_run=True writes NOTHING to the live room (fetch+judge+build only);
    dry_run=False writes + publishes signals and drafts the strong accept's insight+proposal."""
    room = _room_read()
    if not room and not dry_run:
        note = "MACHINE ROOM not accessible to this agent (not a member / no write access) — nothing written."
        print(f"[{AGENT_NAME}] {note}", file=sys.stderr)
        return {"scanned": 0, "accepted": 0, "rejected": 0, "note": note, "no_access": True}

    cursor = _cursor()
    seen = set(cursor)
    fresh = [h for h in _hits() if h["url"] not in seen][:cap]
    if not fresh:
        summary = {"ts": _now(), "scanned": 0, "accepted": 0, "rejected": 0, "note": "no new hits", "dry_run": dry_run}
        _write_lastrun(summary, dry_run)
        return summary

    items = [{"hit": h, "text": _fetch(h)} for h in fresh]
    crit = _criteria(room)
    poi_labels = _poi_labels(room) or {
        "POI_006": "HN_ARXIV_CSAI",
        "POI_004": "MCP_AGENT_PROTOCOLS",
        "POI_002": "EU_AI_ACT",
    }
    criteria_prompt = _criteria_prompt(crit, poi_labels)

    verdicts = _judge(llm, [{"hit": it["hit"], "text": it["text"]} for it in items], criteria_prompt, poi_labels)
    if not verdicts:
        note = "judge produced no usable verdicts — nothing written (check LLM output above)"
        summary = {"ts": _now(), "scanned": len(items), "accepted": 0, "rejected": 0, "note": note, "dry_run": dry_run}
        _write_lastrun(summary, dry_run)
        return summary
    verdicts = _reconsider(llm, items, verdicts, criteria_prompt, poi_labels)

    accepted = rejected = drafted = written = 0
    would_write: list[dict] = []
    used_strong = False
    for v in sorted(verdicts, key=lambda x: x["i"]):
        hit = items[v["i"]]["hit"]
        insight_ref = None
        # strong accept → draft insight + proposal (rare; at most one per run)
        if v.get("verdict") == "ACCEPTED" and v.get("strong") and not used_strong:
            used_strong = True
            insight, proposal = _draft_insight_and_proposal(llm, hit, v)
            if dry_run:
                would_write += [
                    {"space": "insight", "publish": False, "value": insight},
                    {"space": "proposal", "publish": False, "value": proposal},
                ]
                insight_ref = "(dry-run insight)"
            else:
                ok_i, insight_id = _room_write(
                    "insight", f"ins-{_slug(hit.get('title') or '', hit['url'])}", insight, publish=False
                )
                if ok_i:
                    insight_ref = insight_id
                    proposal["insight_id"] = insight_id
                    _room_write("proposal", proposal["id"], proposal, publish=False)
                    drafted += 1
        rec = _signal_record(hit, v, insight_ref=insight_ref)
        if v["verdict"] == "ACCEPTED":
            accepted += 1
        else:
            rejected += 1
        if dry_run:
            would_write.append({"space": "signal", "publish": True, "value": rec})
        else:
            ok, _ = _room_write("signal", rec["id"], rec, publish=True)
            written += 1 if ok else 0

    if not dry_run:  # a dry run must NOT consume the feed — else the real run skips these unwritten hits
        _save_cursor([it["hit"]["url"] for it in items] + cursor)
    summary = {
        "ts": _now(),
        "scanned": len(items),
        "accepted": accepted,
        "rejected": rejected,
        "drafted": drafted,
        "note": (
            "DRY RUN — nothing written to the room" if dry_run else f"{written} signal(s) published, {drafted} draft(s)"
        ),
        "dry_run": dry_run,
    }
    _write_lastrun(summary, dry_run)
    if dry_run:
        summary["would_write"] = would_write
    return summary


def _write_lastrun(summary: dict, dry_run: bool) -> None:
    """Public run summary the room's agents panel reads. Written even on dry-run (to the agent's OWN
    memory — not the room), so a scheduled dry-run still leaves an inspectable trace."""
    payload = {k: summary[k] for k in ("ts", "scanned", "accepted", "rejected", "note", "dry_run") if k in summary}
    _aimeat_call(AGENT_NAME, "aimeat_memory_write", {"key": _LASTRUN_KEY, "value": payload, "visibility": "public"})


# --------------------------------------------------------------------------- #
# manual dry-run entry point
# --------------------------------------------------------------------------- #
def main() -> None:
    from crewaimeat.llm import get_llm

    dry = not _live()
    print(f"[{AGENT_NAME}] {'DRY RUN' if dry else 'LIVE'} — starting one curation pass…", file=sys.stderr)
    summary = run_curation(get_llm(for_tool_use=False, agent_name=AGENT_NAME), dry_run=dry)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

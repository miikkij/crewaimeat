"""some-listener: DETERMINISTIC social-radar scanner for AIMEAT-relevant discussions.

Scanning ONLY. It finds WHERE people are discussing agent memory / multi-agent systems / agent
infrastructure (Hacker News via the free Algolia API in v1), writes a ranked radar (log + memory),
and syncs new hits into the Social Radar workspace as `opportunity` records. It never
posts, replies, or contacts anyone — a human reviews the radar and decides what (if anything) to
engage with. The scan is plain code (not a judgement call left to an LLM), so results are real.

Reddit/X can be added later (they need API keys); the structure here is per-source so it's easy.
"""

from __future__ import annotations

import datetime
import html
import json
import os
import re
import sys

import requests
from crewai.tools import tool

from crewaimeat.aimeat_crew import _aimeat_call

# Topics that signal a genuine fit for AIMEAT (agent substrate / shared memory / multi-agent).
KEYWORDS = [
    "agent memory",
    "shared memory agents",
    "multi-agent",
    "agent orchestration",
    "autonomous agents",
    "agent framework",
    "CrewAI",
    "agent backend",
    "stateful agents",
    "long-running agents",
    "MCP agents",
    "agent infrastructure",
]

# Down-rank obvious off-topic uses of the word "agent".
_NOISE = re.compile(r"\b(real estate|insurance|travel|secret|free|booking|support) agent\b", re.I)
# A real question / help-seeking signal = a good place to genuinely help.
_QUESTION = re.compile(
    r"\?|how (do|to|can)|what'?s the best|recommend|anyone (using|tried|know)|looking for|struggl|stuck|alternativ",
    re.I,
)

_HN = "https://hn.algolia.com/api/v1/search_by_date"

# Social Radar workspace (the crewaimeat dogfood organism) — where scanned opportunities land.
_ORG_ID = "b784641b-a4dd-4d69-adb6-9954dc813e1e"
_RADAR_WS = "ws-mq641mohh0e"


def _hn(query: str, since_ts: int, n: int = 20) -> list[dict]:
    try:
        r = requests.get(
            _HN,
            params={
                "query": query,
                "tags": "(story,comment)",
                "numericFilters": f"created_at_i>{since_ts}",
                "hitsPerPage": n,
            },
            timeout=20,
        )
        return (r.json() or {}).get("hits") or []
    except Exception:
        return []


def _score(hit: dict) -> tuple[int, str]:
    text = " ".join(str(hit.get(k) or "") for k in ("title", "story_title", "comment_text", "story_text"))
    if _NOISE.search(text):
        return (-1, "")
    pts = int(hit.get("points") or 0)
    ncom = int(hit.get("num_comments") or 0)
    is_q = bool(_QUESTION.search(text))
    s = (4 if is_q else 0) + min(pts, 50) // 10 + min(ncom, 50) // 10
    return (s, "question / help-seeking" if is_q else "active discussion")


def _opportunity_record(hit: dict, date: str) -> dict:
    matched = ", ".join(sorted(set(hit.get("matched", []))))
    return {
        "id": f"opp-hn-{hit['id']}",
        "source": "hn",
        "url": hit["url"],
        "title": hit["title"],
        "summary": hit.get("snippet") or hit["title"],
        # fit_score is a 0-5 schema field; the deterministic scan score is a rough proxy (clamped).
        "fit_score": max(0, min(5, int(hit.get("score", 0)))),
        # The scanner cannot judge welcome/no — that is the human's / Grok's call.
        "spam_risk": "borderline",
        "angle": f"Auto-scanned HN ({hit.get('why', '')}; matched: {matched}). Needs human/Grok judgement for fit + angle.",
        "status": "new",
        "found_date": date,
    }


def _sync_to_radar(ranked: list[dict], date: str) -> dict:
    """Write NEW HN hits into the Social Radar workspace as `opportunity` records (deterministic, no LLM).

    Skips hits already present (by id) so a re-scan never overwrites a human's triage (status/angle).
    Best-effort: logs loudly on failure, never crashes the scan.
    """
    data = _aimeat_call("some-listener", "aimeat_workspace_read", {"organism_id": _ORG_ID, "ws": _RADAR_WS})
    if not data or data.get("manifest") is None:
        # Same-owner sub-agents currently can't access the owner's organism workspaces via the connector
        # (list=[], read manifest=null, write=NO_SPACE) even as org members — a known platform gap.
        print(
            "[some-listener] Social Radar not accessible to this agent yet — skipping workspace sync "
            "(radar still written to log + memory).",
            file=sys.stderr,
        )
        return {"added": 0, "skipped": 0, "failed": 0, "no_access": True}
    existing = {o["id"] for o in data.get("objects", {}).get("opportunity", []) if o.get("id")}
    added = skipped = failed = 0
    for hit in ranked:
        oid = f"opp-hn-{hit['id']}"
        if oid in existing:
            skipped += 1
            continue
        rec = _opportunity_record(hit, date)
        wrote = _aimeat_call(
            "some-listener",
            "aimeat_workspace_write",
            {"organism_id": _ORG_ID, "ws": _RADAR_WS, "space": "opportunity", "id": oid, "value": rec},
        )
        pub = (
            _aimeat_call(
                "some-listener",
                "aimeat_workspace_publish",
                {"organism_id": _ORG_ID, "ws": _RADAR_WS, "namespace": "shared.opportunities", "id": oid},
            )
            if wrote
            else None
        )
        if wrote and pub:
            added += 1
        else:
            failed += 1
            print(
                f"[some-listener] Social Radar sync FAILED for {oid} (write={bool(wrote)}, publish={bool(pub)})",
                file=sys.stderr,
            )
    return {"added": added, "skipped": skipped, "failed": failed}


def scan_hn(hours: int = 48, limit: int = 12) -> dict:
    """Deterministically scan Hacker News for AIMEAT-relevant threads in the last `hours`.

    Writes a ranked radar to logs/some_radar_<date>.log AND memory (some.radar.<date> + some.radar.latest).
    Returns {date, count, log, top}. No posting.
    """
    since = int((datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)).timestamp())
    seen: dict[str, dict] = {}
    for kw in KEYWORDS:
        for h in _hn(kw, since):
            oid = h.get("objectID")
            if not oid:
                continue
            s, why = _score(h)
            if s < 0:
                continue
            row = seen.get(oid)
            if row:
                row["score"] += 1  # bonus for matching multiple keywords
                row["matched"].append(kw)
                continue
            title = h.get("title") or h.get("story_title") or "(comment)"
            snippet = re.sub(r"<[^>]+>", "", html.unescape(h.get("comment_text") or h.get("story_text") or ""))
            seen[oid] = {
                "id": oid,
                "url": f"https://news.ycombinator.com/item?id={oid}",
                "title": str(title)[:140],
                "kind": (h.get("_tags") or ["?"])[0],
                "points": int(h.get("points") or 0),
                "comments": int(h.get("num_comments") or 0),
                "author": h.get("author"),
                "created": h.get("created_at"),
                "score": s,
                "why": why,
                "snippet": " ".join(snippet.split())[:220],
                "matched": [kw],
            }
    ranked = sorted(seen.values(), key=lambda r: r["score"], reverse=True)[:limit]

    # SEMANTIC DEDUP (on top of the objectID + workspace-id checks, which only catch the SAME post):
    # a story that resurfaced from an EARLIER DAY as a new HN submission with new wording is dropped.
    # The scan runs several times per day, so a SAME-DAY match is kept (re-scans stay idempotent — the
    # radar shows today's current top list, not a shrinking delta) and an item is remembered only once.
    # Skips are LOGGED with the match. Memory degrades LOUD to None -> the full ranked list stands.
    from crewaimeat.pipeline_memory import open_store

    today = datetime.date.today().isoformat()
    store = open_store("some-listener")
    if store:
        kept = []
        for r in ranked:
            item_text = f"{r['title']} — {r['snippet']}"
            dup = store.dedup_check(item_text, threshold=0.9, category="radar")
            if dup.is_dup and dup.best_metadata.get("date") != today:
                print(
                    f"[some-radar] drop resurfaced story (score {dup.best_score:.2f}, "
                    f"seen {dup.best_metadata.get('date')}): {r['title'][:80]!r}",
                    file=sys.stderr,
                )
                continue
            if not dup.is_dup:  # first sighting -> remember once; same-day re-scans don't re-store
                store.remember(
                    item_text, source="radar", metadata={"category": "radar", "hn_id": r["id"], "date": today}
                )
            kept.append(r)
        ranked = kept

    date = today
    out = [f"# some-radar {date} — Hacker News, last {hours}h — {len(ranked)} hits (SCAN ONLY, human decides)\n"]
    for r in ranked:
        out.append(
            f"- [{r['score']}] {r['title']}\n"
            f"    {r['url']}  ({r['points']}p / {r['comments']}c · {r['why']} · matched: {', '.join(sorted(set(r['matched'])))})\n"
            f"    {r['snippet']}"
        )
    report = "\n".join(out)
    os.makedirs("logs", exist_ok=True)
    log_path = f"logs/some_radar_{date}.log"
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(report)

    # Best-effort: also store in AIMEAT memory (works once 'some-listener' is registered+approved).
    for key in (f"some.radar.{date}", "some.radar.latest"):
        try:
            _aimeat_call("some-listener", "aimeat_memory_write", {"key": key, "value": ranked, "visibility": "owner"})
        except Exception:  # noqa: BLE001
            pass

    # Public mirror for CROSS-ORGANISM display: another organism (e.g. the M-ROOM, same owner) reads this
    # with aimeat_memory_read_public(gaii, "some.radar.public.latest") — the exact path M-ROOM already uses
    # for its ext:mroom feeds. Display-safe shape only: public HN thread pointers + our scoring, no
    # owner-private context. Best-effort; never blocks the scan.
    try:
        public = {
            "date": date,
            "count": len(ranked),
            "items": [
                {"title": r["title"], "url": r["url"], "score": r["score"], "why": r["why"], "snippet": r["snippet"]}
                for r in ranked
            ],
        }
        _aimeat_call(
            "some-listener",
            "aimeat_memory_write",
            {"key": "some.radar.public.latest", "value": public, "visibility": "public"},
        )
    except Exception:  # noqa: BLE001
        pass

    # Sync new hits into the Social Radar workspace as opportunity records (deterministic, no LLM).
    radar = _sync_to_radar(ranked, date)

    return {"date": date, "count": len(ranked), "log": log_path, "radar": radar, "top": ranked}


def make_listener_tools(agent_name: str) -> list:
    """The single scan tool the some-listener agent uses. Scanning only — no posting tools exist here."""

    @tool("scan_hn")
    def _scan(hours: int = 48, limit: int = 12) -> str:
        """Scan Hacker News (free Algolia API) for AIMEAT-relevant discussions in the last `hours` (default 48).
        Writes a ranked radar to logs/some_radar_<date>.log + memory some.radar.latest. Returns the ranked hits.
        SCANNING ONLY — this never posts, replies, or contacts anyone."""
        res = scan_hn(hours=hours, limit=limit)
        rad = res.get("radar", {})
        head = (
            f"Scanned HN (last {hours}h): {res['count']} hits -> {res['log']} + some.radar.latest. "
            f"Social Radar: +{rad.get('added', 0)} new ({rad.get('skipped', 0)} present, "
            f"{rad.get('failed', 0)} failed).\n"
        )
        return head + json.dumps(res["top"], ensure_ascii=False, indent=2)[:3500]

    return [_scan]

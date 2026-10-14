"""some-listener: DETERMINISTIC social-radar scanner for AIMEAT-relevant discussions.

Scanning ONLY. It finds WHERE people are discussing agent memory / multi-agent systems / agent
infrastructure, writes a ranked radar (log + memory), and syncs new hits into the Social Radar
workspace as `opportunity` records. It never posts, replies, or contacts anyone — a human reviews the
radar and decides what (if anything) to engage with. The scan is plain code (not a judgement call
left to an LLM), so results are real.

2026-10-15: the sources and the relevance rule moved to `some_sources.py` (Hacker News, plus Bluesky
search and Mastodon hashtag timelines; a hit must contain a radar PHRASE in its own text, and hiring
threads are out), and the radar got a lifecycle: `age_out` marks an opportunity nobody touched for
STALE_DAYS as skipped, and its unreviewed draft with it. Nothing is deleted. The tool keeps its name
`scan_hn` because the schedule on the node calls it by that name.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
from functools import partial

from crewai.tools import tool

from crewaimeat.aimeat_crew import _aimeat_call, today_local
from crewaimeat.some_sources import HIRING, bluesky_hits, hn_hits, mastodon_hits
from crewaimeat.workspace_tools import OBJECTS_BATCH, published_ids, read_workspace_objects

# Social Radar workspace (the crewaimeat dogfood organism) — where scanned opportunities land.
_ORG_ID = "b784641b-a4dd-4d69-adb6-9954dc813e1e"
_RADAR_WS = "ws-mq641mohh0e"

# An opportunity nobody triaged in this many days is no longer a conversation anyone is in.
STALE_DAYS = 14
# Records the age-out updates per run: two node calls each, and the backlog clears over days.
AGE_OUT_PER_RUN = 150

_NAMESPACE = {"opportunity": "shared.opportunities", "reply-draft": "shared.drafts"}


def _call(tool_name: str, payload: dict):
    return _aimeat_call("some-listener", tool_name, payload)


def _opportunity_record(hit: dict, date: str) -> dict:
    matched = ", ".join(hit.get("matched", []))
    return {
        "id": hit["key"],
        "source": hit["source"],
        "url": hit["url"],
        "title": hit["title"],
        "summary": hit.get("snippet") or hit["title"],
        "fit_score": int(hit.get("fit", 0)),
        # The scanner cannot judge welcome/no — that is the human's / Grok's call.
        "spam_risk": "borderline",
        "angle": f"Auto-scanned {hit['source']} ({hit.get('why', '')}; matched: {matched}). Needs human judgement for fit + angle.",
        "status": "new",
        "found_date": date,
    }


def _radar_index():
    """The workspace's index only: ids, titles and dates, no record opened. None when unreadable."""
    data = read_workspace_objects(partial(_aimeat_call, "some-listener"), _ORG_ID, _RADAR_WS, spaces=[])
    if not data or data.get("manifest") is None:
        return None
    return data


def _sync_to_radar(ranked: list[dict], date: str, index) -> dict:
    """Write NEW hits into the Social Radar workspace as `opportunity` records (deterministic, no LLM).

    Skips hits already present (by id) so a re-scan never overwrites a human's triage (status/angle).
    The ids come from the index, so no record is opened for the check (there were 1,429 to open).
    """
    if index is None:
        print(
            "[some-listener] Social Radar not accessible to this agent — skipping workspace sync "
            "(radar still written to log + memory).",
            file=sys.stderr,
        )
        return {"added": 0, "skipped": 0, "failed": 0, "no_access": True}
    existing = published_ids(index, "opportunity")
    added = skipped = failed = 0
    for hit in ranked:
        oid = hit["key"]
        if oid in existing:
            skipped += 1
            continue
        rec = _opportunity_record(hit, date)
        wrote = _call(
            "aimeat_workspace_write",
            {"organism_id": _ORG_ID, "ws": _RADAR_WS, "space": "opportunity", "id": oid, "value": rec},
        )
        pub = (
            _call(
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


def _stale_candidates(index, today: datetime.date) -> dict[str, list[str]]:
    """Ids worth opening for the age-out, read from the index alone: records not updated for
    STALE_DAYS, and any opportunity whose title is a hiring thread whatever its age."""
    out: dict[str, list[str]] = {}
    rows_by_space = index.get("index") if isinstance(index.get("index"), dict) else {}
    for space in ("opportunity", "reply-draft"):
        picked: list[str] = []
        for row in rows_by_space.get(space) or []:
            if not isinstance(row, dict) or not row.get("id") or not row.get("published"):
                continue
            updated = str(row.get("updated") or "")[:10]
            try:
                age = (today - datetime.date.fromisoformat(updated)).days if updated else 0
            except ValueError:
                age = 0
            hiring = space == "opportunity" and HIRING.search(str(row.get("title") or ""))
            if age >= STALE_DAYS or hiring:
                picked.append(str(row["id"]))
        out[space] = picked
    return out


def age_out(index, today: datetime.date | None = None, limit: int = AGE_OUT_PER_RUN, read=None, write=None) -> dict:
    """Mark untouched opportunities older than STALE_DAYS (and hiring threads) `skipped`, and the
    unreviewed drafts older than STALE_DAYS with them. Only records still `new` / `draft` change:
    anything a person triaged, approved or posted is left exactly as it is. Nothing is deleted.
    """
    if index is None:
        return {"aged": 0, "drafts": 0, "failed": 0, "no_access": True}
    today = today or datetime.date.fromisoformat(today_local())
    read = read or partial(_aimeat_call, "some-listener")
    write = write or _call
    picked = _stale_candidates(index, today)
    aged = drafts = failed = 0
    budget = limit
    stamp = today.isoformat()
    for space, status_from in (("opportunity", "new"), ("reply-draft", "draft")):
        ids = picked.get(space, [])
        for start in range(0, len(ids), OBJECTS_BATCH):
            if budget <= 0:
                break
            got = read(
                "aimeat_workspace_read",
                {"organism_id": _ORG_ID, "ws": _RADAR_WS, "space": space, "ids": ids[start : start + OBJECTS_BATCH]},
            )
            items = got.get("items") if isinstance(got, dict) else None
            if not isinstance(items, list):
                print(f"[some-listener] age-out could not read {space}; nothing changed there", file=sys.stderr)
                failed += 1
                break
            for item in items:
                if budget <= 0:
                    break
                value = item.get("value") if isinstance(item, dict) else None
                if not isinstance(value, dict) or value.get("status") != status_from:
                    continue
                rid = str(value.get("id") or item.get("id"))
                note = f"Aged out {stamp}: nobody picked it up in {STALE_DAYS} days."
                if space == "opportunity" and HIRING.search(str(value.get("title") or "")):
                    note = f"Aged out {stamp}: a hiring thread, not a place to talk about a product."
                new_value = {**value, "status": "skipped", "angle": f"{note} {value.get('angle') or ''}".strip()}
                ok = write(
                    "aimeat_workspace_write",
                    {"organism_id": _ORG_ID, "ws": _RADAR_WS, "space": space, "id": rid, "value": new_value},
                )
                pub = (
                    write(
                        "aimeat_workspace_publish",
                        {"organism_id": _ORG_ID, "ws": _RADAR_WS, "namespace": _NAMESPACE[space], "id": rid},
                    )
                    if ok
                    else None
                )
                budget -= 1
                if ok and pub:
                    if space == "opportunity":
                        aged += 1
                    else:
                        drafts += 1
                else:
                    failed += 1
                    print(f"[some-listener] age-out FAILED for {space} {rid}", file=sys.stderr)
    return {
        "aged": aged,
        "drafts": drafts,
        "failed": failed,
        "left": max(0, sum(len(v) for v in picked.values()) - (aged + drafts)),
    }


#: Each source's share of the radar. Hacker News stories carry points and comments that a Bluesky or
#: Mastodon post never reaches, so one ranking over all of them filled every place with Hacker News
#: (measured 2026-10-15: the top 15 of 110 hits were all HN). A share keeps each network in view;
#: places a source cannot fill go to the best of the rest.
SOURCE_SHARE = {"hn": 0.5, "bluesky": 0.3, "mastodon": 0.2}


def share_out(hits: list[dict], limit: int) -> list[dict]:
    """The radar's `limit` places: each source's best up to its share, then the best of the rest."""
    by_rank = sorted(hits, key=lambda r: r["rank"], reverse=True)
    chosen: list[dict] = []
    for source, share in SOURCE_SHARE.items():
        chosen += [h for h in by_rank if h["source"] == source][: max(1, round(limit * share))]
    chosen = chosen[:limit]
    rest = [h for h in by_rank if h not in chosen]
    chosen += rest[: limit - len(chosen)]
    return sorted(chosen, key=lambda r: r["rank"], reverse=True)


def scan_hn(hours: int = 48, limit: int = 12) -> dict:
    """Scan Hacker News, Bluesky and Mastodon for AIMEAT-relevant discussions in the last `hours`.

    Writes a ranked radar to logs/some_radar_<date>.log AND memory (some.radar.<date> + some.radar.latest),
    syncs new hits into the Social Radar workspace, and ages out what nobody picked up. No posting.
    """
    now = datetime.datetime.now(datetime.timezone.utc)
    since = now - datetime.timedelta(hours=hours)
    hits = hn_hits(int(since.timestamp())) + bluesky_hits(since) + mastodon_hits(since)
    by_source: dict[str, int] = {}
    for h in hits:
        by_source[h["source"]] = by_source.get(h["source"], 0) + 1
    ranked = share_out(hits, limit)

    # SEMANTIC DEDUP (on top of the id checks, which only catch the SAME post): a story that
    # resurfaced from an EARLIER DAY with new wording is dropped. A same-day match is kept, so
    # re-scans stay idempotent. Memory degrades LOUD to None -> the full ranked list stands.
    from crewaimeat.pipeline_memory import open_store

    today = today_local()
    store = open_store("some-listener")
    if store:
        kept = []
        for r in ranked:
            item_text = f"{r['title']} — {r['snippet']}"
            dup = store.dedup_check(item_text, threshold=0.9, category="radar")
            if dup.is_dup and dup.best_metadata.get("date") != today:
                print(
                    f"[some-radar] drop resurfaced item (score {dup.best_score:.2f}, seen {dup.best_metadata.get('date')}): {r['title'][:80]!r}",
                    file=sys.stderr,
                )
                continue
            if not dup.is_dup:
                store.remember(
                    item_text, source="radar", metadata={"category": "radar", "key": r["key"], "date": today}
                )
            kept.append(r)
        ranked = kept

    date = today
    sources = ", ".join(f"{k} {v}" for k, v in sorted(by_source.items())) or "none answered"
    out = [f"# some-radar {date} — last {hours}h — {len(ranked)} hits from {sources} (SCAN ONLY, human decides)\n"]
    for r in ranked:
        out.append(
            f"- [{r['rank']}] ({r['source']}) {r['title']}\n"
            f"    {r['url']}  ({r['points']}p / {r['comments']}c · {r['why']} · matched: {', '.join(r['matched'])})\n"
            f"    {r['snippet']}"
        )
    report = "\n".join(out)
    os.makedirs("logs", exist_ok=True)
    log_path = f"logs/some_radar_{date}.log"
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(report)

    for key in (f"some.radar.{date}", "some.radar.latest"):
        try:
            _call("aimeat_memory_write", {"key": key, "value": ranked, "visibility": "owner"})
        except Exception as exc:  # noqa: BLE001 — the scan log on disk is still written
            print(f"[some-listener] radar write to {key} FAILED: {exc!r}", file=sys.stderr)

    # Public mirror for CROSS-ORGANISM display (the M-ROOM reads some.radar.public.latest). Display-safe
    # shape only: public thread pointers + our scoring. Provenance deliberately not declared: the
    # titles and snippets are strangers' posts, and no model touches this record.
    try:
        public = {
            "date": date,
            "count": len(ranked),
            "items": [
                {
                    "title": r["title"],
                    "url": r["url"],
                    "score": r["rank"],
                    "why": r["why"],
                    "snippet": r["snippet"],
                    "source": r["source"],
                }
                for r in ranked
            ],
        }
        _call("aimeat_memory_write", {"key": "some.radar.public.latest", "value": public, "visibility": "public"})
    except Exception as exc:  # noqa: BLE001 — best-effort, but NEVER silent: this key is the M-ROOM bridge
        print(f"[some-listener] PUBLIC MIRROR some.radar.public.latest FAILED: {exc!r}", file=sys.stderr)

    index = _radar_index()
    radar = _sync_to_radar(ranked, date, index)
    aged = age_out(index)

    return {
        "date": date,
        "count": len(ranked),
        "sources": by_source,
        "log": log_path,
        "radar": radar,
        "aged": aged,
        "top": ranked,
    }


def make_listener_tools(agent_name: str) -> list:
    """The single scan tool the some-listener agent uses. Scanning only — no posting tools exist here."""

    @tool("scan_hn")
    def _scan(hours: int = 48, limit: int = 12) -> str:
        """Scan Hacker News, Bluesky and Mastodon for AIMEAT-relevant discussions in the last `hours`
        (default 48). Writes a ranked radar to logs/some_radar_<date>.log + memory some.radar.latest,
        adds new hits to the Social Radar workspace, and marks what nobody picked up in 14 days as
        skipped. SCANNING ONLY — this never posts, replies, or contacts anyone."""
        res = scan_hn(hours=hours, limit=limit)
        rad = res.get("radar", {})
        aged = res.get("aged", {})
        head = (
            f"Scanned the last {hours}h ({json.dumps(res.get('sources', {}))}): {res['count']} hits -> {res['log']} + some.radar.latest. "
            f"Social Radar: +{rad.get('added', 0)} new ({rad.get('skipped', 0)} present, {rad.get('failed', 0)} failed). "
            f"Aged out {aged.get('aged', 0)} opportunities and {aged.get('drafts', 0)} drafts ({aged.get('left', 0)} left for later runs).\n"
        )
        return head + json.dumps(res["top"], ensure_ascii=False, indent=2)[:3500]

    return [_scan]

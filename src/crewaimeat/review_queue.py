"""The reply-draft backlog, reduced to what is still worth a human's attention.

MEASURED 2026-08-11 in the Social Radar workspace: 664 opportunities, 297 reply-drafts, **0 posted**.
257 of the 297 drafts are from July. The machine has been drafting for six weeks and nothing has
ever shipped, so the bottleneck is entirely review — and 86 % of the queue is answering month-old
threads, which is not review work, it is archaeology.

A UI that shows 297 rows would therefore be a faithful rendering of a useless queue. This module
takes the two decisions that make it usable and takes them in CODE, where they can be read:

  · FRESHNESS — only drafts whose opportunity was found within `days` are actionable. Replying to a
    month-old X thread reaches nobody, so an old draft is not "pending", it is expired.
  · SIZE — a queue longer than `limit` does not get reviewed either, so the rest is reported as a
    COUNT, honestly labelled, instead of being hidden or padded out.

The result is written to `some.queue.latest` for the Aamukatsaus app: the browser reads one small
record instead of a 253 kB workspace index, and the filtering stays here where it is auditable.
"""

from __future__ import annotations

import datetime
import sys

from crewaimeat.aimeat_crew import _aimeat_call

AGENT = "some-analyst"
HOME_ORG = "b784641b-a4dd-4d69-adb6-9954dc813e1e"
RADAR_WS = "ws-mq641mohh0e"

FRESH_DAYS = 7  # a thread older than a week is past the point where a reply lands
MAX_ITEMS = 25  # a queue nobody finishes is the same as no queue


def _ws_read(agent: str = AGENT) -> dict[str, list]:
    """Every record in the Social Radar workspace, by space.

    The connector's `aimeat_workspace_read` returns FULL records under `objects` (the AppDev MCP
    surface returns a lighter `index` instead — different shapes, same tool name, so read the one we
    actually get). ~1 MB for 964 records, fetched once a day by an agent, never by the browser."""
    d = _aimeat_call(agent, "aimeat_workspace_read", {"organism_id": HOME_ORG, "ws": RADAR_WS})
    objs = (d or {}).get("objects") if isinstance(d, dict) else None
    return objs if isinstance(objs, dict) else {}


def build_queue(days: int = FRESH_DAYS, limit: int = MAX_ITEMS, agent: str = AGENT, now=None) -> dict:
    """Join fresh reply-drafts to their opportunities and return the review queue + backlog truth."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    cutoff = (now - datetime.timedelta(days=days)).isoformat()
    objs = _ws_read(agent)
    drafts_all = [d for d in (objs.get("reply-draft") or []) if isinstance(d, dict)]
    opps_all = [o for o in (objs.get("opportunity") or []) if isinstance(o, dict)]
    opps = {o.get("id"): o for o in opps_all if o.get("id")}
    posted = len(objs.get("posted") or [])

    def when(rec: dict) -> str:
        return rec.get("_updatedAt") or rec.get("_createdAt") or ""

    # Only drafts still waiting on a person, and only ones whose thread is recent enough that a
    # reply still reaches someone. `status` carries the reviewer's own verdict once one exists.
    open_drafts = [d for d in drafts_all if (d.get("status") or "draft") in ("draft", "new", "pending")]
    fresh = sorted((d for d in open_drafts if when(d) >= cutoff), key=when, reverse=True)

    items = []
    for d in fresh[:limit]:
        opp = opps.get(d.get("opportunity_ref")) or {}  # the join the schema gives us, no id parsing
        items.append(
            {
                "id": d.get("id"),
                "title": opp.get("title") or d.get("id"),
                "url": opp.get("url") or "",  # the thread — a reply cannot be judged without opening it
                "platform": d.get("platform") or (opp.get("source") or "").replace("grok-", "") or "?",
                "fit_score": opp.get("fit_score"),
                "spam_risk": opp.get("spam_risk"),
                "summary": opp.get("summary") or "",
                "angle": d.get("angle") or opp.get("angle") or "",
                "draft": d.get("draft") or "",
                "status": d.get("status") or "draft",
                "found_date": opp.get("found_date") or "",
                "updated": when(d),
            }
        )
    items.sort(key=lambda i: (-(i.get("fit_score") or 0), i.get("updated") or ""))

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "fresh_days": days,
        "items": items,
        # The backlog is REPORTED, never silently dropped: a queue that quietly hides 257 items is
        # how 297 accumulated unnoticed in the first place.
        "backlog": {
            "drafts_total": len(drafts_all),
            "drafts_open": len(open_drafts),
            "drafts_fresh": len(fresh),
            "drafts_expired": len(open_drafts) - len(fresh),
            "opportunities_total": len(opps_all),
            "posted_total": posted,
            "note": (
                f"{len(open_drafts) - len(fresh)} open draft(s) are older than {days} days — past the point "
                f"where a reply lands. {posted} have ever been posted."
            ),
        },
    }


def publish(days: int = FRESH_DAYS, limit: int = MAX_ITEMS, agent: str = AGENT) -> dict:
    """Build the queue and write it to `some.queue.latest` for the app."""
    q = build_queue(days=days, limit=limit, agent=agent)
    # No ai_provenance: every field is copied verbatim from records that carry their own, or is a
    # count this code measured. Declaring one here would attribute the drafts' authorship to us.
    if (
        _aimeat_call(agent, "aimeat_memory_write", {"key": "some.queue.latest", "value": q, "visibility": "owner"})
        is None
    ):
        print(f"[{agent}] review queue: write some.queue.latest FAILED", file=sys.stderr)
    b = q["backlog"]
    print(
        f"[{agent}] review queue: {len(q['items'])} actionable · {b['drafts_expired']} expired · "
        f"{b['posted_total']} ever posted",
        file=sys.stderr,
    )
    return q

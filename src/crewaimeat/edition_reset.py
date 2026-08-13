"""Wipe ONE edition's regenerable keys so it can be produced again from scratch.

WHY THIS EXISTS. Re-running a bad edition on top of itself does not give a clean result: the raw
stays whatever the last fetch produced, `_recent_seen_urls` excludes the very URLs the replacement
should be allowed to use again, and articles from the old run survive for categories the new run
skips. A reset makes "produce this day again" mean what it says.

WHAT IS DELETED — only what the pipeline can rebuild:
    news.<date>.<edition>.raw            the scraped sources (fetch rebuilds)
    news.<date>.<edition>.raw.<cat>      pre-consolidation per-category raw, if any survives
    news.<date>.<edition>.article.*      every article (the desks rebuild)
    news.<date>.<edition>.quiz           (features rebuilds)
    news.<date>.<edition>.editorial      (editorial rebuilds)
    news.<date>.<edition>.status         the step record (re-seeded by fetch)

WHAT IS KEPT, AND THIS IS THE POINT OF THE LIST:
    news.<date>.<edition>.raw.lukijoilta   READER TIPS. A person sent these to us through the
        Sanomat desk; no fetch can bring them back. Deleting them to "clean up" would destroy
        submitted material to save re-scraping a news site. Opt in with keep_tips=False only if
        you know what you are throwing away.
    newspaper.frontpage    the index spans MANY days; the editorial step rewrites this day's
        entries on its own. Wiping it would take out every other edition too.

DELETION MECHANICS, measured 2026-08-13: the edition's keys are written by six different agents,
so `DELETE /v1/memory/<key>` with the writer's own token 404s (the lookup is namespaced). What
works is any of the owner's agents with `?owner_scope=true`, which resolves the owner's whole
family. That is the only path used here.

DRY RUN IS THE DEFAULT everywhere in this module: `plan()` tells you what would go, `reset()`
requires confirm=True. A destructive tool that acts on its default invocation is a trap.
"""

from __future__ import annotations

import sys

from crewaimeat.aimeat_crew import _aimeat_call, _aimeat_rest

AGENT = "news-fetcher"  # any of the owner's agents can do this; the fetcher owns the edition's raw
TIPS_SUFFIX = ".raw.lukijoilta"
STATUS_SUFFIX = ".status"


def _keys(date: str, edition: str, agent: str = AGENT) -> list[str]:
    r = _aimeat_call(
        agent, "aimeat_memory_list", {"owner_scope": True, "prefix": f"news.{date}.{edition}.", "limit": 500}
    )
    items = (r.get("items") or []) if isinstance(r, dict) else []
    return sorted(k for k in (i.get("key") for i in items) if k)


def plan(date: str, edition: str = "evening", keep_tips: bool = True, agent: str = AGENT) -> dict:
    """What a reset would delete and what it would keep. Reads only."""
    delete, keep = [], []
    for k in _keys(date, edition, agent):
        if keep_tips and k.endswith(TIPS_SUFFIX):
            keep.append(k)
        elif k.endswith(STATUS_SUFFIX):
            # The status record is written with owner_scope, so it belongs to the OWNER's GHII and
            # an agent token cannot delete it (measured 2026-08-13: 404 NOT_FOUND, while the same
            # call removed all 27 agent-owned keys). That refusal is correct — an agent has no
            # business deleting the owner's own records — and it costs nothing here: `seed_status`
            # rewrites all six fields to "queued" at the start of the next fetch, so a surviving
            # record is re-initialised rather than stale. Listing it as a failure would report a
            # mixed edition that is not mixed.
            keep.append(k)
        else:
            delete.append(k)
    return {"date": date, "edition": edition, "delete": delete, "keep": keep}


def reset(
    date: str, edition: str = "evening", *, confirm: bool = False, keep_tips: bool = True, agent: str = AGENT
) -> dict:
    """Delete the edition's regenerable keys. Does nothing unless `confirm=True`.

    Reports every failure rather than stopping: a key that will not delete leaves the edition in a
    mixed state, and the caller has to know which one so the re-run can be judged."""
    p = plan(date, edition, keep_tips=keep_tips, agent=agent)
    if not confirm:
        p["dry_run"] = True
        return p
    deleted, failed = [], []
    for k in p["delete"]:
        # owner_scope: the six writing agents each own their own keys, and only the owner-scoped
        # path reaches all of them from one caller (measured — see the module note).
        if _aimeat_rest(agent, "DELETE", f"/v1/memory/{k}?owner_scope=true") is None:
            failed.append(k)
        else:
            deleted.append(k)
    print(
        f"[{agent}] reset {date} {edition}: {len(deleted)} deleted, {len(failed)} FAILED, "
        f"{len(p['keep'])} kept" + (f" — kept: {', '.join(p['keep'])}" if p["keep"] else ""),
        file=sys.stderr,
    )
    if failed:
        print(f"[{agent}] reset: could NOT delete {failed}", file=sys.stderr)
    return {**p, "dry_run": False, "deleted": deleted, "failed": failed}

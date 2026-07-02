"""Backfill the Sanomat pipeline memories from what the paper ALREADY published on the node.

The editorial/section memory wiring (pipeline_memory) starts cold — this one-shot, owner-run script
seeds it from history so anti-repetition and continuity work from day one. It reads the node
READ-ONLY (the per-day `news.<date>.<edition>.*` keys the pipelines wrote) and writes ONLY the local
semantic stores under AIMEAT_HOME. Idempotent: an item already remembered (semantic match >= 0.97)
is skipped, so re-running never duplicates.

PREREQUISITES: the fleet's serve daemon must be up (reads ride the loopback tunnel via _aimeat_call)
and an embedder must be reachable (the store is opened required=True — backfilling into no memory is
an error, not a no-op).

    uv run python scripts/backfill_sanomat_memory.py --since 2026-06-01              # editorials
    uv run python scripts/backfill_sanomat_memory.py --since 2026-06-01 --sections   # + all sections
    uv run python scripts/backfill_sanomat_memory.py --dry-run                       # count only
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

EDITORIAL_AGENT = "editorial-writer"
# Section -> the writer crew whose store that section's live wiring uses (must match the pipelines).
_TIDBIT_CATS = ("koodaus", "prompt-niksi", "matikka")  # features_pipeline._TIDBITS


def _section_agents() -> dict[str, str]:
    from crewaimeat.write_pipeline import DESK_A, DESK_B

    m = dict.fromkeys(_TIDBIT_CATS, "daily-features-writer")
    m.update(dict.fromkeys(DESK_A, "news-writer"))
    m.update(dict.fromkeys(DESK_B, "news-writer-b"))
    return m


def _read_key(agent: str, key: str) -> str:
    from crewaimeat.aimeat_crew import _aimeat_call

    r = _aimeat_call(agent, "aimeat_memory_read", {"key": key})
    v = (r or {}).get("value") if isinstance(r, dict) else None
    return v if isinstance(v, str) else ""


def _dates(since: str) -> list[str]:
    d = dt.date.fromisoformat(since)
    out = []
    while d <= dt.date.today():
        out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Seed Sanomat pipeline memories from published history (read-only on the node)."
    )
    ap.add_argument("--since", default="2026-06-01", help="first date to scan (ISO, default 2026-06-01)")
    ap.add_argument("--editions", default="evening", help="comma list of editions (default: evening)")
    ap.add_argument("--sections", action="store_true", help="also backfill section articles (tidbits + desks)")
    ap.add_argument("--dry-run", action="store_true", help="report what WOULD be remembered; write nothing")
    args = ap.parse_args()

    from crewaimeat.pipeline_memory import open_store

    editions = [e.strip() for e in args.editions.split(",") if e.strip()]
    targets: list[tuple[str, str, str]] = [("editorial", EDITORIAL_AGENT, "editorial")]
    if args.sections:
        targets += [(f"article.{cat}", agent, cat) for cat, agent in _section_agents().items()]

    stores: dict[str, object] = {}
    counts = {"found": 0, "remembered": 0, "skipped_dup": 0}
    for date in _dates(args.since):
        for edition in editions:
            for suffix, agent, category in targets:
                key = f"news.{date}.{edition}.{suffix}"
                text = _read_key(agent, key)
                if not text.strip():
                    continue
                counts["found"] += 1
                if args.dry_run:
                    print(f"[backfill] would remember {key} ({len(text)} chars) -> store {agent}/{category}")
                    continue
                store = stores.get(agent)
                if store is None:
                    store = stores[agent] = open_store(agent, required=True)  # no embedder = loud error
                dup = store.dedup_check(text, threshold=0.97, category=category)
                if dup.is_dup:
                    counts["skipped_dup"] += 1
                    print(f"[backfill] {key}: already remembered (score {dup.best_score:.2f}) — skip")
                    continue
                store.remember(
                    text, source="backfill", metadata={"date": date, "edition": edition, "category": category}
                )
                counts["remembered"] += 1
                print(f"[backfill] remembered {key} ({len(text)} chars)")

    print(
        f"[backfill] done: {counts['found']} found, {counts['remembered']} remembered, {counts['skipped_dup']} already present"
    )
    if counts["found"] == 0:
        print(
            "[backfill] WARNING: 0 items found — is the fleet's serve daemon up (reads need the tunnel), "
            "and is --since inside the period the paper has existed?",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()

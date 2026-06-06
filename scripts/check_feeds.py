"""Check the curated feed registry — which feeds still return items. Feeds die over time; run this
periodically and prune/replace the dead ones in src/crewaimeat/feed_sources.py.

    uv run python scripts/check_feeds.py
"""
from crewaimeat.feed_sources import FEED_REGISTRY, _parse_feed

dead = []
for cat, feeds in FEED_REGISTRY.items():
    for f in feeds:
        n = len(_parse_feed(f, 3))
        print(f"{'ok  ' if n else 'DEAD'} {n:2d}  {cat:18s} {f}")
        if not n:
            dead.append((cat, f))
print(f"\n{len(dead)} dead feed(s)" + (":\n  " + "\n  ".join(f"{c}: {u}" for c, u in dead) if dead else ""))

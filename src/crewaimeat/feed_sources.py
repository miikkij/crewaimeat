"""Curated RSS/Atom feed sources per news category — for day-to-day VARIETY.

A keyword search ("tiedeuutiset Suomi") over a month-wide window returns ~the same top results every day,
so evergreen sections repeat. Curated aggregator feeds (ScienceDaily branches, The Guardian sections, YLE,
Hacker News, arXiv, Aeon …) publish fresh items continuously; pulling from them DIRECTLY — rotating through a
category's feeds by date and excluding URLs already used in recent editions — gives genuine daily variety.

`make_feed_tools(agent_name)` returns a `fetch_category_feed` tool the news-fetcher researcher calls per
category (then deepens the URLs with fetch_article_text). Categories with no good feed fall back to Web
Search. The registry is pruned to feeds verified to return items (see scripts/check_feeds.py).
"""

from __future__ import annotations

import datetime

import feedparser  # type: ignore

from crewaimeat.aimeat_crew import _aimeat_call

# category -> ordered list of feed URLs. Rotated by date for variety; dead feeds are skipped at runtime.
FEED_REGISTRY: dict[str, list[str]] = {
    "tiede": [
        "https://www.sciencedaily.com/rss/top/science.xml",
        "https://www.sciencedaily.com/rss/space_time.xml",
        "https://www.sciencedaily.com/rss/matter_energy.xml",
        "https://www.sciencedaily.com/rss/plants_animals.xml",
        "https://www.sciencedaily.com/rss/earth_climate.xml",
        "https://www.sciencedaily.com/rss/computers_math.xml",
        "https://www.sciencedaily.com/rss/strange_offbeat.xml",
        "https://phys.org/rss-feed/",
        "https://www.theguardian.com/science/rss",
    ],
    "tekoaly": [
        "https://hnrss.org/frontpage?points=120",
        "http://export.arxiv.org/rss/cs.AI",
        "https://www.theguardian.com/technology/artificialintelligenceai/rss",
        "https://feeds.arstechnica.com/arstechnica/technology-lab",
    ],
    "talous": [
        "https://www.theguardian.com/business/rss",
        "https://feeds.yle.fi/uutiset/v1/majorHeadlines/YLE_UUTISET.rss",
    ],
    "politiikka-globaali": [
        "https://www.theguardian.com/world/rss",
        "http://feeds.bbci.co.uk/news/world/rss.xml",
    ],
    "politiikka-suomi": [
        "https://feeds.yle.fi/uutiset/v1/majorHeadlines/YLE_UUTISET.rss",
    ],
    "paivankohtaiset": [
        "https://feeds.yle.fi/uutiset/v1/majorHeadlines/YLE_UUTISET.rss",
        "http://feeds.bbci.co.uk/news/rss.xml",
    ],
    "urheilu": [
        "https://www.theguardian.com/sport/rss",
        "http://feeds.bbci.co.uk/sport/rss.xml",
    ],
    "kulttuuri": [
        "https://www.theguardian.com/culture/rss",
        "https://www.theguardian.com/books/rss",
    ],
    "terveys": [
        "https://www.sciencedaily.com/rss/health_medicine.xml",
        "https://www.theguardian.com/society/health/rss",
    ],
    "pelit": [
        "https://www.theguardian.com/games/rss",
        "https://www.eurogamer.net/feed",
        "https://www.rockpapershotgun.com/feed",
    ],
    "pelidevaus": [
        "https://www.gamedeveloper.com/rss.xml",
        "https://hnrss.org/newest?q=game+engine",
    ],
    "startup": [
        "https://techcrunch.com/feed/",
        "https://hnrss.org/frontpage?points=150",
    ],
    "ruoka": [
        "https://www.theguardian.com/food/rss",
        "https://www.bonappetit.com/feed/rss",
    ],
    "luonto": [
        "https://www.theguardian.com/environment/rss",
        "https://www.sciencedaily.com/rss/earth_climate.xml",
        "https://www.sciencedaily.com/rss/plants_animals.xml",
    ],
    "mieli": [
        "https://www.sciencedaily.com/rss/mind_brain.xml",
        "https://www.theguardian.com/lifeandstyle/health-and-wellbeing/rss",
    ],
    "filosofia": [
        "https://aeon.co/feed.rss",
        "https://3quarksdaily.com/feed",
        "https://psyche.co/feed.rss",
    ],
    "kevennykset": [
        "https://www.positive.news/feed/",
        "https://www.goodnewsnetwork.org/feed/",
    ],
}


def _parse_feed(url: str, limit: int = 8) -> list[dict]:
    try:
        d = feedparser.parse(url)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for e in (d.entries or [])[:limit]:
        link = getattr(e, "link", None)
        title = getattr(e, "title", None)
        if not link or not title:
            continue
        summ = getattr(e, "summary", "") or getattr(e, "description", "")
        out.append(
            {
                "title": title.strip(),
                "url": link.strip(),
                "published": getattr(e, "published", "") or getattr(e, "updated", ""),
                "summary": _clean(summ)[:300],
            }
        )
    return out


def _clean(html: str) -> str:
    import re

    return re.sub(r"<[^>]+>", "", html or "").strip()


def _recent_seen_urls(agent_name: str, category: str, limit_keys: int = 3) -> set[str]:
    """URLs already used in the most recent editions for this category — so we don't repeat them."""
    seen: set[str] = set()
    try:
        r = _aimeat_call(agent_name, "aimeat_memory_list", {"owner_scope": True, "prefix": "news."})
        rows = (r or {}).get("items") if isinstance(r, dict) else None
        keys = sorted(
            (it.get("key", "") for it in (rows or []) if it.get("key", "").endswith(".raw." + category)), reverse=True
        )[:limit_keys]
        for k in keys:
            v = (_aimeat_call(agent_name, "aimeat_memory_read", {"key": k}) or {}).get("value")
            if isinstance(v, str) and v.strip()[:1] == "[":
                import json

                try:
                    v = json.loads(v)
                except Exception:  # noqa: BLE001
                    v = []
            for a in v if isinstance(v, list) else []:
                if isinstance(a, dict) and a.get("url"):
                    seen.add(a["url"])
    except Exception:  # noqa: BLE001
        pass
    return seen


def make_feed_tools(agent_name: str) -> list:
    from crewai.tools import tool

    @tool("fetch_category_feed")
    def fetch_category_feed(category: str, max_items: int = 8) -> str:
        """Pull FRESH items for a news category straight from curated RSS feeds (ScienceDaily, The Guardian,
        YLE, Hacker News, arXiv, Aeon, …) — use this BEFORE a keyword Web Search, because it gives real
        day-to-day variety for evergreen topics that a keyword search just repeats. It rotates through the
        category's feeds by today's date and EXCLUDES URLs already used in recent editions. Then deepen the
        returned URLs with fetch_article_text and write the raw. If the category has no curated feed, it says
        so — fall back to Web Search for that one."""
        feeds = FEED_REGISTRY.get(category)
        if not feeds:
            return f"[feed] no curated feed for '{category}'. Use the Web Search tool instead."
        doy = datetime.date.today().timetuple().tm_yday
        n = min(len(feeds), 3)
        chosen = [feeds[(doy + i) % len(feeds)] for i in range(n)]  # rotate window by date
        seen = _recent_seen_urls(agent_name, category)
        items: list[dict] = []
        urls: set[str] = set()
        for f in chosen:
            for it in _parse_feed(f, limit=8):
                if it["url"] in seen or it["url"] in urls:
                    continue
                urls.add(it["url"])
                items.append(it)
        if not items:
            return (
                f"[feed] '{category}': feeds returned no NEW items (all recently used or feeds down). "
                "Use the Web Search tool instead."
            )
        items = items[:max_items]
        lines = [
            f"Fresh curated items for '{category}' from {len(chosen)} feed(s) "
            f"(rotated by date, {len(seen)} recent URLs excluded):",
            "",
        ]
        for i, it in enumerate(items, 1):
            lines.append(f"{i}. {it['title']}\n   URL: {it['url']}\n   {it['summary'] or '(no summary)'}")
        return "\n".join(lines)

    fetch_category_feed.cache_function = lambda *_a, **_k: False  # live network — never cache
    return [fetch_category_feed]

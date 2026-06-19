"""DETERMINISTIC news fetch — no LLM in the loop.

The old fetcher was a CrewAI crew whose researcher agent *decided* whether to call trafilatura; grok skipped
it and stored 1-line RSS snippets. Scraping a page is not a judgement call, so it runs here in plain code:

    for each category:  curated RSS feed (rotated, recent URLs excluded)  ->  if thin, SearXNG keyword search
                        ->  ALWAYS trafilatura (Playwright fallback) for full body  ->  write rich raw

`build_edition_raw(agent_name, date, edition)` writes news.<date>.<edition>.raw.<category> for every
category and returns a per-category report. A weak/empty model can no longer produce stub raw.
"""

from __future__ import annotations

import datetime
import os

import requests

from crewaimeat.aimeat_crew import _aimeat_call
from crewaimeat.article_extract import _MIN_CHARS, _playwright_text, _trafilatura_text
from crewaimeat.feed_sources import FEED_REGISTRY, _parse_feed, _recent_seen_urls

# category -> (keyword query, language, time_range) for categories with no/thin feed (paikallinen/saa…)
CATEGORY_QUERY: dict[str, tuple[str, str, str]] = {
    "talous": ("talous uutiset Suomi", "fi", "day"),
    "paikallinen": ("Tapiola Espoo uutiset", "fi", "week"),
    "saa": ("Suomi sää varoitus helle myrsky rajuilma", "fi", "day"),
    "tiede": ("tiedeuutiset Suomi tutkimus", "fi", "month"),
    "politiikka-suomi": ("Suomi politiikka uutiset", "fi", "day"),
    "politiikka-globaali": ("world politics news today", "en", "day"),
    "paivankohtaiset": ("päivän uutiset Suomi", "fi", "day"),
    "urheilu": ("urheilu uutiset Suomi", "fi", "day"),
    "kulttuuri": ("kulttuuri viihde uutiset Suomi", "fi", "week"),
    "terveys": ("terveys hyvinvointi uutiset Suomi", "fi", "week"),
    "kevennykset": ("positiiviset hyvät uutiset Suomi", "fi", "week"),
    "tekoaly": ("tekoäly AI uutiset", "en", "week"),
    "pelit": ("peliuutiset video game news", "fi", "week"),
    "pelidevaus": ("game development Unity Unreal Godot news", "en", "month"),
    "startup": ("startup uutiset Suomi rahoitus funding", "en", "week"),
    "yliluonnolliset": ("yliluonnolliset ilmiöt kummitukset UFO", "fi", "month"),
    "ruoka": ("ruoka ruokatrendit reseptit Suomi", "fi", "month"),
    "luonto": ("luonto ympäristö eläimet Suomi", "fi", "month"),
    "mieli": ("mielenterveys mieli hyvinvointi psykologia Suomi", "fi", "month"),
    "filosofia": ("filosofia ajattelu etiikka", "fi", "month"),
}
ALL_CATEGORIES = list(CATEGORY_QUERY.keys())


def _searxng_urls(query: str, language: str, time_range: str, n: int = 12) -> list[str]:
    base = os.getenv("SEARXNG_URL", "http://localhost:21333").rstrip("/")
    try:
        r = requests.get(
            base + "/search",
            params={"q": query, "format": "json", "language": language, "time_range": time_range},
            timeout=15,
        )
        return [it.get("url") for it in (r.json().get("results") or []) if it.get("url")][:n]
    except Exception:  # noqa: BLE001
        return []


def _scrape(url: str) -> str:
    txt = _trafilatura_text(url)
    if len(txt) < _MIN_CHARS:
        alt = _playwright_text(url)
        if len(alt) > len(txt):
            txt = alt
    return txt


def build_category_raw(agent_name: str, category: str, date: str, edition: str, max_items: int = 6) -> tuple[int, int]:
    """Feed/search -> ALWAYS trafilatura -> write rich raw for ONE category. Returns (items, total_chars)."""
    seen = _recent_seen_urls(agent_name, category)
    cand: list[dict] = []
    # 1) curated feeds (rotated, recent URLs excluded)
    feeds = FEED_REGISTRY.get(category)
    if feeds:
        doy = datetime.date.today().timetuple().tm_yday
        chosen = [feeds[(doy + i) % len(feeds)] for i in range(min(3, len(feeds)))]
        for f in chosen:
            for it in _parse_feed(f, 8):
                if it["url"] in seen:
                    continue
                cand.append(it)
    # 2) keyword search top-up if thin / no feed
    if len(cand) < max_items:
        q = CATEGORY_QUERY.get(category)
        if q:
            for u in _searxng_urls(*q):
                if u in seen or any(c["url"] == u for c in cand):
                    continue
                cand.append({"title": "", "url": u, "summary": ""})
    # dedup + cap
    chosen_items: list[dict] = []
    used: set = set()
    for c in cand:
        if c["url"] in used:
            continue
        used.add(c["url"])
        chosen_items.append(c)
        if len(chosen_items) >= max_items:
            break
    # 3) ALWAYS scrape full text
    raw: list[dict] = []
    for c in chosen_items:
        body = _scrape(c["url"])
        content = body if body.strip() else (c.get("summary") or "")
        if not content.strip():
            continue
        raw.append(
            {"title": c.get("title") or content.split("\n", 1)[0][:80], "url": c["url"], "content": content[:6000]}
        )
    _aimeat_call(
        agent_name,
        "aimeat_memory_write",
        {"key": f"news.{date}.{edition}.raw.{category}", "value": raw, "visibility": "owner"},
    )
    return len(raw), sum(len(r["content"]) for r in raw)


def build_edition_raw(agent_name: str, date: str, edition: str, categories: list[str] | None = None) -> str:
    cats = categories or ALL_CATEGORIES
    lines = [f"deterministic fetch — {date} {edition}"]
    for c in cats:
        try:
            n, chars = build_category_raw(agent_name, c, date, edition)
            lines.append(f"  {c:18s} items={n:2d} chars={chars}")
        except Exception as e:  # noqa: BLE001
            lines.append(f"  {c:18s} ERROR {type(e).__name__}: {str(e)[:60]}")
    return "\n".join(lines)


def make_fetch_tools(agent_name: str) -> list:
    """A single tool the news-fetcher crew calls ONCE — all the scraping is deterministic inside it."""
    from crewai.tools import tool

    @tool("fetch_edition_raw")
    def fetch_edition_raw(date: str, edition: str) -> str:
        """Deterministically fetch + SCRAPE (trafilatura, always) every news category for date+edition and
        write the rich raw to news.<date>.<edition>.raw.<category>. Call this ONCE with the resolved target
        date and edition — the feeds, search, and full-text scraping all run in code (the LLM never decides
        what to scrape, so raw is never a stub). Returns a per-category items+chars report."""
        return build_edition_raw(agent_name, (date or "").strip(), (edition or "").strip())

    fetch_edition_raw.cache_function = lambda *_a, **_k: False
    return [fetch_edition_raw]

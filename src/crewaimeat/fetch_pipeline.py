"""DETERMINISTIC news fetch — no LLM in the loop.

The old fetcher was a CrewAI crew whose researcher agent *decided* whether to call trafilatura; grok skipped
it and stored 1-line RSS snippets. Scraping a page is not a judgement call, so it runs here in plain code:

    for each category:  curated RSS feed (rotated, recent URLs excluded)  ->  if thin, SearXNG keyword search
                        ->  ALWAYS trafilatura (Playwright fallback) for full body  ->  collect rich raw

`build_edition_raw(agent_name, date, edition)` writes ONE key — news.<date>.<edition>.raw — holding
every category, and returns a per-category report. A weak/empty model can no longer produce stub raw.

ONE KEY, NOT 21. Measured on aimeat.io 2026-08-09: the evening pipeline wrote 44 keys per run and
news.* had grown to 2,993 keys over 68 edition-days with nothing ageing them out. 21 of those 44 were
this step's per-category raw. A memory value holds up to 1024 kB and a day's whole raw measures 457 kB
median / 515 kB at the worst observed day, so the split was a habit, not a requirement. The shipped
per-principal ceiling is 1000 keys (AIMEAT_MEMORY_MAX_KEYS) — aimeat.io runs 100,000, which no other
node does, so at 44 keys/run a default-configured node exhausted itself in 23 edition-days.
"""

from __future__ import annotations

import datetime
import json
import os
import sys

import requests

from crewaimeat.aimeat_crew import _aimeat_call
from crewaimeat.article_extract import _MIN_CHARS, _playwright_text, _trafilatura_doc, _trafilatura_text
from crewaimeat.edition_status import seed_status, step_status
from crewaimeat.feed_sources import FEED_REGISTRY, _parse_feed, _recent_seen_urls, reset_seen_cache

# Raw is scraped SOURCE material: the desks read it during the edition and nobody reads it after the
# paper ships. It was never expirable while it was 21 keys a day (an expiry per category is 21 chances
# to age out half a day's sources); as ONE key it finally is. 14 days is comfortably longer than any
# re-run or backfill window we have ever used, and short enough that raw stops accumulating forever.
# NOTHING ELSE in the edition expires — the articles, editorial, quiz and front-page index ARE the
# published newspaper, and a newspaper that deletes its own back issues is not one.
RAW_TTL_HOURS = 14 * 24

# Loud floor, well under the node's 1024 kB per-value cap. The worst day ever measured is 515 kB, so
# crossing this means something changed in kind (a category exploding, an extractor returning a whole
# site) — and it must be shouted about at 17:00 while there is still an evening to fix it, not
# discovered as a 413 that silently costs an edition.
RAW_WARN_BYTES = 800 * 1024

# How old a DATED source may be and still count as news. Undated sources are not dropped by
# this (see `_is_stale`) — they are marked, because the desks' own prompt must then refuse to
# invent a timeframe for them. 14 days is generous for evergreen categories (filosofia, ruoka)
# while still excluding the years-old landing-page teasers that produced the 2026-08-13 error.
FRESH_DAYS = 14

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


def _scrape_doc(url: str) -> tuple[str, str | None]:
    """Body text plus the page's OWN publication date (None when the page declares none).

    The Playwright fallback renders JS and returns text only, so a page that needs it stays
    undated rather than borrowing a date it never gave."""
    doc = _trafilatura_doc(url)
    txt, date = doc.get("text") or "", doc.get("date")
    if len(txt) < _MIN_CHARS:
        alt = _playwright_text(url)
        if len(alt) > len(txt):
            txt = alt
    return txt, date


def _is_stale(published: str | None, today: datetime.date) -> bool:
    """True when the source states a date and that date is older than the freshness window.

    Undated is NOT stale — it is unknown, and it is handled differently: unknown items are kept
    and marked, because dropping everything undated would empty the categories whose sources are
    landing pages. What must never happen again is a story being DATED by the writer when nothing
    in the source supports it (2026-08-13: a 2024 bridge collapse was published as "viime
    torstaina")."""
    if not published:
        return False
    s = str(published).strip()
    d = None
    try:
        d = datetime.date.fromisoformat(s[:10])
    except ValueError:
        # A source may still hand us RFC-822 ("Wed, 28 Feb 2024 16:16:00 GMT"). Left unparsed it
        # would read as "no date" and sail past this filter — which is exactly how the 2024 bridge
        # story would have come back in through a feed after the first fix.
        try:
            from email.utils import parsedate_to_datetime

            d = parsedate_to_datetime(s).date()
        except (TypeError, ValueError):
            return False
    return (today - d).days > FRESH_DAYS


def fetch_category_raw(agent_name: str, category: str, max_items: int = 6) -> list[dict]:
    """Feed/search -> ALWAYS trafilatura -> the rich raw items for ONE category.

    Pure fetch: it RETURNS the items and writes nothing. The edition's single raw record is assembled
    and written once by `build_edition_raw`, which is what makes one key possible at all."""
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
    # 3) ALWAYS scrape full text — and capture WHEN THE SOURCE SAYS IT WAS PUBLISHED
    raw: list[dict] = []
    today = datetime.date.today()
    for c in chosen_items:
        body, page_date = _scrape_doc(c["url"])
        content = body if body.strip() else (c.get("summary") or "")
        if not content.strip():
            continue
        # The feed's own date wins over the page's: a feed entry states the date of THAT entry,
        # while a scraped page may be a landing page whose metadata describes the site.
        published = (c.get("published") or "").strip() or page_date
        if _is_stale(published, today):
            continue  # provably older than the window — not news, whatever the page looks like
        raw.append(
            {
                "title": c.get("title") or content.split("\n", 1)[0][:80],
                "url": c["url"],
                # WHEN THE SOURCE SAYS IT WAS PUBLISHED, or None when it says nothing. Kept apart
                # from `fetched_at` on purpose: one is the story's age, the other is when we looked,
                # and conflating them is what let a 2024 event be printed as "last Thursday".
                "published_at": published or None,
                # WHEN we actually read it — the article's provenance cites `retrieved_at`, and a
                # timestamp invented at publish time would be a guess about the past. Stamped here,
                # at the only moment that knows it.
                "fetched_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
                "content": content[:6000],
            }
        )
    return raw


def build_edition_raw(agent_name: str, date: str, edition: str, categories: list[str] | None = None) -> str:
    """Scrape every category and write the edition's raw as ONE record:

        news.<date>.<edition>.raw  ->  {fetchedAt, categories: {talous: [...], tiede: [...], ...}}

    The categories live under a `categories` FIELD rather than at the record root so the offer's
    success signal can point at exactly the payload (`count_nonempty` with `path: "categories"`) and
    so metadata can sit beside it without a reader having to know which root keys are categories.

    A category that fails is recorded in the report and SKIPPED — the same resilience as before, one
    bad extractor never costs the other twenty."""
    cats = categories or ALL_CATEGORIES
    key = f"news.{date}.{edition}.raw"
    lines = [f"deterministic fetch — {date} {edition}"]
    # The recent-editions dedup reads whole raw records now, so it caches them for the run — start
    # from a clean cache, or a long-lived daemon answers tonight's fetch out of last night's memory.
    reset_seen_cache()
    # First step of the edition, so this is where the shared status record is born — with every step
    # at "queued", so it says what is still coming and not only what has already happened.
    seed_status(agent_name, date, edition)
    with step_status(agent_name, date, edition, "fetch"):
        by_category: dict[str, list[dict]] = {}
        for c in cats:
            try:
                items = fetch_category_raw(agent_name, c)
            except Exception as e:  # noqa: BLE001
                lines.append(f"  {c:18s} ERROR {type(e).__name__}: {str(e)[:60]}")
                continue
            if items:  # an empty category is left OUT, so `count_nonempty` counts what is really there
                by_category[c] = items
            lines.append(f"  {c:18s} items={len(items):2d} chars={sum(len(i['content']) for i in items)}")

        value = {
            "fetchedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "categories": by_category,
        }
        # Measured in UTF-8 BYTES — what actually travels and what the node's cap counts. Finnish raw is
        # full of two-byte characters, so a character count would under-report the thing being capped.
        size = len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
        if size > RAW_WARN_BYTES:
            print(
                f"[{agent_name}] {key} is {size / 1024:.0f} kB — over the {RAW_WARN_BYTES // 1024} kB floor and "
                f"closing on the node's 1024 kB per-value cap. The write is still attempted; if it 413s, this "
                f"line is the reason. Look for a category returning whole pages instead of articles.",
                file=sys.stderr,
            )
        # PROVENANCE — DELIBERATELY NOT DECLARED ON THIS WRITE, and that is not an oversight.
        # This value is third-party press text a scraper extracted verbatim. No model of ours wrote it,
        # so "ai-generated" is false; but we cannot verify a PERSON wrote the source either (a scraped
        # outlet may itself publish model-written copy), and asserting "original" about someone else's
        # authorship is exactly the generous-direction overstatement that matters.
        #
        # THE LINE, stated so this does not read as contradicting reader_desk/corrections, which DO
        # declare ORIGINAL for text a stranger wrote: there, the author submitted to us through our own
        # channel — a consenting human on our surface, whose act of submitting is the evidence of
        # authorship. A scraped page never addressed us. The test is the consenting-submission surface,
        # not "is this human text".
        #
        # Absence is the DESIGNED answer for verbatim third-party material, not a gap: unstated is a
        # first-class reading, and declaring a level would make US the attestor (stampedBy: principal) of
        # a claim we cannot stand behind. Left undeclared, the node records its own inference instead,
        # clearly marked stampedBy: node / observed: false.
        # The sources ARE stated — one hop downstream on the ARTICLE (write_pipeline._publish_article),
        # which is the honest `synthesized` write and the one a reader actually sees.
        #
        # Consolidating 21 keys into one changes NONE of that: it is the same material, and one
        # undeclared record makes exactly the claim twenty-one undeclared records did.
        written = _aimeat_call(
            agent_name,
            "aimeat_memory_write",
            {"key": key, "value": value, "visibility": "owner", "ttl_hours": RAW_TTL_HOURS},
        )
        if written is None:
            # The desks read this key and nothing else — a lost raw write is the whole edition, so it
            # must go RED here rather than leave the downstream steps to fail one by one.
            raise RuntimeError(f"raw write failed for {key} ({size / 1024:.0f} kB) — the desks have nothing to read")
        lines.append(f"  -> {key}  {len(by_category)} categories, {size / 1024:.0f} kB, ttl {RAW_TTL_HOURS}h")
    return "\n".join(lines)


def make_fetch_tools(agent_name: str) -> list:
    """A single tool the news-fetcher crew calls ONCE — all the scraping is deterministic inside it."""
    from crewai.tools import tool

    @tool("fetch_edition_raw")
    def fetch_edition_raw(date: str, edition: str) -> str:
        """Deterministically fetch + SCRAPE (trafilatura, always) every news category for date+edition and
        write the rich raw to ONE key, news.<date>.<edition>.raw (every category under its `categories`
        field). Call this ONCE with the resolved target date and edition — the feeds, search, and full-text
        scraping all run in code (the LLM never decides what to scrape, so raw is never a stub). Returns a
        per-category items+chars report."""
        return build_edition_raw(agent_name, (date or "").strip(), (edition or "").strip())

    fetch_edition_raw.cache_function = lambda *_a, **_k: False
    return [fetch_edition_raw]

"""Fetch the FULL main text of news articles from SearXNG result URLs — so writers work from real article
bodies, not 1-line search snippets. Trafilatura is the primary extractor (fast, clean main-text); a
Playwright render is the fallback for JS-heavy pages. URLs are deduped by domain so we pull a diverse set
(not N articles from one site). Wire into a crew via:  tools=[*_web_tools(), fetch_article_text]
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

from crewai.tools import tool

_MIN_CHARS = 300  # below this, the trafilatura pass is too thin — try the Playwright fallback


def _domain(u: str) -> str:
    try:
        return urlparse(u).netloc.lower().replace("www.", "")
    except Exception:  # noqa: BLE001
        return u or ""


def _top_domain_diverse(urls: list, n: int) -> list:
    """Top-N URLs from DIFFERENT domains (first occurrence per host wins, ranked order preserved)."""
    seen: set = set()
    out: list = []
    for u in urls:
        if not isinstance(u, str) or not u.startswith("http"):
            continue
        d = _domain(u)
        if d in seen:
            continue
        seen.add(d)
        out.append(u)
        if len(out) >= n:
            break
    return out


def _trafilatura_text(url: str) -> str:
    try:
        import trafilatura
        dl = trafilatura.fetch_url(url)
        if not dl:
            return ""
        return trafilatura.extract(dl, include_comments=False, favor_recall=True) or ""
    except Exception:  # noqa: BLE001
        return ""


def _playwright_text(url: str) -> str:
    """Fallback for JS-heavy pages: render with Playwright, then extract main text from the rendered HTML."""
    try:
        import trafilatura
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                page.goto(url, wait_until="networkidle", timeout=20000)
            except Exception:  # noqa: BLE001
                page.goto(url, timeout=20000)
            page.wait_for_timeout(2500)  # let late JS content settle
            html = page.content()
            browser.close()
        return trafilatura.extract(html, include_comments=False, favor_recall=True) or ""
    except Exception:  # noqa: BLE001
        return ""


@tool("fetch_article_text")
def fetch_article_text(urls_json: str, max_articles: int = 5) -> str:
    """Fetch the FULL main text of news articles from SearXNG result URLs — far richer than 1-line snippets.
    Pass urls_json = a JSON array of the result URLs in ranked order. It selects the top `max_articles` from
    DIFFERENT domains (dedupes by hostname, so never N articles from one site — if fewer domains exist you
    get fewer), extracts each article's main text with trafilatura, and falls back to a Playwright render for
    JS-heavy pages. Returns one block per article (URL + extracted text). Use it AFTER searching a category,
    then store the FULL texts in raw memory so the writer works from real articles, not snippets."""
    try:
        urls = json.loads(urls_json) if isinstance(urls_json, str) else list(urls_json or [])
        if isinstance(urls, dict):  # tolerate {"urls":[...]}
            urls = urls.get("urls") or urls.get("results") or []
    except Exception:  # noqa: BLE001 — tolerate a comma/space/newline separated list
        urls = [u.strip() for u in str(urls_json).replace(",", "\n").split() if u.strip().startswith("http")]
    # tolerate a list of result dicts ([{url,...}])
    urls = [(u.get("url") if isinstance(u, dict) else u) for u in urls]
    picked = _top_domain_diverse([u for u in urls if u], max(1, int(max_articles)))
    if not picked:
        return "No usable article URLs provided (need http(s) URLs from the search results)."
    blocks = []
    for u in picked:
        txt = _trafilatura_text(u)
        if len(txt) < _MIN_CHARS:
            alt = _playwright_text(u)
            if len(alt) > len(txt):
                txt = alt
        if not txt.strip():
            blocks.append(f"=== {u} ===\n(could not extract article text — skip this source)")
        else:
            blocks.append(f"=== {u} ({len(txt)} chars) ===\n{txt[:4000]}")
    domains = len({_domain(u) for u in picked})
    return f"Extracted {len(picked)} articles from {domains} distinct domains:\n\n" + "\n\n".join(blocks)

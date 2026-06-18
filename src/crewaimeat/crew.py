"""Shared domain tool helper.

`_web_tools()` returns the web-search tool a crew should use. Selection is automatic so the same
crew works whether or not any search infrastructure is running:

  1. `USE_TAVILY=1` + `TAVILY_API_KEY`  → Tavily (paid, explicit opt-in).
  2. `WEB_SEARCH=searxng|ddg|tavily`    → force a specific backend, skip auto-detection.
  3. a reachable SearXNG (`SEARXNG_URL`, default `http://localhost:21333`) → SearXNG.
  4. otherwise                          → DuckDuckGo (free, no API key, no server, no Docker).

This means a self-hosted SearXNG is used transparently when present (dev fleet), while a bundled
desktop install with nothing running falls back to DuckDuckGo automatically — zero configuration.
Crews import it as `from crewaimeat.crew import _web_tools` and pass `tools=_web_tools()`.
"""

from __future__ import annotations

import os

# Cache the SearXNG reachability probe for the life of the process — crews are built repeatedly and
# we don't want a network round-trip every time. None = not yet probed.
_SEARXNG_REACHABLE: bool | None = None


def _searxng_reachable() -> bool:
    """One short, cached probe of the SearXNG JSON API. Never raises."""
    global _SEARXNG_REACHABLE
    if _SEARXNG_REACHABLE is not None:
        return _SEARXNG_REACHABLE
    base = os.getenv("SEARXNG_URL", "http://localhost:21333").rstrip("/")
    try:
        import requests

        # A bare GET / is enough to confirm something is listening and speaking HTTP.
        requests.get(base, timeout=1.5)
        _SEARXNG_REACHABLE = True
    except Exception:  # noqa: BLE001 — unreachable / not running / DNS, all mean "fall back"
        _SEARXNG_REACHABLE = False
    return _SEARXNG_REACHABLE


def _web_tools() -> list:
    """Return the web-search tool in a list (see module docstring for selection order).

    Imports are local so a missing optional dependency never breaks this module's import.
    """
    if os.getenv("USE_TAVILY") and os.getenv("TAVILY_API_KEY"):
        from crewai_tools import TavilySearchTool

        return [TavilySearchTool()]

    forced = (os.getenv("WEB_SEARCH") or "").strip().lower()
    if forced == "tavily":
        from crewai_tools import TavilySearchTool

        return [TavilySearchTool()]
    if forced == "searxng":
        from crewaimeat.searxng_search import SearxngSearchTool

        return [SearxngSearchTool()]
    if forced == "ddg":
        from crewaimeat.ddg_search import DdgSearchTool

        return [DdgSearchTool()]

    # Auto-detect: prefer a running SearXNG (richer metasearch), else free DuckDuckGo.
    if _searxng_reachable():
        from crewaimeat.searxng_search import SearxngSearchTool

        return [SearxngSearchTool()]

    from crewaimeat.ddg_search import DdgSearchTool

    return [DdgSearchTool()]


def _browser_tools(profile: str | None = None, allowed_domains: list[str] | None = None) -> list:
    """Return the Playwright browser tool in a list (or [] if playwright isn't installed).

    Pass `profile` to persist login across runs (logs/.browser/<profile>.json); pass `allowed_domains`
    (or set env BROWSER_ALLOWED_DOMAINS) to restrict navigation. The screenshot action can describe the
    page with a vision model (qwen-vl via OpenRouter). Only give this to crews that test/operate web apps.
    Imports are local so a missing optional dependency (playwright) never breaks this module's import.
    """
    try:
        from crewaimeat.browser_tool import PlaywrightBrowserTool
    except Exception:  # noqa: BLE001 — playwright not installed
        return []
    domains = allowed_domains or [d.strip() for d in os.getenv("BROWSER_ALLOWED_DOMAINS", "").split(",") if d.strip()]
    tool = PlaywrightBrowserTool()
    if domains:
        tool.allowed_domains = tuple(domains)
    return [tool]

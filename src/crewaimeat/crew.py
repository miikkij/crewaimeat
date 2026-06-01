"""Shared domain tool helper.

`_web_tools()` returns the web-search tool a crew should use. By default this is the free,
self-hosted SearXNG tool (no API key, no cost) — set `SEARXNG_URL` if your instance is not at the
default `http://localhost:21333`. To use Tavily instead, set `USE_TAVILY=1` and `TAVILY_API_KEY`.
Crews import it as `from crewaimeat.crew import _web_tools` and pass `tools=_web_tools()`.
"""

from __future__ import annotations

import os


def _web_tools() -> list:
    """Return the web-search tool in a list.

    Default: the free self-hosted SearXNG tool. Opt into Tavily with `USE_TAVILY=1` (+ `TAVILY_API_KEY`).
    Imports are local so a missing optional dependency never breaks this module's import.
    """
    if os.getenv("USE_TAVILY") and os.getenv("TAVILY_API_KEY"):
        from crewai_tools import TavilySearchTool

        return [TavilySearchTool()]
    from crewaimeat.searxng_search import SearxngSearchTool

    return [SearxngSearchTool()]

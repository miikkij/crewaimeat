"""Shared domain tool helper.

`_web_tools()` returns the Tavily web-search tool when `TAVILY_API_KEY` is set, and an
empty list otherwise, so an agent can pass `tools=_web_tools()` and work with or without
web search. Crews import it as `from crewaimeat.crew import _web_tools`.
"""

from __future__ import annotations

import os


def _web_tools() -> list:
    """Return the Tavily web-search tool in a list if `TAVILY_API_KEY` is set, else `[]`."""
    if not os.getenv("TAVILY_API_KEY"):
        return []
    # Import here so a missing tavily-python does not break this module's import.
    from crewai_tools import TavilySearchTool

    return [TavilySearchTool()]

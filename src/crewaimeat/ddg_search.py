"""Free web search via DuckDuckGo — a zero-infrastructure drop-in for SearXNG/Tavily.

No API key, no server, no Docker: it queries DuckDuckGo directly through the ``ddgs`` library
(formerly ``duckduckgo-search``). This is the search tool the bundled desktop runtime falls back to
when no SearXNG instance is reachable, so a non-technical user gets working web search out of the box.

Output is byte-for-byte the same shape SearxngSearchTool produces (same ``name = "Web Search"``,
same numbered title/URL/snippet block) so crews need no changes when the backend swaps. On any
failure the tool returns an explanatory message instead of crashing the agent.

Selection is automatic via ``crewaimeat.crew._web_tools()``; you rarely instantiate this directly.
"""

from __future__ import annotations

from crewai.tools import BaseTool
from pydantic import BaseModel, Field


def _ddgs():
    """Import the DDGS client, tolerating the ``duckduckgo-search`` → ``ddgs`` rename."""
    try:
        from ddgs import DDGS  # current package name (>= 6.x)
    except ImportError:  # pragma: no cover — older environments
        from duckduckgo_search import DDGS  # type: ignore[no-redef]
    return DDGS


class DdgSearchInput(BaseModel):
    query: str = Field(..., description="The search query.")
    max_results: int = Field(5, description="Maximum number of results to return.")
    categories: str = Field("general", description="Search category: general or news.")
    language: str = Field("all", description="Language code (e.g. 'en'), or 'all'.")
    time_range: str | None = Field(None, description="Optional recency filter: day, week, month, year.")


class DdgSearchTool(BaseTool):
    """Search the live web through DuckDuckGo (free, no API key, no server)."""

    name: str = "Web Search"
    description: str = (
        "Search the web for up-to-date information (articles, reports, news) on a topic. Returns a "
        "ranked list of results, each with a title, a URL you can cite, and a content snippet. Use it "
        "to find recent, sourced facts."
    )
    args_schema: type[BaseModel] = DdgSearchInput

    # SearXNG recency words → DuckDuckGo single-letter timelimit codes.
    _TIMELIMIT = {"day": "d", "week": "w", "month": "m", "year": "y"}

    def _run(
        self,
        query: str,
        max_results: int = 5,
        categories: str = "general",
        language: str = "all",
        time_range: str | None = None,
    ) -> str:
        timelimit = self._TIMELIMIT.get((time_range or "").lower()) if time_range else None
        # DuckDuckGo has no 'all' region; 'wt-wt' is its worldwide/no-region default.
        region = "wt-wt" if language in ("all", "", None) else language
        try:
            DDGS = _ddgs()
            with DDGS() as ddgs:
                if categories == "news":
                    raw = ddgs.news(query, region=region, timelimit=timelimit, max_results=max_results)
                else:
                    raw = ddgs.text(query, region=region, timelimit=timelimit, max_results=max_results)
            raw = list(raw or [])
        except Exception as exc:  # noqa: BLE001 — never crash the agent on a search failure
            return f"[Web Search error] DuckDuckGo query {query!r} failed: {exc}"

        # Normalise to the same record shape SearxngSearchTool emits. ddgs uses 'href'/'url' for the
        # link and 'body' for the snippet; news() also carries a 'date'.
        results: list[dict] = []
        for item in raw:
            url = item.get("href") or item.get("url")
            title = item.get("title")
            if not url or not title:
                continue
            results.append(
                {
                    "title": title.strip(),
                    "url": url,
                    "content": (item.get("body") or item.get("excerpt") or "").strip(),
                }
            )
            if len(results) >= max_results:
                break

        if not results:
            return f"[Web Search] no results found for {query!r}."

        # Readable, citation-friendly block the agent can use directly — identical to SearXNG's.
        lines = [f'Web search results for "{query}" ({len(results)}):', ""]
        for i, res in enumerate(results, 1):
            snippet = res["content"] or "(no snippet)"
            lines.append(f"{i}. {res['title']}\n   URL: {res['url']}\n   {snippet}")
        return "\n".join(lines)

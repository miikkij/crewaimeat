"""Free web search via a self-hosted SearXNG instance — a drop-in replacement for Tavily.

No API key, no cost: it hits a local SearXNG JSON API (GET /search?format=json). The base URL comes
from the env var ``SEARXNG_URL`` (default ``http://localhost:21333``) so it is portable. On any
failure the tool returns an explanatory message instead of crashing the agent.

Wire it into a crew exactly like any CrewAI tool::

    from crewaimeat.searxng_search import SearxngSearchTool
    agent = Agent(role="...", goal="...", tools=[SearxngSearchTool()])

In this project crews get it automatically via ``crewaimeat.crew._web_tools()`` (SearXNG is the
default; set ``USE_TAVILY=1`` with ``TAVILY_API_KEY`` to fall back to Tavily).
"""

from __future__ import annotations

import os

import requests
from crewai.tools import BaseTool
from pydantic import BaseModel, Field


class SearxngSearchInput(BaseModel):
    query: str = Field(..., description="The search query.")
    max_results: int = Field(5, description="Maximum number of results to return.")
    categories: str = Field("general", description="SearXNG category: general, news, science, ...")
    language: str = Field("all", description="Language code (e.g. 'en'), or 'all'.")
    time_range: str | None = Field(None, description="Optional recency filter: day, week, month, year.")


class SearxngSearchTool(BaseTool):
    """Search the live web through a self-hosted SearXNG instance (free, no API key)."""

    name: str = "Web Search"
    description: str = (
        "Search the web for up-to-date information (articles, reports, news) on a topic. Returns a "
        "ranked list of results, each with a title, a URL you can cite, and a content snippet. Use it "
        "to find recent, sourced facts."
    )
    args_schema: type[BaseModel] = SearxngSearchInput

    def _run(
        self,
        query: str,
        max_results: int = 5,
        categories: str = "general",
        language: str = "all",
        time_range: str | None = None,
    ) -> str:
        base = os.getenv("SEARXNG_URL", "http://localhost:21333").rstrip("/")
        params = {"q": query, "format": "json", "categories": categories, "language": language}
        if time_range:
            params["time_range"] = time_range
        try:
            r = requests.get(f"{base}/search", params=params, timeout=15)
            if r.status_code != 200:
                return f"[Web Search error] SearXNG at {base} returned HTTP {r.status_code} for query {query!r}."
            data = r.json()
        except Exception as exc:  # noqa: BLE001 — never crash the agent on a search failure
            return f"[Web Search error] could not reach SearXNG at {base}: {exc}"

        # Tavily-compatible records; drop anything missing a url or title.
        results: list[dict] = []
        for item in data.get("results", []):
            url, title = item.get("url"), item.get("title")
            if not url or not title:
                continue
            results.append(
                {
                    "title": title.strip(),
                    "url": url,
                    "content": (item.get("content") or "").strip(),
                    "score": item.get("score"),
                }
            )
            if len(results) >= max_results:
                break

        if not results:
            return f"[Web Search] no results found for {query!r}."

        # Readable, citation-friendly block the agent can use directly.
        lines = [f'Web search results for "{query}" ({len(results)}):', ""]
        for i, res in enumerate(results, 1):
            snippet = res["content"] or "(no snippet)"
            lines.append(f"{i}. {res['title']}\n   URL: {res['url']}\n   {snippet}")
        return "\n".join(lines)

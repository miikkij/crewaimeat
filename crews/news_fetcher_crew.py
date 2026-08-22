"""news-fetcher: DETERMINISTIC news fetch for (L)AIMEAT Sanomat.

The actual work (curated feeds + SearXNG search + ALWAYS trafilatura full-text scraping, per category) runs
in plain code — `crewaimeat.fetch_pipeline.build_edition_raw`. Scraping a page is not a judgement call, so it
is NOT left to the LLM: the old crew-driven researcher kept skipping trafilatura and storing 1-line snippets.
This crew is now a thin wrapper: the agent only resolves the target date+edition from the request and calls
`fetch_edition_raw` ONCE; the tool scrapes everything deterministically and writes rich raw — as ONE
`news.<date>.<edition>.raw` record, not one key per category.

Register + approve, then run:
  npx aimeat@latest connect --url https://aimeat.io --owner <you> --agent news-fetcher
  uv run python crews/news_fetcher_crew.py
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.fetch_pipeline import make_fetch_tools

AGENT_NAME = "news-fetcher"

# ── This agent's own declaration ─────────────────────────────────────────────────────────────
# The single source for what this agent is: its model routing, how it is discovered, and what it
# promises. These used to live in three central lists (fleet_identity.py / llm_providers.json /
# offers.py) that nothing kept in step, so an agent could — and did — come online missing from
# all of them. crewaimeat.agent_manifest reads these statically; the lists are derived.
LLM_PROFILE = "content-free"
TAGS = ["news-fetch", "laimeat", "role.task-runner"]
OFFERS = [
    {
        "id": "fetch-edition-raw",
        "title": "Fetch the day's raw news per category",
        "ask": "I pull curated feeds + search and ALWAYS run full-text extraction per category into the edition raw. "
        "Runs on the 17:00 schedule — ask only for a re-fetch. I fetch and extract; I don't write articles.",
        "example": "Re-fetch the evening raw for today",
        "cost": "cheap",
        "latency": "minutes",
        "repeatability": "accumulative",
        "verification": "deterministic",
        "scheduleBorn": "daily 17:00 Europe/Helsinki — runs automatically",
        "consequences": [],
        "sample": "## Edition raw — 2026-06-16 evening (per category)\n"
        "\n"
        "- **talous:** 6 lähdettä, full-text poimittu ✓\n"
        "- **politiikka:** 5 ✓ · **urheilu:** 7 ✓ · **tiede:** 4 ✓ … (20 kategoriaa)\n"
        "\n"
        "Yhteensä 112 artikkelin raakateksti tallennettu YHTEEN avaimeen `news.2026-06-16.evening.raw` "
        "(kategoriat sen `categories`-kentässä). Ei LLM:ää — haku + ekstraktio koodissa.\n"
        "\n"
        "…",
    }
]


README = """[[FIGLET:slant]["News Fetcher"]]

Deterministic Finnish news fetch: curated RSS feeds + SearXNG search + **always-on trafilatura full-text
scraping** per category, written to ONE key — `news.<date>.<edition>.raw`, every category under its
`categories` field. The scraping runs in code, not at the LLM's discretion, so the raw is real article
bodies — never stubs.

**Retention:** the raw key is written with `ttl_hours = 14 days`, so the node expires it by itself — no
prune step to schedule and forget. Raw is scraped SOURCE material: the desks read it while the edition is
being made and nobody reads it afterwards. It only became expirable once it was one key a day; as 21 keys
an expiry was 21 chances to age out half a day's sources mid-run. **Nothing else in the edition expires** —
the articles, editorial, quiz and front-page index are the published newspaper.

**How to task me:** "Hae <date> <edition> uutiset" — I resolve the date/edition and run the deterministic fetch.
"""


def build_domain(ctx: BuildContext):
    fetcher = Agent(
        role="News Fetch Runner",
        goal="Resolve the target date + edition from the request and trigger the deterministic fetch.",
        backstory="You do not scrape by hand or decide what to fetch. You read the request, work out the "
        "target date and edition, and call fetch_edition_raw ONCE — the tool deterministically pulls "
        "feeds, searches, and scrapes full article text for every category. You then report what it "
        "wrote. You never fabricate.",
        llm=ctx.llm,
        tools=[*make_fetch_tools(AGENT_NAME)],
    )

    fetch_task = Task(
        description=(
            f"Today is {ctx.today}. Request: '{ctx.prompt}'\n\n"
            "1. Resolve the TARGET DATE as YYYY-MM-DD (the date stated in the request; if none, use today) and "
            "the EDITION ('evening' if the request mentions ilta/evening, else 'morning').\n"
            "2. Call fetch_edition_raw(date=<resolved date>, edition=<resolved edition>) EXACTLY ONCE. It "
            "deterministically fetches + scrapes (trafilatura) every category and writes the rich raw — you do "
            "NOT need to search or scrape yourself.\n"
            "3. Return the per-category items+chars report the tool gives you."
        ),
        agent=fetcher,
        expected_output="The fetch_edition_raw report: each category's raw key with item count + total chars.",
    )

    return ([fetcher], [fetch_task])


def run() -> None:
    run_crew(CrewSpec(agent_name=AGENT_NAME, build_domain=build_domain, readme_md=README, temperature=0.2))


if __name__ == "__main__":
    run()

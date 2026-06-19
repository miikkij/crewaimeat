"""news-writer (DESK A): deterministic article writing for (L)AIMEAT Sanomat.

The category loop runs in code (crewaimeat.write_pipeline) — every desk-A category with non-empty raw gets a
full-length article written by a direct grok call from the rich scraped raw. The old crew left "which
categories to write" to the LLM, which skipped ~30% and wrote some empty. This crew is a thin wrapper: the
agent resolves the target date+edition and calls write_edition_articles ONCE.

Register + approve, then run:
  npx aimeat@latest connect add --agent news-writer --mode task-runner --url https://aimeat.io --owner <you>
  uv run python crews/news_writer_crew.py
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.write_pipeline import make_write_tools

AGENT_NAME = "news-writer"
README = """[[FIGLET:slant]["News Writer A"]]

Core-news desk (politiikka, talous, paikallinen, kulttuuri, urheilu, tiede, terveys, kevennykset,
päivänkohtaiset). Writes a full Finnish article per category from the scraped raw — deterministic loop, grok
prose, each in its named persona's voice. Runs in parallel with news-writer-b.
"""


def build_domain(ctx: BuildContext):
    writer = Agent(
        role="Desk-A Write Runner",
        goal="Resolve the target date + edition from the request and trigger the deterministic desk-A write.",
        backstory="You do not write articles by hand or choose which to write. You read the request, work out "
        "the target date and edition, and call write_edition_articles ONCE — the tool writes a full "
        "article for every desk-A category that has raw. You then report what it wrote.",
        llm=ctx.llm,
        tools=[*make_write_tools(AGENT_NAME, "A")],
    )
    task = Task(
        description=(
            f"Today is {ctx.today}. Request: '{ctx.prompt}'\n\n"
            "1. Resolve the TARGET DATE (YYYY-MM-DD — the date in the request, else today) and EDITION "
            "('evening' if the request mentions ilta/evening, else 'morning').\n"
            "2. Call write_edition_articles(date=<resolved>, edition=<resolved>) EXACTLY ONCE. It writes a "
            "full article for every desk-A category that has raw — you do NOT write articles yourself.\n"
            "3. Return the per-category char-count report it gives you."
        ),
        agent=writer,
        expected_output="The write_edition_articles report: each desk-A article key + char count, or skips.",
    )
    return ([writer], [task])


def run() -> None:
    run_crew(CrewSpec(agent_name=AGENT_NAME, build_domain=build_domain, readme_md=README, temperature=0.2))


if __name__ == "__main__":
    run()

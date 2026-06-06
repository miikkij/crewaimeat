"""news-writer-b (DESK B): deterministic article writing for (L)AIMEAT Sanomat.

Tech/lifestyle/feature desk (tekoäly, pelit, pelinkehitys, startup, huhut, yliluonnolliset, ruoka, luonto,
mieli, filosofia). The category loop runs in code (crewaimeat.write_pipeline): every desk-B category with
non-empty raw gets a full Finnish article via a direct grok call from the scraped raw, in its named persona's
voice. Thin wrapper around the deterministic writer; runs in parallel with news-writer.

Register + approve, then run:
  npx aimeat@latest connect add --agent news-writer-b --mode task-runner --url https://aimeat.io --owner <you>
  uv run python crews/news_writer_b_crew.py
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.write_pipeline import make_write_tools

AGENT_NAME = "news-writer-b"
README = '''[[FIGLET:slant]["News Writer B"]]

Tech/lifestyle/feature desk (tekoäly, pelit, pelinkehitys, startup, huhut, yliluonnolliset, ruoka, luonto,
mieli, filosofia). Full Finnish article per category from the scraped raw — deterministic loop, grok prose,
named personas. Runs in parallel with news-writer.
'''


def build_domain(ctx: BuildContext):
    writer = Agent(
        role="Desk-B Write Runner",
        goal="Resolve the target date + edition from the request and trigger the deterministic desk-B write.",
        backstory="You do not write articles by hand or choose which to write. You read the request, work out "
                  "the target date and edition, and call write_edition_articles ONCE — the tool writes a full "
                  "article for every desk-B category that has raw. You then report what it wrote.",
        llm=ctx.llm,
        tools=[*make_write_tools(AGENT_NAME, "B")],
    )
    task = Task(
        description=(
            f"Today is {ctx.today}. Request: '{ctx.prompt}'\n\n"
            "1. Resolve the TARGET DATE (YYYY-MM-DD — the date in the request, else today) and EDITION "
            "('evening' if the request mentions ilta/evening, else 'morning').\n"
            "2. Call write_edition_articles(date=<resolved>, edition=<resolved>) EXACTLY ONCE. It writes a "
            "full article for every desk-B category that has raw — you do NOT write articles yourself.\n"
            "3. Return the per-category char-count report it gives you."
        ),
        agent=writer,
        expected_output="The write_edition_articles report: each desk-B article key + char count, or skips.",
    )
    return ([writer], [task])


def run() -> None:
    run_crew(CrewSpec(agent_name=AGENT_NAME, build_domain=build_domain, readme_md=README, temperature=0.2))


if __name__ == "__main__":
    run()

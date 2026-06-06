"""editorial-writer: DETERMINISTIC gonzo editorial + front-page index for (L)AIMEAT Sanomat.

The work runs in code (crewaimeat.editorial_pipeline.build_editorial_and_index): grok writes the savage
Spider-Jerusalem "— S.J." column from the day's article headlines, it is stored VERBATIM (no polite
Publisher rewrite), and the public index is rebuilt with index_frontpage_auto (per-article source counts).
This crew is a thin wrapper: the agent resolves the target date+edition and calls the tool ONCE.

Register + approve, then run:
  npx aimeat@latest connect add --agent editorial-writer --mode task-runner --url https://aimeat.io --owner <you>
  uv run python crews/editorial_writer_crew.py
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.editorial_pipeline import make_editorial_tools

AGENT_NAME = "editorial-writer"
README = '''[[FIGLET:slant]["Editorial"]]

Writes the daily **gonzo S.J. editorial** (savage, provocative, Spider Jerusalem) from the day's articles and
rebuilds the public front-page index (with source counts). Deterministic: grok writes the prose, the column is
stored verbatim, the index is built in code.
'''


def build_domain(ctx: BuildContext):
    editor = Agent(
        role="Editorial Runner",
        goal="Resolve the target date + edition and trigger the deterministic gonzo editorial + index build.",
        backstory="You do not write or rewrite the editorial by hand. You read the request, work out the target "
                  "date and edition, and call write_editorial_and_index ONCE — the tool writes the savage S.J. "
                  "column and rebuilds the public index. You then report what it did.",
        llm=ctx.llm,
        tools=[*make_editorial_tools(AGENT_NAME)],
    )
    task = Task(
        description=(
            f"Today is {ctx.today}. Request: '{ctx.prompt}'\n\n"
            "1. Resolve the TARGET DATE (YYYY-MM-DD — the date in the request, else today) and EDITION "
            "('evening' if the request mentions ilta/evening, else 'morning').\n"
            "2. Call write_editorial_and_index(date=<resolved>, edition=<resolved>) EXACTLY ONCE. It writes the "
            "gonzo S.J. editorial (verbatim) and rebuilds the public front-page index — you do NOT write or "
            "index anything yourself.\n"
            "3. Return the report it gives you (editorial size + index PUBLISHER/INDEX_KEY/counts)."
        ),
        agent=editor,
        expected_output="The write_editorial_and_index report: editorial chars + index PUBLISHER + INDEX_KEY + counts.",
    )
    return ([editor], [task])


def run() -> None:
    run_crew(CrewSpec(agent_name=AGENT_NAME, build_domain=build_domain, readme_md=README, temperature=0.2))


if __name__ == "__main__":
    run()

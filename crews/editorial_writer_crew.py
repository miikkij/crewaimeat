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
    # Self-healing guard (output-existence, no LLM in the check): the 18:00 schedule fires
    # node-side every day, but if THIS daemon is down/restarting right then the task can be
    # lost and the evening edition silently never gets its editorial + front-page index
    # (bit us 2026-06-11). From 18:15 local on: today's editorial key absent AND today's
    # articles present -> run the deterministic stage directly. The key's existence is the
    # dedup, so retries are free and a normally-completed schedule run makes this a no-op.
    def _ensure_today() -> None:
        import datetime
        from zoneinfo import ZoneInfo

        from crewaimeat.aimeat_crew import _aimeat_call
        from crewaimeat.editorial_pipeline import build_editorial_and_index

        now = datetime.datetime.now(ZoneInfo("Europe/Helsinki"))
        if (now.hour, now.minute) < (18, 15):
            return
        date = now.date().isoformat()
        if _aimeat_call(AGENT_NAME, "aimeat_memory_read", {"key": f"news.{date}.evening.editorial"}):
            return
        arts = _aimeat_call(AGENT_NAME, "aimeat_memory_list",
                            {"owner_scope": True, "prefix": f"news.{date}.evening.article.",
                             "limit": 5, "response_format": "concise"}) or {}
        n = len(arts.get("items") or [])
        if n < 3:
            print(f"[{AGENT_NAME}] self-heal: editorial missing but only {n} articles — "
                  f"writers' stage incomplete, not fabricating from nothing", flush=True)
            return
        print(f"[{AGENT_NAME}] self-heal: news.{date}.evening.editorial missing after 18:15 "
              f"-> running the stage", flush=True)
        print(build_editorial_and_index(AGENT_NAME, date, "evening"), flush=True)

    run_crew(CrewSpec(agent_name=AGENT_NAME, build_domain=build_domain, readme_md=README,
                      temperature=0.2, idle_hook=_ensure_today, idle_hook_seconds=300))


if __name__ == "__main__":
    run()

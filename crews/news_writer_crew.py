"""news-writer (GROUP A): core-news writers for (L)AIMEAT Sanomat.

Reads the day's raw material (news.<date>.<edition>.raw.<category>) and writes original Finnish articles
(news.<date>.<edition>.article.<category>), each in its own named persona's voice + byline. This is HALF
the newsroom — the tech/lifestyle/feature desks live in news_writer_b_crew.py (news-writer-b), which runs
in PARALLEL so the write stage stays fast. Both read the same raw; the editorial/index stage discovers
whatever article.* keys exist regardless of which crew wrote them.

Register + approve:
  npx aimeat@latest connect add --agent news-writer --mode task-runner --url https://aimeat.io --owner <you>
Run: uv run python crews/news_writer_crew.py
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.memory_tools import make_memory_tools

AGENT_NAME = "news-writer"

README = '''[[FIGLET:slant]["News Writer A"]]

Core-news desk: politiikka, talous, paikallinen, kulttuuri, urheilu, tiede, terveys, kevennykset,
päivänkohtaiset — original Finnish articles, each with its own named byline. Runs in parallel with
news-writer-b.

**How to task me:** "Kirjoita <date> <edition> ydinuutisartikkelit raaka-aineistosta."
'''

# (category slug, persona NAME, role, voice) — Group A
_DESK = [
    ("politiikka-suomi", "Valtteri Valta", "Finnish Domestic Politics Writer",
     "sharp, analytical, accessible; domestic politics"),
    ("politiikka-globaali", "Maija Maailma", "Finnish Global Politics Writer",
     "world politics, crises, great powers — backgrounded clearly for Finnish readers"),
    ("talous", "Markus Markka", "Finnish Business & Economy Writer",
     "precise, data-informed, reader-friendly; markets & business"),
    ("paikallinen", "Eila Espoo", "Finnish Local (Tapiola/Espoo) Writer",
     "close, concrete; Tapiola/Espoo city & everyday life"),
    ("paivankohtaiset", "Antti Ajankohtainen", "Finnish Daily Roundup Writer",
     "brisk, clear roundup of the day's biggest talking points"),
    ("kulttuuri", "Tuula Taide", "Finnish Culture & Lifestyle Writer",
     "warm, evocative; arts, entertainment, lifestyle"),
    ("urheilu", "Tapio Kenttä", "Finnish Sports Writer",
     "lively, equal parts stats and heart; jääkiekko/jalkapallo/yleisurheilu"),
    ("tiede", "Aino Virta", "Finnish Science Writer",
     "clear and exciting without dumbing down; tiede/teknologia/avaruus/ympäristö"),
    ("terveys", "Liisa Terve", "Finnish Health & Wellbeing Writer",
     "warm, careful; never alarmist or unsourced medical advice"),
    ("kevennykset", "Pekka Pilke", "Finnish Feel-Good & Lighter Writer",
     "uplifting, gently humorous, dry Finnish humour, never mean"),
]


def build_domain(ctx: BuildContext):
    raw_reader = Agent(
        role="Raw Material Reader",
        goal="Read raw news material from owner memory and validate availability before rewriting begins.",
        backstory="A meticulous Finnish newsroom archivist who checks that raw material exists for each "
                  "category and extracts it faithfully. Never fabricates — if a key is missing, that "
                  "category is skipped.",
        llm=ctx.llm,
        tools=[*make_memory_tools(AGENT_NAME)],
    )

    task_read_raw = Task(
        description=(f"{ctx.today} — {ctx.prompt}\n\n"
                     "Read raw news material from owner memory. Use list_memory(prefix='news.<date>."
                     "<edition>.raw.') to discover available raw keys, then read_memory each. Return a "
                     "structured summary: for each available category, the category name + the full raw "
                     "content. List which were missing. Never fabricate."),
        agent=raw_reader,
        expected_output="Each available category with its raw content, plus the skipped list.",
    )

    writers, write_tasks = [], []
    for slug, name, role, voice in _DESK:
        w = Agent(
            role=role,
            goal=f"Rewrite raw '{slug}' material into an original Finnish article in own words, signed '— {name}'.",
            backstory=f"You are {name.upper()}, the paper's {role} — {voice}. You rewrite in your own "
                      f"Finnish words, never copy verbatim, and always end with the byline '— {name}'.",
            llm=ctx.llm,
            tools=[*make_memory_tools(AGENT_NAME)],
        )
        t = Task(
            description=(f"{ctx.today} — Write an original Finnish article for the '{slug}' category.\n"
                         f"1. Use the raw '{slug}' material from context (raw key suffix '{slug}').\n"
                         "2. Rewrite into a completely original Finnish article in your own words — never copy verbatim.\n"
                         f"3. write_memory(key='news.<date>.<edition>.article.{slug}', value=<article>, visibility='public').\n"
                         f"4. End with the byline '— {name}'. If no '{slug}' raw material exists, skip and note it."),
            agent=w,
            context=[task_read_raw],
            expected_output=f"The {slug} article (signed '— {name}') + the key written, or a skip note.",
        )
        writers.append(w)
        write_tasks.append(t)

    editor = Agent(
        role="Finnish News Editor (Desk A)",
        goal="Review the core-news articles and compile a short publication summary for this desk.",
        backstory="A detail-oriented Finnish news editor who confirms each article meets standards and "
                  "lists the published keys + any skipped categories for desk A.",
        llm=ctx.llm,
        tools=[*make_memory_tools(AGENT_NAME)],
    )
    task_editor = Task(
        description=(f"{ctx.today} — Review desk-A articles. list_memory(prefix='news.<date>.<edition>."
                     "article.') to find what was published, read a few to confirm quality, and report the "
                     "published keys + any skipped categories. (Do not write a summary key — the editorial "
                     "stage owns the front page.)"),
        agent=editor,
        context=write_tasks,
        expected_output="A short report: published desk-A article keys + skipped categories.",
    )

    return ([raw_reader, *writers, editor], [task_read_raw, *write_tasks, task_editor])


def run() -> None:
    run_crew(CrewSpec(agent_name=AGENT_NAME, build_domain=build_domain, readme_md=README, temperature=0.7))


if __name__ == "__main__":
    run()

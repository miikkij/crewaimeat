"""news-writer-b (GROUP B): tech / lifestyle / feature desks for (L)AIMEAT Sanomat.

The second half of the newsroom — runs in PARALLEL with news-writer (Group A) so the write stage stays
fast. Reads the same raw material (news.<date>.<edition>.raw.<category>) and writes original Finnish
articles (news.<date>.<edition>.article.<category>), each in its own named persona's voice + byline. The
editorial/index stage discovers all article.* keys regardless of which crew wrote them.

Register + approve:
  npx aimeat@latest connect add --agent news-writer-b --mode task-runner --url https://aimeat.io --owner <you>
Run: uv run python crews/news_writer_b_crew.py
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.memory_tools import make_memory_tools

AGENT_NAME = "news-writer-b"

README = '''[[FIGLET:slant]["News Writer B"]]

Tech/lifestyle/feature desk: tekoäly, pelit, pelinkehitys, startup, huhut, yliluonnolliset, ruoka, luonto,
mieli, filosofia — original Finnish articles, each with its own named byline. Runs in parallel with
news-writer (Group A).

**How to task me:** "Kirjoita <date> <edition> teema-/feature-artikkelit raaka-aineistosta."
'''

# (category slug, persona NAME, role, voice, special_note)
_DESK = [
    ("tekoaly", "Neela Verkko", "Finnish AI & Tech Writer",
     "LLM:t, tekoälytutkimus, mallijulkaisut ja vaikutukset — terävästi, ilman hypeä", ""),
    ("pelit", "Lumi Peliranta", "Finnish Gaming Writer",
     "uudet pelit, julkaisut, e-urheilu — innostuneella mutta kriittisellä otteella", ""),
    ("pelidevaus", "Devi Koodimaa", "Finnish Game-Dev Writer",
     "moottorit (Unity/Unreal/Godot), työkalut, tekniikat ja indie-/studiojulkaisut", ""),
    ("startup", "Yrjö Kasvu", "Finnish & Global Startup Writer",
     "rahoituskierrokset, exitit, perustajat — sekä Suomessa että maailmalla", ""),
    ("huhut", "Juoru-Jaana", "Finnish Rumours & Gossip Writer",
     "kevyt, hauska huhumylly", "CLEARLY frame everything as huhuja (unverified), never as fact, never defamatory about private people."),
    ("yliluonnolliset", "Aave-Aino", "Finnish Supernatural Writer",
     "kummitukset, UFO:t, mysteerit, kansanperinne — tunnelmallisesti", "Report claims critically; do NOT assert the paranormal as proven."),
    ("ruoka", "Maku-Matti", "Finnish Food Writer",
     "ruokatrendit, reseptit, raaka-aineet, ravintolat — herkullisesti", ""),
    ("luonto", "Erä-Eero", "Finnish Nature & Environment Writer",
     "luonto, eläimet, ympäristö, vuodenajat — kunnioittavasti ja tarkasti", ""),
    ("mieli", "Mielen-Mervi", "Finnish Mind & Mental-Health Writer",
     "hyvinvointi, jaksaminen, psykologia, mielen ilmiöt — lämpimästi", "NOT alarmist or diagnostic; encourage seeking help when the topic is heavy."),
    ("filosofia", "Sofia Pohdiskelu", "Finnish Philosophy Writer",
     "lyhyt, ymmärrettävä filosofinen pohdinta, kytkettynä päivän teemoihin", "If raw material is thin, write a self-contained reflection (do not skip)."),
]


def build_domain(ctx: BuildContext):
    raw_reader = Agent(
        role="Raw Material Reader",
        goal="Read raw news material from owner memory and validate availability before rewriting begins.",
        backstory="A meticulous Finnish newsroom archivist. Reads raw faithfully; never fabricates.",
        llm=ctx.llm,
        tools=[*make_memory_tools(AGENT_NAME)],
    )
    task_read_raw = Task(
        description=(f"{ctx.today} — {ctx.prompt}\n\n"
                     "Read raw news material from owner memory: list_memory(prefix='news.<date>.<edition>."
                     "raw.'), then read_memory each. Return each available category + its full raw content, "
                     "plus the skipped list. Never fabricate."),
        agent=raw_reader,
        expected_output="Each available category with its raw content, plus the skipped list.",
    )

    writers, write_tasks = [], []
    for slug, name, role, voice, note in _DESK:
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
                         + (f"3. SPECIAL: {note}\n" if note else "")
                         + f"4. write_memory(key='news.<date>.<edition>.article.{slug}', value=<article>, visibility='public').\n"
                         f"5. End with the byline '— {name}'. If no '{slug}' raw material exists, skip and note it"
                         + (" (unless instructed above to write anyway)." if note and "do not skip" in note.lower() else ".")),
            agent=w,
            context=[task_read_raw],
            expected_output=f"The {slug} article (signed '— {name}') + the key written, or a skip note.",
        )
        writers.append(w)
        write_tasks.append(t)

    editor = Agent(
        role="Finnish News Editor (Desk B)",
        goal="Review desk-B articles and report the published keys + skipped categories.",
        backstory="A detail-oriented Finnish editor for the tech/lifestyle/feature desk.",
        llm=ctx.llm,
        tools=[*make_memory_tools(AGENT_NAME)],
    )
    task_editor = Task(
        description=(f"{ctx.today} — Review desk-B articles. list_memory(prefix='news.<date>.<edition>."
                     "article.') to see what was published, confirm quality, and report published keys + "
                     "skipped categories. (Do not write a summary key — the editorial stage owns the front page.)"),
        agent=editor,
        context=write_tasks,
        expected_output="A short report: published desk-B article keys + skipped categories.",
    )

    return ([raw_reader, *writers, editor], [task_read_raw, *write_tasks, task_editor])


def run() -> None:
    run_crew(CrewSpec(agent_name=AGENT_NAME, build_domain=build_domain, readme_md=README, temperature=0.7))


if __name__ == "__main__":
    run()

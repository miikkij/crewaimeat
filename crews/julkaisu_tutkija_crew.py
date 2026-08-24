"""julkaisu-tutkija: what the OPEN WEB says about the person's order. First step of KANSI.

The person has already read the changelog — that is how they placed the order. This agent's job is
everything the changelog does not contain: who else has written about this problem, what comparable
products do about it by name, why this is the right week to say it, and the strongest case that it
is not interesting at all.

Reads `julkaisu.{ref}.tilaus`, writes `julkaisu.{ref}.tausta`.

**It really does reach the web** — SearXNG when it is up, keyless DuckDuckGo otherwise, both driven
from code (`crewaimeat.julkaisu_brief.web_search`) rather than handed to the model as a tool, so the
URLs stay data. That matters for the one check this step lives or dies by: a finding whose `lahde`
is not one of the pages the search actually returned is REFUSED. A researcher that can invent a
citation is worse than no researcher at all.

`ei_loytynyt` is not optional either. An empty search is a finding, and hiding it is what would make
this step worse than useless.

Register + approve, then restart the fleet:
  npx aimeat@latest connect --url https://aimeat.io --owner <your-aimeat-account> --agent julkaisu-tutkija
Run standalone: uv run python crews/julkaisu_tutkija_crew.py
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.julkaisu_brief import make_tutkija_tools
from crewaimeat.julkaisu_pipeline import KEY_RULE, KEY_RULE_BACKSTORY

AGENT_NAME = "julkaisu-tutkija"

# ── This agent's own declaration ─────────────────────────────────────────────────────────────
# `{ref}` is a workflow VARIABLE and stays literal here; the engine substitutes it per run.
LLM_PROFILE = "news"  # Finnish findings, and judgement about what is worth searching for
TAGS = ["kansi", "julkaisupoyta", "tausta", "verkkohaku", "role.task-runner"]
CAPABILITIES = {
    "technical": [
        {"name": "julkaisu-tutkija", "type": "skill"},
        {"name": "web-search", "type": "tool"},
    ],
    "domain": ["open-web research", "sourced findings", "consumes:julkaisu-tilaus@1", "produces:julkaisu-tausta@1"],
    "languages": ["fi", "en"],
}
OFFERS = [
    {
        "id": "tutki-tausta",
        "title": "Etsi verkosta tausta tilatulle aiheelle",
        "ask": "Haen avoimesta verkosta mitä tästä aiheesta on jo sanottu, mitä vertailtavat tuotteet "
        "tekevät nimeltä, miksi juuri nyt, ja vahvimman vastaväitteen. Jokainen väite kantaa "
        "lähde-URLin, ja käytän vain sivuja jotka haku oikeasti palautti — en keksi osoitetta. "
        "Sen minkä etsin mutta en löytänyt, kerron erikseen. En valitse aihetta enkä julkaise.",
        "example": "Etsi tausta tämän viikon julkaisulle",
        "cost": "cheap",
        "latency": "minutes",
        "tags": ["kansi"],
        "repeatability": "idempotent",
        "verification": "deterministic",
        "dataHandling": "third-party",  # the search queries go to SearXNG / DuckDuckGo
        "json": True,
        "consequences": [],
        "required_to_function": {"kind": "deterministic", "op": "nonempty", "key": "julkaisu.{ref}.tilaus"},
        "success_signal": {
            "kind": "deterministic",
            "op": "count_nonempty",
            "key": "julkaisu.{ref}.tausta",
            "path": "loydokset",
            "min": 2,
        },
        "deliverable_location": {"key": "julkaisu.{ref}.tausta"},
        "sample": {
            "loydokset": [
                {
                    "vaite": "Artikla 50:n läpinäkyvyysvelvoitteet alkavat 2. elokuuta 2026, ja ne koskevat "
                    "agenttia joka asioi ihmisen kanssa.",
                    "lahde": "https://ai-act-service-desk.ec.europa.eu/en/faq",
                    "julkaistu": "2026-06-18",
                    "merkitys": "Oikeuksien kysyminen etukäteen ei ole enää vain hyvää tapaa vaan lähestyvä vaatimus.",
                }
            ],
            "vertailu": [
                {
                    "kuka": "Usercentrics",
                    "mita_tekee": "Myy suostumusinfrastruktuuria ja kirjoittaa samasta pakosta omalle yleisölleen.",
                    "lahde": "https://usercentrics.com/knowledge-hub/eu-ai-act-high-risk-delay-article-50-transparency-consent/",
                }
            ],
            "ajankohtaisuus": "Elokuun 2026 määräpäivä on lähellä ja siitä kirjoitetaan juuri nyt.",
            "vastavaite": "Oikeusvalinta yhdistämishetkellä on pieni käyttöliittymämuutos; kukaan ei vaihda "
            "palvelua sen takia.",
            "ei_loytynyt": "En löytänyt lukua siitä kuinka moni keskeyttää asennuksen oikeuksien kohdalla — "
            "haku palautti mielipiteitä, ei mittausta.",
        },
    }
]

README = """[[FIGLET:slant]["Tutkija"]]

Etsin avoimesta verkosta sen mitä muutosloki ei kerro. Sinä olet jo lukenut merkinnän — minä katson
mitä muut ovat siitä sanoneet.

**Mistä luen:** `julkaisu.<ref>.tilaus` (sinun tilauksesi). **Mihin kirjoitan:**
`julkaisu.<ref>.tausta` — löydökset lähde-URLeineen, nimetyt vertailukohdat, ajankohtaisuus,
vastaväite ja **ei_loytynyt**.

**Jokainen väite kantaa lähteen**, ja lähde on sivu jonka haku oikeasti palautti — se tarkistetaan
koneellisesti, joten en voi keksiä osoitetta. Tyhjä haku on tulos: kerron mitä etsin enkä löytänyt.

Haku kulkee SearXNG:n kautta kun se on pystyssä, muuten DuckDuckGon (kumpikaan ei vaadi avainta).
**En valitse aihetta enkä julkaise mitään.**
"""


def build_domain(ctx: BuildContext):
    researcher = Agent(
        role="Julkaisupöydän tutkija",
        goal="Trigger the deterministic open-web research for this run and report what it found.",
        backstory=KEY_RULE_BACKSTORY
        + "You do not browse by hand and you do not summarise from memory. One tool call plans the "
        "searches, runs them, reads the pages and stores the findings — each tied to a URL the "
        "search actually returned. You call it ONCE and report its result. If it reports FAILED — "
        "no search backend answered, or a finding could not be sourced — you report that failure "
        "as it is. You never supply a source you did not receive from the tool.",
        llm=ctx.llm,
        tools=[*make_tutkija_tools(AGENT_NAME, task=ctx.task, prompt=ctx.prompt)],
    )
    task = Task(
        description=(
            f"Today is {ctx.today}. Request: '{ctx.prompt}'\n\n"
            + KEY_RULE
            + "THIS RUN: you read julkaisu.<id>.tilaus, and you write julkaisu.<id>.tausta.\n\n"
            "1. Call tutki_tausta() EXACTLY ONCE. It takes no arguments: it reads this run's order, "
            "searches the open web, reads the pages it found, and stores the findings with their "
            "sources. You do NOT search or summarise yourself.\n"
            "2. Return its report verbatim — how many findings landed and where, or the FAILED line "
            "and its reason."
        ),
        agent=researcher,
        expected_output="The tutki_tausta report: findings + the memory key written, or the FAILED reason.",
    )
    return ([researcher], [task])


def run() -> None:
    run_crew(CrewSpec(agent_name=AGENT_NAME, build_domain=build_domain, readme_md=README, temperature=0.3))


if __name__ == "__main__":
    run()

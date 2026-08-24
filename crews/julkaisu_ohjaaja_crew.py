"""julkaisu-ohjaaja: several different ways into the same material, for a person to choose from.

Second step of KANSI. It does NOT choose the story — that is the whole point of v3. It lays out as
many angles as the order asked for (1–5), each a different story that could be told from the same
entries and research, and hands them to a person.

Reads `julkaisu.{ref}.tilaus`, `julkaisu.{ref}.tausta` and the node's own `julkaisu.ohjaajat`.
Writes `julkaisu.{ref}.kulmat`.

Each angle carries its **first line actually written** and a **probability** of landing with the
named audience, with the reason for that number. The spread is checked in code: three or more angles
sitting within 15 points of each other is a tell that nothing was judged, and it is handed back.

The director shapes how each angle is framed, per the order's `kaytto` — `full`, `inspired-by`,
`opposite-of` or `blend`. The directors list is READ from the node every run, never copied into this
repo: the person adds directors there.

**"Lisää kulmia" appends.** The angle gate takes two answers, `valittu` and `lisaa`. On `lisaa` this
agent runs again with whatever new instruction the person typed and APPENDS, numbering on from the
highest existing angle — because the person is looking at the first batch in the app, and replacing
it would delete what they were reading.

Register + approve, then restart the fleet:
  npx aimeat@latest connect --url https://aimeat.io --owner <your-aimeat-account> --agent julkaisu-ohjaaja
Run standalone: uv run python crews/julkaisu_ohjaaja_crew.py
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.julkaisu_brief import make_ohjaaja_tools
from crewaimeat.julkaisu_pipeline import KEY_RULE, KEY_RULE_BACKSTORY

AGENT_NAME = "julkaisu-ohjaaja"

# ── This agent's own declaration ─────────────────────────────────────────────────────────────
# `{ref}` is a workflow VARIABLE and stays literal here; the engine substitutes it per run.
LLM_PROFILE = "news"  # Finnish angles, written openings, and a judgement about what lands
TAGS = ["kansi", "julkaisupoyta", "kulmat", "ohjaajatyyli", "role.task-runner"]
CAPABILITIES = {
    "technical": [{"name": "julkaisu-ohjaaja", "type": "skill"}],
    "domain": [
        "editorial angles",
        "director styles",
        "consumes:julkaisu-tausta@1",
        "produces:julkaisu-kulmat@1",
    ],
    "languages": ["fi", "en"],
}
OFFERS = [
    {
        "id": "tee-kulmat",
        "title": "Tarjoa kulmat, joista tilaaja valitsee",
        "ask": "Teen tilatusta aiheesta 1–5 ERILAISTA kulmaa — en saman idean sanamuotoja — ja kirjoitan "
        "kunkin avausrivin valmiiksi. Jokainen kantaa todennäköisyyden ja perustelun, ja arvioin "
        "heikon kulman heikoksi. Luen ohjaajalistan solmulta ja kehystän kulmat tilatun ohjaajan "
        "mukaan. En valitse kulmaa enkä kirjoita valmista tekstiä — ihminen päättää.",
        "example": "Tee viisi kulmaa tämän viikon julkaisusta",
        "cost": "cheap",
        "latency": "minutes",
        "tags": ["kansi"],
        "repeatability": "accumulative",  # "Lisää kulmia" appends a batch, never replaces one
        "verification": "deterministic",
        "dataHandling": "llm-provider",
        "json": True,
        "consequences": [],
        "required_to_function": {
            "kind": "deterministic",
            "op": "count_nonempty",
            "key": "julkaisu.{ref}.tausta",
            "path": "loydokset",
            "min": 1,
        },
        "success_signal": {
            "kind": "deterministic",
            "op": "count_nonempty",
            "key": "julkaisu.{ref}.kulmat",
            "path": "kulmat",
            "min": 1,
        },
        "deliverable_location": {"key": "julkaisu.{ref}.kulmat"},
        "sample": {
            "kulmat": [
                {
                    "nro": 1,
                    "otsikko": "Määräpäivä tekee tästä pakollisen",
                    "kulma": "Elokuussa 2026 oikeuksien kysyminen etukäteen lakkaa olemasta hyvä tapa ja "
                    "muuttuu vaatimukseksi.",
                    "avaus": "Sinulla on yksitoista kuukautta aikaa siihen, että tämä ikkuna on pakko olla.",
                    "miksi_toimii": "Kohdeyleisö rakentaa integraatioita ja reagoi määräpäivään, ei "
                    "käyttöliittymäpäivitykseen.",
                    "kenelle": "integraatioita rakentavat kehittäjät, jotka eivät ole vielä lukeneet artikla 50:tä",
                    "nojaa": "Artikla 50:n läpinäkyvyysvelvoitteet alkavat 2. elokuuta 2026",
                    "todennakoisyys": 74,
                    "perustelu": "Määräpäivä on kova fakta ja lähde on komission oma, mutta aihe on kuiva "
                    "eikä kilpaile tunteesta.",
                    "ohjaaja_ele": "inspired by David Fincher: yksi luku ruudulla, ei mitään muuta",
                    "riski": "Kuulostaa pelottelulta jos määräpäivä on ainoa argumentti.",
                },
                {
                    "nro": 2,
                    "otsikko": "Vastaväite etunenässä",
                    "kulma": "Tämä on pieni käyttöliittymämuutos — paitsi että se on se kohta jossa asennus keskeytyi.",
                    "avaus": "Tiedän mitä ajattelet: yksi valintaikkuna lisää.",
                    "miksi_toimii": "Aloittaa siitä mitä lukija jo ajattelee, jolloin vastaus tuntuu ansaitulta.",
                    "kenelle": "skeptikot jotka ovat nähneet liikaa 'turvallisuusparannuksia'",
                    "nojaa": "changelog",
                    "todennakoisyys": 41,
                    "perustelu": "Vaatii että lukija tunnistaa keskeytyskohdan omakseen; ilman lukua se jää väitteeksi.",
                    "ohjaaja_ele": "inspired by David Fincher: kylmä myönnytys ennen käännettä",
                    "riski": "Jos vastaväite on vahvempi kuin vastaus, teksti myy lukijan pois.",
                },
            ],
            "ohjaaja_luettu": "fincher/inspired-by",
            "notes": "En yrittänyt henkilötarinaa: tausta ei sisältänyt yhtäkään nimettyä käyttäjää, ja "
            "sellaisen keksiminen olisi ollut tekaistu.",
        },
    }
]

README = """[[FIGLET:slant]["Ohjaaja"]]

Tarjoan sinulle kulmat. En valitse niistä — se on sinun työsi.

**Mistä luen:** `julkaisu.<ref>.tilaus`, `julkaisu.<ref>.tausta` ja `julkaisu.ohjaajat` (luen listan
solmulta joka ajolla, joten lisäämäsi ohjaaja on heti käytössä). **Mihin kirjoitan:**
`julkaisu.<ref>.kulmat`.

Teen niin monta kulmaa kuin tilasit (1–5), ja ne ovat **eri tarinoita** — ei viittä sanamuotoa
yhdestä ideasta. Yksi voi lähteä tutkimuslöydöksestä, yksi vastaväitteestä, yksi turhautumisesta,
yksi vertailusta, yksi luvusta. Jokaisessa on **avausrivi kirjoitettuna** ja **todennäköisyys**
perusteluineen. Jos kulma on heikko, annan sille matalan luvun ja sanon miksi — viisi
kahdeksankymppistä olisi merkki siitä ettei mitään arvioitu, ja se hylätään koneellisesti.

**"Lisää kulmia"** tuo uuden erän vanhojen PERÄÄN, numerointi jatkuu — se mitä luet appissa ei katoa.
"""


def build_domain(ctx: BuildContext):
    director = Agent(
        role="Julkaisupöydän ohjaaja",
        goal="Trigger the deterministic angle round for this run and report what was offered.",
        backstory=KEY_RULE_BACKSTORY
        + "You do not choose the angle and you do not write the finished piece — a person does both, "
        "after reading what you offer. One tool call reads the order, the research and the node's "
        "directors list, and stores the angles. You call it ONCE and report its result. If it "
        "reports FAILED — no research yet, an unknown director, or probabilities too flat to be a "
        "real judgement — you report that failure as it is.",
        llm=ctx.llm,
        tools=[*make_ohjaaja_tools(AGENT_NAME, task=ctx.task, prompt=ctx.prompt)],
    )
    task = Task(
        description=(
            f"Today is {ctx.today}. Request: '{ctx.prompt}'\n\n"
            + KEY_RULE
            + "THIS RUN: you read julkaisu.<id>.tilaus and julkaisu.<id>.tausta, and you write "
            "julkaisu.<id>.kulmat.\n\n"
            "1. Call tee_kulmat() EXACTLY ONCE. It takes no arguments: it reads this run's order, its "
            "research and the directors list, and stores the angles — appending if a batch already "
            "exists. You do NOT invent angles yourself and you do NOT pick one.\n"
            "2. Return its report verbatim — how many angles were offered with their probabilities, "
            "or the FAILED line and its reason."
        ),
        agent=director,
        expected_output="The tee_kulmat report: angles offered + the memory key written, or the FAILED reason.",
    )
    return ([director], [task])


def run() -> None:
    run_crew(CrewSpec(agent_name=AGENT_NAME, build_domain=build_domain, readme_md=README, temperature=0.7))


if __name__ == "__main__":
    run()

"""julkaisu-toimittaja: decides what is worth telling, and digs up why. The first step of `julkaisupoyta`.

This is the agent that was missing. Before it, the writers were handed a pre-written summary and the
whole desk was a rephrasing machine. Now the editor does the thinking and the writers only choose
words:

  1. fetches https://aimeat.io/changelog.json itself (public, no auth — a server-side fetch, which is
     the point: the browser is blocked cross-origin, the agent is not),
  2. reads `julkaisu.kerrottu` — what has been told and how each one did — and skips those,
  3. picks ONE entry: not the newest by default, the one after which a reader's own work changes,
  4. DIGS — what did this REPLACE, who was stuck, what is now possible — from the entry body, the
     neighbouring entries, and the node's own llms.txt; what it cannot verify goes in `varmuus`
     instead of being invented,
  5. writes `julkaisu.{ref}.aineisto` and appends the entry to `julkaisu.kerrottu`.

The fetch, the already-told filter, the verbatim title and the field checks are code
(`crewaimeat.julkaisu_desk`). The judgement — which entry matters, and what the story actually is —
is the model's, and it is the only part that is.

Register + approve, then restart the fleet:
  npx aimeat@latest connect --url https://aimeat.io --owner <your-aimeat-account> --agent julkaisu-toimittaja
Run standalone: uv run python crews/julkaisu_toimittaja_crew.py
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.julkaisu_desk import make_toimittaja_tools
from crewaimeat.julkaisu_pipeline import KEY_RULE, KEY_RULE_BACKSTORY

AGENT_NAME = "julkaisu-toimittaja"

# ── This agent's own declaration ─────────────────────────────────────────────────────────────
# `{ref}` is a workflow VARIABLE and stays literal here; the engine substitutes it per run. This
# agent is the one step that may also MINT a ref (from the entry it picked) when the dispatch
# carries none — it is the first step, so there is nothing upstream to have named one.
LLM_PROFILE = "news"  # Finnish judgement + Finnish prose fields — not grok (weak in Finnish)
TAGS = ["julkaisupoyta", "toimittaja", "aiheenvalinta", "role.task-runner"]
CAPABILITIES = {
    "technical": [
        {"name": "julkaisu-toimittaja", "type": "skill"},
        {"name": "changelog-fetch", "type": "tool"},
    ],
    "domain": ["editorial judgement", "release storytelling", "produces:julkaisu-aineisto@1"],
    "languages": ["fi", "en"],
}
OFFERS = [
    {
        "id": "valitse-aihe",
        "title": "Valitse julkaisun aihe ja kaiva siitä tarina",
        "ask": "Luen AIMEATin muutoslokin itse, katson mitä on jo kerrottu, ja valitsen yhden merkinnän "
        "jonka jälkeen lukijan oma työ muuttuu. Kaivan esiin mitä se korvasi ja kuka oli jumissa. "
        "En kirjoita valmista postausta enkä julkaise mitään — teen aineiston, jonka pohjalta "
        "kirjoittajat työskentelevät. Mitä en pysty varmistamaan, sen sanon ääneen enkä keksi.",
        "example": "Valitse tämän viikon julkaisun aihe",
        "cost": "cheap",
        "latency": "minutes",
        "tags": ["julkaisupoyta"],
        "repeatability": "accumulative",
        "verification": "deterministic",
        "dataHandling": "llm-provider",
        "json": True,
        "consequences": [],
        "required_to_function": "none",  # it fetches its own input
        "success_signal": {"kind": "deterministic", "op": "nonempty", "key": "julkaisu.{ref}.aineisto"},
        "deliverable_location": {"key": "julkaisu.{ref}.aineisto"},
        "sample": {
            "valittu": "Decide what your AI may do the moment you connect it",
            "paiva": "2026-08-24",
            "kulma": "Yhteys ei ole enää valmis ennen kuin olet päättänyt, mitä agentti saa tehdä.",
            "ennen": "Yhteys syntyi niillä oikeuksilla jotka agentilla sattui olemaan, ja niiden "
            "muuttaminen vaati Profiili > Agentit -sivun löytämisen ja koko MCP-yhteyden purkamisen.",
            "nyt": "Hyväksymisikkuna kysyy oikeudet ennen kuin yhteys valmistuu: pidä nykyiset, vain "
            "luku, vakio, täydet, tai itse rastitetut.",
            "kenelle": "ihmiset jotka kytkevät claude.ai:n tai ChatGPT:n omaan dataansa ensimmäistä kertaa",
            "todiste": "uudelleenkytkentä katosi kokonaan — se oli oma työvaiheensa pelkän oikeusmuutoksen takia",
            "ei_kerrota": [
                "agentin nimi identiteettinä, jonka alle tekemiset kirjataan",
                "että oikeudet koskevat vain tätä AIMEAT-tiliä",
            ],
            "varmuus": "En löytänyt lukua siitä, kuinka moni keskeytti asennuksen uudelleenkytkennän kohdalla.",
            "lahde": "https://aimeat.io/changelog.json#2026-08-24",
        },
    }
]

README = """[[FIGLET:slant]["Toimittaja"]]

Päättää mistä julkaisupöytä kertoo, ja kaivaa esiin miksi se on tarina.

Luen muutoslokin itse (`https://aimeat.io/changelog.json`), katson `julkaisu.kerrottu`-muistista
mitä on jo kerrottu ja miten se pärjäsi, ja valitsen YHDEN merkinnän — en uusinta oletuksena vaan
sen, jonka jälkeen lukijan oma työ muuttuu. Sitten kaivan: mitä tämä korvasi, kuka oli jumissa,
mikä on nyt mahdollista.

**Mihin kirjoitan:** `julkaisu.<ref>.aineisto` (kulma, ennen, nyt, kenelle, todiste, ei_kerrota,
varmuus) — ja lisään valitun merkinnän `julkaisu.kerrottu`-listaan, jottei seuraava ajo toista sitä.
**En kirjoita postauksia enkä julkaise mitään.** Kirjoittajat lukevat aineiston ja valitsevat sanat.

**Mitä en pysty varmistamaan**, sen kerron kentässä `varmuus` — en keksi ennen-tilaa jota en löytänyt.
"""


def build_domain(ctx: BuildContext):
    editor = Agent(
        role="Julkaisupöydän toimittaja",
        goal="Trigger the deterministic pick-and-dig for this run and report what it chose and why.",
        backstory=KEY_RULE_BACKSTORY
        + "You do not browse, summarise or write by hand. The changelog fetch, the already-told "
        "filter, the choice prompt and the dig are one tool call; you call it ONCE and report its "
        "result. If it reports FAILED — the changelog was unreadable, everything is already told, "
        "or the dig would not meet the contract — you report that failure exactly as it is. You "
        "never write about a changelog entry the tool did not return to you.",
        llm=ctx.llm,
        tools=[*make_toimittaja_tools(AGENT_NAME, task=ctx.task, prompt=ctx.prompt)],
    )
    task = Task(
        description=(
            f"Today is {ctx.today}. Request: '{ctx.prompt}'\n\n"
            + KEY_RULE
            + "THIS RUN: you read the changelog (web) + julkaisu.kerrottu, and you write "
            "julkaisu.<id>.aineisto.\n\n"
            "1. Call valitse_ja_kaiva() EXACTLY ONCE. It takes no arguments: it fetches the public "
            "changelog, skips everything already told, picks the entry worth telling, digs out what it "
            "replaced, and stores this run's aineisto. You do NOT choose or write it yourself.\n"
            "2. Return its report verbatim — the entry it chose, the key it wrote and the angle, or the "
            "FAILED line and its reason."
        ),
        agent=editor,
        expected_output="The valitse_ja_kaiva report: the chosen entry + the memory key + the angle, or the FAILED reason.",
    )
    return ([editor], [task])


def run() -> None:
    run_crew(CrewSpec(agent_name=AGENT_NAME, build_domain=build_domain, readme_md=README, temperature=0.4))


if __name__ == "__main__":
    run()

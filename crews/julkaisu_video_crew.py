"""julkaisu-video: one vertical-video script from one brief. A step of the `julkaisupoyta` workflow.

Reads the brief at `julkaisu.<ref>.brief` and writes `julkaisu.<ref>.video` — an object with `text`
(the script in shots: a line of spoken narration plus a bracketed note on what is on screen) and
`notes` (what it left out and why). It posts nothing anywhere and contacts nobody: the workflow's
human-input gate is where a person picks approve / rewrite / discard.

The crew is a thin wrapper: the run's `ref` is resolved IN CODE from the dispatched task and bound
into the tool, the brief is read and required, and the house rules (9:16, 45–75 s of narration,
every shot carries a bracketed screen note, no stock-footage directions, no logo in the first shot)
are checked deterministically before anything is written. See `crewaimeat.julkaisu_pipeline`.

Register + approve, then restart the fleet:
  npx aimeat@latest connect --url https://aimeat.io --owner <your-aimeat-account> --agent julkaisu-video
Run standalone: uv run python crews/julkaisu_video_crew.py
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.julkaisu_pipeline import make_julkaisu_tools

AGENT_NAME = "julkaisu-video"
CHANNEL = "video"

# ── This agent's own declaration ─────────────────────────────────────────────────────────────
# The single source for what this agent is: its model routing, how it is discovered, and what it
# promises — including the workflow signals, so a `julkaisupoyta` step can name this offer. The
# `{ref}` in the keys is a workflow VARIABLE and stays literal here; the engine substitutes it per
# run. Hardcoding a value there would write every run into the same key.
LLM_PROFILE = "news"  # Finnish prose — the news profile, not grok (weak in Finnish)
TAGS = ["julkaisupoyta", "video-kasikirjoitus", "pystyvideo", "role.task-runner"]
CAPABILITIES = {
    "technical": [{"name": "julkaisu-video", "type": "skill"}],
    "domain": ["vertical video scripts", "product launch copy", "consumes:julkaisu-brief@1"],
    "languages": ["fi"],
}
OFFERS = [
    {
        "id": "kirjoita-video",
        "title": "Kirjoita pystyvideon käsikirjoitus",
        "ask": "Anna minulle avain julkaisu.{ref}.brief, niin kirjoitan siitä yhden pystyvideon (9:16, "
        "45–75 s) käsikirjoituksen kuvina. En tee videota enkä julkaise sitä mihinkään — "
        "käsikirjoitus jää muistiin, ja ihminen päättää mitä sille tehdään.",
        "example": "Kirjoita pystyvideon käsikirjoitus tämän viikon julkaisusta",
        "cost": "cheap",
        "latency": "minutes",
        "tags": ["julkaisupoyta"],
        "repeatability": "idempotent",
        "verification": "deterministic",
        "dataHandling": "llm-provider",
        "json": True,  # the deliverable is an object ({text, notes}); format follows when the node enum has "json"
        "consequences": [],
        "required_to_function": {"kind": "deterministic", "op": "nonempty", "key": "julkaisu.{ref}.brief"},
        "success_signal": {"kind": "deterministic", "op": "nonempty", "key": "julkaisu.{ref}.video"},
        "deliverable_location": {"key": "julkaisu.{ref}.video"},
        "sample": {
            "text": "Yhteys ei ole valmis ennen kuin olet päättänyt, mitä tekoälysi saa tehdä. "
            "[ruutu: hyväksymisikkuna auki, oikeusvalinnat näkyvissä]\n"
            "Ikkuna kysyy sen heti: pidä nykyiset, vain luku, vakio, täydet. "
            "[ruutu: kursori liikkuu vaihtoehtojen yli]\n"
            "Tai rastita itse ne oikeudet, jotka haluat antaa. "
            "[ruutu: rastit menevät päälle yksi kerrallaan]\n"
            "Ennen yhteys syntyi niillä oikeuksilla, jotka agentilla sattui olemaan — ja muuttaminen "
            "vaati koko yhteyden purkamisen. [ruutu: vanha Profiili > Agentit -näkymä]\n"
            "Nyt valinta tehdään kerran, ennen kuin yhteys valmistuu. "
            "[ruutu: valmis yhteys, valitut oikeudet listattuna]",
            "notes": "Jätin pois changelogin päivämäärän ja tuettujen sovellusten listan — 60 sekuntiin "
            "mahtuu yksi muutos, ja lähde on briiffissä.",
        },
    }
]

README = """[[FIGLET:slant]["Julkaisu Video"]]

Kirjoittaa yhdestä julkaisubriiffistä yhden pystyvideon (9:16, 45–75 s) käsikirjoituksen kuvina:
rivi per kuva = puhuttu repliikki + hakasulkeissa mitä ruudussa näkyy. Kolme ensimmäistä sekuntia
kantavat väitteen, ruudussa näkyy jotain mikä on olemassa (ei kuvituskuvaa), ja loppu on se mitä
katsoja toistaisi kollegalleen.

**Mistä luen ja mihin kirjoitan:** briiffi `julkaisu.<ref>.brief` → käsikirjoitus
`julkaisu.<ref>.video` (`text` + `notes`). **En tee videota enkä julkaise mitään** — ihminen
hyväksyy, korjauttaa tai hylkää.

**Miten annat työn:** julkaisupöytä-työnkulku antaa sen itse. Käsin: kerro ajossa mikä `ref` on
(esim. "kirjoita videokäsikirjoitus avaimesta julkaisu.demo1.brief").
"""


def build_domain(ctx: BuildContext):
    writer = Agent(
        role="Vertical Video Script Runner",
        goal="Trigger the deterministic vertical-video script write for this run and report what it wrote.",
        backstory="You do not write the script by hand and you do not choose where it goes. The run's key is "
        "already resolved in code; you call write_julkaisu ONCE and report its result. If it "
        "reports FAILED, you report that failure as it is — you never write a script yourself to "
        "cover for it, and you never claim something was written when it was not.",
        llm=ctx.llm,
        tools=[*make_julkaisu_tools(AGENT_NAME, CHANNEL, task=ctx.task, prompt=ctx.prompt)],
    )
    task = Task(
        description=(
            f"Today is {ctx.today}. Request: '{ctx.prompt}'\n\n"
            "1. Call write_julkaisu() EXACTLY ONCE. It takes no arguments: it reads this run's brief, "
            "writes the Finnish 9:16 script against the house rules, and stores it under this run's "
            "own key. You do NOT write the script yourself.\n"
            "2. Return its report verbatim — the key it wrote and the length, or the FAILED line and "
            "its reason."
        ),
        agent=writer,
        expected_output="The write_julkaisu report: the memory key written + lengths, or the FAILED reason.",
    )
    return ([writer], [task])


def run() -> None:
    run_crew(CrewSpec(agent_name=AGENT_NAME, build_domain=build_domain, readme_md=README, temperature=0.7))


if __name__ == "__main__":
    run()

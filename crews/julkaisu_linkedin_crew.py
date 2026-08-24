"""julkaisu-linkedin: one Finnish LinkedIn post from one brief. A step of the `julkaisupoyta` workflow.

Reads the brief at `julkaisu.<ref>.brief` and writes `julkaisu.<ref>.linkedin` — an object with
`text` (the finished post) and `notes` (what it left out and why). It posts nothing anywhere and
contacts nobody: the workflow's human-input gate is where a person picks approve / rewrite / discard.

The crew is a thin wrapper, as the space-weather crew is: the run's `ref` is resolved IN CODE from
the dispatched task and bound into the tool, the brief is read and required, and the house rules
(600–1200 chars, no hashtag pile, no "olen innoissani", no rhetorical opener) are checked
deterministically before anything is written. See `crewaimeat.julkaisu_pipeline`.

Register + approve, then restart the fleet:
  npx aimeat@latest connect --url https://aimeat.io --owner <your-aimeat-account> --agent julkaisu-linkedin
Run standalone: uv run python crews/julkaisu_linkedin_crew.py
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.julkaisu_pipeline import make_julkaisu_tools

AGENT_NAME = "julkaisu-linkedin"
CHANNEL = "linkedin"

# ── This agent's own declaration ─────────────────────────────────────────────────────────────
# The single source for what this agent is: its model routing, how it is discovered, and what it
# promises — including the workflow signals, so a `julkaisupoyta` step can name this offer. The
# `{ref}` in the keys is a workflow VARIABLE and stays literal here; the engine substitutes it per
# run. Hardcoding a value there would write every run into the same key.
LLM_PROFILE = "news"  # Finnish prose — the news profile, not grok (weak in Finnish)
TAGS = ["julkaisupoyta", "linkedin", "somekirjoitus", "role.task-runner"]
CAPABILITIES = {
    "technical": [{"name": "julkaisu-linkedin", "type": "skill"}],
    "domain": ["LinkedIn posts", "product launch copy", "consumes:julkaisu-brief@1"],
    "languages": ["fi"],
}
OFFERS = [
    {
        "id": "kirjoita-linkedin",
        "title": "Kirjoita LinkedIn-postaus suomeksi",
        "ask": "Anna minulle avain julkaisu.{ref}.brief, niin kirjoitan siitä yhden LinkedIn-postauksen "
        "suomeksi. En julkaise sitä mihinkään enkä ota yhteyttä kehenkään — teksti jää muistiin, ja "
        "ihminen päättää mitä sille tehdään.",
        "example": "Kirjoita LinkedIn-postaus tämän viikon julkaisusta",
        "cost": "cheap",
        "latency": "minutes",
        "tags": ["julkaisupoyta"],
        "repeatability": "idempotent",
        "verification": "deterministic",
        "dataHandling": "llm-provider",
        "json": True,  # the deliverable is an object ({text, notes}); format follows when the node enum has "json"
        "consequences": [],
        "required_to_function": {"kind": "deterministic", "op": "nonempty", "key": "julkaisu.{ref}.brief"},
        "success_signal": {"kind": "deterministic", "op": "nonempty", "key": "julkaisu.{ref}.linkedin"},
        "deliverable_location": {"key": "julkaisu.{ref}.linkedin"},
        "sample": {
            "text": "Yhteys tekoälyn ja oman datan välillä valmistuu nyt vasta kun olet päättänyt, mitä "
            "agentti saa tehdä.\n\nHyväksymisikkuna kysyy sen heti: pidä nykyiset oikeudet, vain luku, "
            "vakio, täydet — tai rastita itse ne, jotka haluat antaa.\n\nAiemmin yhteys syntyi niillä "
            "oikeuksilla, jotka agentilla sattui olemaan, ja muuttaminen vaati Profiili > Agentit "
            "-sivun löytämisen ja koko yhteyden purkamisen. Juuri siihen uusi käyttäjä pysähtyi.\n\n"
            "Valinta tehdään kerran, ennen kuin yhteys on valmis.",
            "notes": "Jätin pois changelogin päivämäärän ja tuotenimien listan — postaus kertoo yhden "
            "muutoksen, ja lähde on briiffissä.",
        },
    }
]

README = """[[FIGLET:slant]["Julkaisu LinkedIn"]]

Kirjoittaa yhdestä julkaisubriiffistä yhden suomenkielisen LinkedIn-postauksen (600–1200 merkkiä):
lukijan hyöty ensimmäiseen kappaleeseen, korkeintaan kaksi aihetunnistetta, ei "olen innoissani"
-aloitusta. Suomi kirjoitetaan suomeksi, ei käännetä englannista.

**Mistä luen ja mihin kirjoitan:** briiffi `julkaisu.<ref>.brief` → postaus `julkaisu.<ref>.linkedin`
(`text` + `notes`). **En julkaise mitään mihinkään** — ihminen hyväksyy, korjauttaa tai hylkää.

**Miten annat työn:** julkaisupöytä-työnkulku antaa sen itse. Käsin: kerro ajossa mikä `ref` on
(esim. "kirjoita LinkedIn-postaus avaimesta julkaisu.demo1.brief").
"""


def build_domain(ctx: BuildContext):
    writer = Agent(
        role="LinkedIn Write Runner",
        goal="Trigger the deterministic LinkedIn write for this run and report what it wrote.",
        backstory="You do not write the post by hand and you do not choose where it goes. The run's key is "
        "already resolved in code; you call write_julkaisu ONCE and report its result. If it "
        "reports FAILED, you report that failure as it is — you never write a post yourself to "
        "cover for it, and you never claim something was written when it was not.",
        llm=ctx.llm,
        tools=[*make_julkaisu_tools(AGENT_NAME, CHANNEL, task=ctx.task, prompt=ctx.prompt)],
    )
    task = Task(
        description=(
            f"Today is {ctx.today}. Request: '{ctx.prompt}'\n\n"
            "1. Call write_julkaisu() EXACTLY ONCE. It takes no arguments: it reads this run's brief, "
            "writes the Finnish LinkedIn post against the house rules, and stores it under this run's "
            "own key. You do NOT write the post yourself.\n"
            "2. Return its report verbatim — the key it wrote and the length, or the FAILED line and "
            "its reason."
        ),
        agent=writer,
        expected_output="The write_julkaisu report: the memory key written + lengths, or the FAILED reason.",
    )
    return ([writer], [task])


def run() -> None:
    run_crew(CrewSpec(agent_name=AGENT_NAME, build_domain=build_domain, readme_md=README, temperature=0.6))


if __name__ == "__main__":
    run()

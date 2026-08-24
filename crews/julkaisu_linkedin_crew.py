"""julkaisu-linkedin: one Finnish LinkedIn post from the editor's angle. A step of `julkaisupoyta`.

Reads `julkaisu.{ref}.aineisto` — the editor's kulma / ennen / nyt / kenelle / todiste — and writes
`julkaisu.{ref}.linkedin` as `{text, notes}`. It writes FROM the angle; it does not restate the
changelog entry, and it never touches anything the editor listed under `ei_kerrota`.

This post leads with the FIX and what it is worth to the reader. The English X thread for the same
story leads with the before-state, so the two are two pieces rather than one text in two languages.

It posts nothing anywhere and contacts nobody: the workflow's human-input gate is where a person
picks approve / rewrite / discard.

The run's `ref` is resolved in code, the aineisto is required, and the house rules (600–1200 chars,
benefit first, at most two hashtags, no "olen innoissani", no rhetorical opener, nothing excluded)
are checked before anything is stored. See `crewaimeat.julkaisu_pipeline`.

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
# `{ref}` is a workflow VARIABLE and stays literal here; the engine substitutes it per run.
LLM_PROFILE = "news"  # Finnish prose — the news profile, not grok (weak in Finnish)
TAGS = ["julkaisupoyta", "linkedin", "somekirjoitus", "role.task-runner"]
CAPABILITIES = {
    "technical": [{"name": "julkaisu-linkedin", "type": "skill"}],
    "domain": ["LinkedIn posts", "release storytelling", "consumes:julkaisu-aineisto@1"],
    "languages": ["fi"],
}
OFFERS = [
    {
        "id": "kirjoita-linkedin",
        "title": "Kirjoita LinkedIn-postaus suomeksi",
        "ask": "Kirjoitan toimittajan aineistosta yhden LinkedIn-postauksen suomeksi: hyöty ensimmäiseen "
        "kappaleeseen, 600–1200 merkkiä, korkeintaan kaksi aihetunnistetta. En referoi "
        "muutosmerkintää enkä kirjoita asioista jotka aineisto on rajannut ulos. En julkaise "
        "postausta mihinkään enkä ota yhteyttä kehenkään — ihminen päättää mitä sille tehdään.",
        "example": "Kirjoita tämän ajon LinkedIn-postaus",
        "cost": "cheap",
        "latency": "minutes",
        "tags": ["julkaisupoyta"],
        "repeatability": "idempotent",
        "verification": "deterministic",
        "dataHandling": "llm-provider",
        "json": True,
        "consequences": [],
        "required_to_function": {"kind": "deterministic", "op": "nonempty", "key": "julkaisu.{ref}.aineisto"},
        "success_signal": {"kind": "deterministic", "op": "nonempty", "key": "julkaisu.{ref}.linkedin"},
        "deliverable_location": {"key": "julkaisu.{ref}.linkedin"},
        "sample": {
            "text": "Yhteys tekoälyn ja oman datan välillä valmistuu nyt vasta kun olet päättänyt, mitä "
            "agentti saa tehdä.\n\nHyväksymisikkuna kysyy sen heti: pidä nykyiset oikeudet, vain luku, "
            "vakio, täydet — tai rastita itse ne, jotka haluat antaa.\n\nAiemmin yhteys syntyi niillä "
            "oikeuksilla, jotka agentilla sattui olemaan, ja muuttaminen vaati Profiili > Agentit "
            "-sivun löytämisen ja koko yhteyden purkamisen. Juuri siihen uusi käyttäjä pysähtyi.\n\n"
            "Valinta tehdään kerran, ennen kuin yhteys on valmis.",
            "notes": "Jätin pois agentin nimen identiteettinä ja oikeuksien tilirajauksen — aineisto "
            "rajasi molemmat ulos tästä tarinasta.",
        },
    }
]

README = """[[FIGLET:slant]["Julkaisu LinkedIn"]]

Kirjoitan toimittajan aineistosta yhden suomenkielisen LinkedIn-postauksen (600–1200 merkkiä).

**Mistä luen:** `julkaisu.<ref>.aineisto` — kulma, ennen, nyt, kenelle, todiste. Kirjoitan NIISTÄ,
en referoi muutosmerkintää. **Mihin kirjoitan:** `julkaisu.<ref>.linkedin` (`text` + `notes`).

Lukijan hyöty ensimmäiseen kappaleeseen. Korkeintaan kaksi aihetunnistetta. Ei "olen innoissani"
-aloitusta, ei retorista kysymystä ensimmäisenä rivinä. Suomi kirjoitetaan suomeksi, ei käännetä.
Aineiston `ei_kerrota`-listan asiat jäävät pois, ja `varmuus`-kentän epävarmuuksia en esitä varmana.

Tämä postaus avaa **korjauksesta**; saman tarinan englanninkielinen X-ketju avaa turhautumisesta.
**En julkaise mitään mihinkään** — ihminen hyväksyy, korjauttaa tai hylkää.
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
            "1. Call write_julkaisu() EXACTLY ONCE. It takes no arguments: it reads the editor's material "
            "for this run, writes the Finnish LinkedIn post against the house rules, and stores it under "
            "this run's own key. You do NOT write the post yourself.\n"
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

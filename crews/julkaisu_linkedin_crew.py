"""julkaisu-linkedin: one Finnish LinkedIn post from the angle A PERSON chose. A step of KANSI.

Reads `julkaisu.{ref}.valinta` (the chosen angle, the director, the style, the picked extras) plus
`julkaisu.{ref}.tausta` (the sourced research), and writes `julkaisu.{ref}.linkedin` as
`{text, notes}`. It writes THAT angle; it does not pick another, and it refuses a gate that answered
"lisaa" — asking for more angles is not permission to choose one.

This post opens on the angle's written first line. The English X thread for the same angle opens on
the tension, so the two are two pieces rather than one text in two languages. The DIRECTOR shapes the
writing too, not only the video: rhythm, sentence length, what is left unsaid.

It posts nothing anywhere and contacts nobody: the workflow's human-input gate is where a person
picks approve / rewrite / discard.

The run's `ref` is resolved in code, the choice is required, and the house rules (600–1200 chars,
benefit first, at most two hashtags, no "olen innoissani", no rhetorical opener, nothing excluded)
are checked before anything is stored. See `crewaimeat.julkaisu_pipeline`.

Register + approve, then restart the fleet:
  npx aimeat@latest connect --url https://aimeat.io --owner <your-aimeat-account> --agent julkaisu-linkedin
Run standalone: uv run python crews/julkaisu_linkedin_crew.py
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.julkaisu_pipeline import KEY_RULE, KEY_RULE_BACKSTORY, make_julkaisu_tools

AGENT_NAME = "julkaisu-linkedin"
CHANNEL = "linkedin"

# ── This agent's own declaration ─────────────────────────────────────────────────────────────
# `{ref}` is a workflow VARIABLE and stays literal here; the engine substitutes it per run.
LLM_PROFILE = "news"  # Finnish prose — the news profile, not grok (weak in Finnish)
TAGS = ["julkaisupoyta", "linkedin", "somekirjoitus", "role.task-runner"]
CAPABILITIES = {
    "technical": [{"name": "julkaisu-linkedin", "type": "skill"}],
    "domain": ["LinkedIn posts", "release storytelling", "consumes:julkaisu-valinta@1"],
    "languages": ["fi"],
}
OFFERS = [
    {
        "id": "kirjoita-linkedin",
        "title": "Kirjoita LinkedIn-postaus suomeksi",
        "ask": "Kirjoitan valitusta kulmasta yhden LinkedIn-postauksen suomeksi tilatun ohjaajan rytmissä: "
        "hyöty ensimmäiseen kappaleeseen, 600–1200 merkkiä, korkeintaan kaksi aihetunnistetta. "
        "En valitse kulmaa itse enkä väitä sitä mitä tausta ei vahvista. En julkaise postausta "
        "mihinkään enkä ota yhteyttä kehenkään — ihminen päättää mitä sille tehdään.",
        "example": "Kirjoita tämän ajon LinkedIn-postaus",
        "cost": "cheap",
        "latency": "minutes",
        "tags": ["julkaisupoyta"],
        "repeatability": "idempotent",
        "verification": "deterministic",
        "dataHandling": "llm-provider",
        "json": True,
        "consequences": [],
        "required_to_function": {"kind": "deterministic", "op": "nonempty", "key": "julkaisu.{ref}.valinta"},
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

Kirjoitan SINUN valitsemastasi kulmasta yhden suomenkielisen LinkedIn-postauksen (600–1200 merkkiä).

**Mistä luen:** `julkaisu.<ref>.valinta` (valittu kulma, ohjaaja, tyyli, poimitut) ja
`julkaisu.<ref>.tausta` (lähteistetty tausta). **Mihin kirjoitan:** `julkaisu.<ref>.linkedin`.

Lukijan hyöty ensimmäiseen kappaleeseen. Korkeintaan kaksi aihetunnistetta. Ei "olen innoissani"
-aloitusta, ei retorista kysymystä ensimmäisenä rivinä. Suomi kirjoitetaan suomeksi, ei käännetä.
Taustan `ei_loytynyt` kertoo mitä ei varmistettu — en esitä sitä varmana.

Tämä postaus avaa **kulman omalla avausrivillä**; saman kulman X-ketju avaa jännitteestä.
**Ohjaaja koskee myös kirjoittamista** — Fincher-postaus ei ole sama kuin Gondry-postaus.
**En julkaise mitään mihinkään** — ihminen hyväksyy, korjauttaa tai hylkää.
"""


def build_domain(ctx: BuildContext):
    writer = Agent(
        role="LinkedIn Write Runner",
        goal="Trigger the deterministic LinkedIn write for this run and report what it wrote.",
        backstory=KEY_RULE_BACKSTORY
        + "You do not write the post by hand and you do not choose where it goes. The run's key is "
        "already resolved in code; you call write_julkaisu ONCE and report its result. If it "
        "reports FAILED, you report that failure as it is — you never write a post yourself to "
        "cover for it, and you never claim something was written when it was not.",
        llm=ctx.llm,
        tools=[*make_julkaisu_tools(AGENT_NAME, CHANNEL, task=ctx.task, prompt=ctx.prompt)],
    )
    task = Task(
        description=(
            f"Today is {ctx.today}. Request: '{ctx.prompt}'\n\n"
            + KEY_RULE
            + "THIS RUN: you read julkaisu.<id>.valinta (the angle a person chose), and you write julkaisu.<id>.linkedin.\n\n"
            "1. Call write_julkaisu() EXACTLY ONCE. It takes no arguments: it reads the angle this run's "
            "person chose plus the research behind it, writes the Finnish LinkedIn post against the house "
            "rules and the ordered director, and stores it under this run's own key. You do NOT write the "
            "post yourself and you do NOT choose a different angle.\n"
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

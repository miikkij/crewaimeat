"""julkaisu-video: a vertical-video SHOT LIST from the angle A PERSON chose. A step of KANSI.

The previous version wrote prose with `[ruutu: …]` glued on the end of each line. That is not a
script — nobody can shoot from it. This one writes `julkaisu.{ref}.video` as a structured shot list:

    {"kesto_s": 58, "muoto": "9:16",
     "kohtaukset": [{"nro", "kesto_s", "kuvakoko", "kuvassa", "liike", "puhe", "ruututeksti", "aani"}],
     "kuvapyynnot": [{"nro", "prompt"}], "text": "…", "notes": "…"}

Every field is checked in code before it is stored: framing and movement and sound come from fixed
vocabularies, no shot runs over 6 s, the total lands in 45–75 s and matches `kesto_s`, `kuvassa`
names something that EXISTS (never a stock-footage direction), the first shot carries the claim
rather than a logo, burned-in text stays under seven words, and `kuvapyynnot` may only ask for an
image where the shot is not a screen recording. See `crewaimeat.julkaisu_pipeline.check_video`.

Those image requests are what `julkaisu-kuva` picks up next.

It posts nothing anywhere and contacts nobody: the workflow's human-input gate is where a person
picks approve / rewrite / discard.

Register + approve, then restart the fleet:
  npx aimeat@latest connect --url https://aimeat.io --owner <your-aimeat-account> --agent julkaisu-video
Run standalone: uv run python crews/julkaisu_video_crew.py
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.julkaisu_pipeline import KEY_RULE, KEY_RULE_BACKSTORY, make_julkaisu_tools

AGENT_NAME = "julkaisu-video"
CHANNEL = "video"

# ── This agent's own declaration ─────────────────────────────────────────────────────────────
# `{ref}` is a workflow VARIABLE and stays literal here; the engine substitutes it per run. The
# success signal counts SCENES inside the record, not the record's existence: a "script" with two
# shots is not a script, and an existence check would call it done.
LLM_PROFILE = "news"  # Finnish spoken lines — the news profile, not grok (weak in Finnish)
TAGS = ["julkaisupoyta", "video-kasikirjoitus", "pystyvideo", "role.task-runner"]
CAPABILITIES = {
    "technical": [{"name": "julkaisu-video", "type": "skill"}],
    "domain": ["vertical video scripts", "shot lists", "consumes:julkaisu-valinta@1", "produces:julkaisu-video@1"],
    "languages": ["fi"],
}
OFFERS = [
    {
        "id": "kirjoita-video",
        "title": "Kirjoita pystyvideon kuvaluettelo",
        "ask": "Kirjoitan valitusta kulmasta pystyvideon (9:16, 45–75 s) kuvaluettelon tilatun ohjaajan "
        "kuvakielellä: jokainen "
        "kohtaus kuvakokona, liikkeenä, repliikkinä, ruututekstinä ja äänenä. Ruudussa näkyy vain "
        "sitä mikä on olemassa — en kirjoita kuvituskuvaohjeita, enkä pyydä generoitua kuvaa "
        "kohtaukseen joka on ruutukaappaus. En tee videota enkä julkaise mitään mihinkään.",
        "example": "Kirjoita tämän ajon videon kuvaluettelo",
        "cost": "cheap",
        "latency": "minutes",
        "tags": ["julkaisupoyta"],
        "repeatability": "idempotent",
        "verification": "deterministic",
        "dataHandling": "llm-provider",
        "json": True,
        "consequences": [],
        "required_to_function": {"kind": "deterministic", "op": "nonempty", "key": "julkaisu.{ref}.valinta"},
        "success_signal": {
            "kind": "deterministic",
            "op": "count_nonempty",
            "key": "julkaisu.{ref}.video",
            "path": "kohtaukset",
            "min": 6,
        },
        "deliverable_location": {"key": "julkaisu.{ref}.video"},
        "sample": {
            "kesto_s": 58,
            "muoto": "9:16",
            "kohtaukset": [
                {
                    "nro": 1,
                    "kesto_s": 3,
                    "kuvakoko": "ruutukaappaus",
                    "kuvassa": "hyväksymisikkuna auki, oikeusvalinnat listattuna",
                    "liike": "still",
                    "puhe": "Yhteys ei ole valmis ennen kuin olet päättänyt, mitä tekoälysi saa tehdä.",
                    "ruututeksti": "Päätä ennen kuin yhdistät",
                    "aani": "puhe",
                },
                {
                    "nro": 2,
                    "kesto_s": 5,
                    "kuvakoko": "ruutukaappaus",
                    "kuvassa": "kursori liikkuu vaihtoehtojen yli ja pysähtyy kohtaan Vakio",
                    "liike": "hidas zoom sisaan",
                    "puhe": "Ikkuna kysyy sen heti: pidä nykyiset, vain luku, vakio tai täydet.",
                    "ruututeksti": "Neljä valmista tasoa",
                    "aani": "puhe",
                },
            ],
            "kuvapyynnot": [
                {
                    "nro": 4,
                    "prompt": "A person at a laptop pausing mid-setup, hand off the trackpad, warm evening "
                    "light, shot from behind the shoulder, vertical framing",
                }
            ],
            "text": "1 (3 s, ruutukaappaus) Yhteys ei ole valmis ennen kuin olet päättänyt… [hyväksymisikkuna auki]\n"
            "2 (5 s, ruutukaappaus) Ikkuna kysyy sen heti… [kursori pysähtyy kohtaan Vakio]",
            "notes": "Jätin pois agentin nimen identiteettinä — aineisto rajasi sen ulos. Kaikki paitsi "
            "kohtaus 4 on ruutukaappaus, joten kuvapyyntöjä on yksi.",
        },
    }
]

README = """[[FIGLET:slant]["Julkaisu Video"]]

Kirjoitan SINUN valitsemastasi kulmasta pystyvideon (9:16, 45–75 s) **kuvaluettelon** — en proosaa,
josta kukaan ei pysty kuvaamaan. Tilattu **ohjaaja** näkyy tässä eniten: kuva, rytmi, väri, ääni.

**Mistä luen:** `julkaisu.<ref>.valinta` ja `julkaisu.<ref>.tausta`. **Mihin kirjoitan:** `julkaisu.<ref>.video`: `kohtaukset`
(nro, kesto, kuvakoko, kuvassa, liike, puhe, ruututeksti, ääni), `kuvapyynnot`, `text` ja `notes`.

Kolme ensimmäistä sekuntia kantavat väitteen, eivät logoa. Yksikään kuva ei ole yli 6 sekuntia.
`kuvassa` nimeää jotain **mikä on olemassa** — oikea näkymä, oikea luku — ei kuvituskuvaa.
Ruututeksti enintään kuusi sanaa. Kuvapyyntö vain niihin kohtauksiin joita ei voi kuvata
ruutukaappauksena; ne menevät `julkaisu-kuva`-agentille.

**En tee videota enkä julkaise mitään** — ihminen hyväksyy, korjauttaa tai hylkää.
"""


def build_domain(ctx: BuildContext):
    writer = Agent(
        role="Vertical Video Shot-List Runner",
        goal="Trigger the deterministic shot-list write for this run and report what it wrote.",
        backstory=KEY_RULE_BACKSTORY
        + "You do not write the script by hand and you do not choose where it goes. The run's key is "
        "already resolved in code; you call write_julkaisu ONCE and report its result. If it "
        "reports FAILED — a shot over six seconds, a stock-footage direction, a total that does "
        "not add up — you report that failure as it is, and you never write a script yourself to "
        "cover for it.",
        llm=ctx.llm,
        tools=[*make_julkaisu_tools(AGENT_NAME, CHANNEL, task=ctx.task, prompt=ctx.prompt)],
    )
    task = Task(
        description=(
            f"Today is {ctx.today}. Request: '{ctx.prompt}'\n\n"
            + KEY_RULE
            + "THIS RUN: you read julkaisu.<id>.valinta (the angle a person chose), and you write julkaisu.<id>.video.\n\n"
            "1. Call write_julkaisu() EXACTLY ONCE. It takes no arguments: it reads the editor's material "
            "for this run, writes the 9:16 shot list against the house rules, and stores it under this "
            "run's own key. You do NOT write the script yourself.\n"
            "2. Return its report verbatim — the key it wrote, how many scenes and image requests it "
            "holds, or the FAILED line and its reason."
        ),
        agent=writer,
        expected_output="The write_julkaisu report: the memory key written + scene/image counts, or the FAILED reason.",
    )
    return ([writer], [task])


def run() -> None:
    run_crew(CrewSpec(agent_name=AGENT_NAME, build_domain=build_domain, readme_md=README, temperature=0.6))


if __name__ == "__main__":
    run()

"""julkaisu-kuva: the images the video script asked for. A step of the `julkaisupoyta` workflow.

Reads `julkaisu.{ref}.video`, takes its `kuvapyynnot` — the shots that cannot be a screen recording
— generates one image per request (Seedream 4.5 via OpenRouter, the same path image-maker uses),
uploads each with visibility "public", and writes `julkaisu.{ref}.kuvat`.

Every image is recorded with BOTH its public URL and its `storage_key`. The app attaches an image to
a published post BY KEY, and a URL alone cannot be attached — so a record with only a link is a
record the app cannot use.

**No model runs in this agent's work at all.** The prompts were written by the script; inventing new
ones here would quietly overrule the person who wrote the shot list. It is a forked, narrowed
image-maker: same generation and upload path, one job, its own workflow signals.

Register + approve, then restart the fleet:
  npx aimeat@latest connect --url https://aimeat.io --owner <your-aimeat-account> --agent julkaisu-kuva
Run standalone: uv run python crews/julkaisu_kuva_crew.py
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.julkaisu_pipeline import KEY_RULE, KEY_RULE_BACKSTORY, make_kuva_tools

AGENT_NAME = "julkaisu-kuva"

# ── This agent's own declaration ─────────────────────────────────────────────────────────────
# `{ref}` is a workflow VARIABLE and stays literal here; the engine substitutes it per run.
LLM_PROFILE = "content"  # the model only has to call one no-argument tool and report the result
TAGS = ["julkaisupoyta", "kuvat", "kuvageneraatio", "role.task-runner"]
CAPABILITIES = {
    "technical": [
        {"name": "julkaisu-kuva", "type": "skill"},
        {"name": "seedream-4-5", "type": "tool"},
    ],
    "domain": ["image generation", "consumes:julkaisu-video@1", "produces:julkaisu-kuvat@1"],
    "languages": ["fi", "en"],
}
OFFERS = [
    {
        "id": "tee-kuvat",
        "title": "Tee videokäsikirjoituksen pyytämät kuvat",
        "ask": "Otan tämän ajon videokäsikirjoituksesta sen kuvapyynnöt, teen yhden kuvan kutakin kohti "
        "ja tallennan ne julkisesti — jokaisen sekä osoitteena että tallennusavaimena, koska "
        "sovellus liittää kuvan avaimella. En keksi omia kuvapyyntöjä enkä tee kuvia niihin "
        "kohtauksiin jotka kuvataan ruutukaappauksena. En julkaise mitään mihinkään.",
        "example": "Tee tämän ajon videon kuvat",
        "cost": "expensive",  # ~$0.04 per image, several per script
        "latency": "minutes",
        "tags": ["julkaisupoyta"],
        "repeatability": "accumulative",
        "verification": "deterministic",
        "dataHandling": "third-party",  # the prompt goes to OpenRouter/Seedream
        "json": True,
        "consequences": [{"type": "publishes-public", "note": "the image files themselves are uploaded public"}],
        # The step needs a SCRIPT to read. How many images that script asks for is the step's own
        # business: as an entry condition it kept a CORRECT run — every shot a screen recording, so
        # no image requests — from ever starting, and reported it as input-red.
        "required_to_function": {
            "kind": "deterministic",
            "op": "exists",
            "key": "julkaisu.{ref}.video",
        },
        # An empty list is a finished run, so the record EXISTING is the signal. The partial case
        # this used to catch (five asked for, one made) is reported by the step itself.
        "success_signal": {
            "kind": "deterministic",
            "op": "exists",
            "key": "julkaisu.{ref}.kuvat",
        },
        "deliverable_location": {"key": "julkaisu.{ref}.kuvat"},
        "sample": {
            "kuvat": [
                {
                    "nro": 4,
                    "url": "https://aimeat.io/v1/pub/julkaisu-kuva%23happydude500001%40aimeat-finland-001-genesis/images/20260824-193000-9f2c1ab4de.png",
                    "storage_key": "images/20260824-193000-9f2c1ab4de.png",
                    "prompt": "A person at a laptop pausing mid-setup, a permissions dialog open on screen, "
                    "warm evening light, shot from behind the shoulder, vertical framing",
                }
            ]
        },
    }
]

README = """[[FIGLET:slant]["Julkaisu Kuva"]]

Teen ne kuvat, jotka videokäsikirjoitus pyysi — en yhtään enempää.

**Mistä luen:** `julkaisu.<ref>.video` → sen `kuvapyynnot` (vain ne kohtaukset joita ei voi kuvata
ruutukaappauksena). **Mihin kirjoitan:** `julkaisu.<ref>.kuvat`, jokainen kuva sekä julkisena
osoitteena että `storage_key`-avaimena — sovellus liittää kuvan avaimella, pelkkä linkki ei riitä.

**En keksi kuvapyyntöjä.** Jos käsikirjoitus ei pyydä yhtään kuvaa, se on käsikirjoituksen päätös ja
kerron sen sellaisenaan. Jos osa kuvista epäonnistuu, tallennan onnistuneet ja sanon mitkä jäivät.

Kuvat maksavat noin 0,04 $ kappale, joten en tee samaa kuvaa kahdesti.
"""


def build_domain(ctx: BuildContext):
    imager = Agent(
        role="Kuvatuottaja",
        goal="Trigger the deterministic image run for this script and report exactly what landed.",
        backstory=KEY_RULE_BACKSTORY
        + "You do not write image prompts and you do not decide which shots need a picture — the "
        "script already did both. You call tee_kuvat ONCE and report its result. Images cost real "
        "money, so you never call it twice to 'try again'. If it reports a partial or FAILED "
        "result you report that, naming the requests that failed.",
        llm=ctx.llm,
        tools=[*make_kuva_tools(AGENT_NAME, task=ctx.task, prompt=ctx.prompt)],
    )
    task = Task(
        description=(
            f"Today is {ctx.today}. Request: '{ctx.prompt}'\n\n"
            + KEY_RULE
            + "THIS RUN: you read julkaisu.<id>.video, and you write julkaisu.<id>.kuvat.\n\n"
            "1. Call tee_kuvat() EXACTLY ONCE. It takes no arguments: it reads this run's video script, "
            "generates one image per kuvapyynto, uploads them public and records each with its URL and "
            "storage key. You do NOT write prompts yourself, and you do NOT call it a second time.\n"
            "2. Return its report verbatim — how many images landed, the key they were written to, and "
            "any request that failed."
        ),
        agent=imager,
        expected_output="The tee_kuvat report: images written + the memory key, or the FAILED reason.",
    )
    return ([imager], [task])


def run() -> None:
    run_crew(CrewSpec(agent_name=AGENT_NAME, build_domain=build_domain, readme_md=README, temperature=0.2))


if __name__ == "__main__":
    run()

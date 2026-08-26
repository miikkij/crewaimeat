"""julkaisu-grok: the shot list turned into Grok Imagine clips. A step of the `julkaisupoyta` chain.

It invents no story. The script already exists (`julkaisu.{ref}.video`); this turns it into clips a
person pastes into Grok Imagine, **with the settings to select there** — mode, length, resolution,
aspect, sound — so nobody has to infer them from the prompt. Writes `julkaisu.{ref}.grok`.

The app can do this as a rule-transform and keeps that as its fallback. What the agent adds is where
a rule cannot reach: camera and sound written as prose, a subject that stays recognisable across
clips, and each clip's own settings with the reason for them.

Four things break the app if they are wrong, so all four are enforced in code
(`crewaimeat.julkaisu_grok`), not merely requested in the prompt:

  1. Every owner read passes `owner_scope=True` (commit 8ad9144) — otherwise the agent cannot see
     what the app wrote.
  2. The output key comes from the dispatch's `deliverable_key` VERBATIM (`run_address`, rule 1).
  3. A clip's id is its shot numbers joined by a hyphen (5 and 6 -> "5-6"). The app matches an
     uploaded video AND the clip's settings on that id, so the GROUPING is done in code — a model
     that merged shots differently on a re-run would rename a clip the person already filled.
  4. A `ruutukaappaus` shot is always `"nauhoita"` with a `nauhoitusohje` and never a prompt, and
     recorded and generated shots never share a clip.

It posts nothing and generates nothing: it prepares, and a person runs Imagine.

Register + approve, then restart the fleet:
  npx aimeat@latest connect --url https://aimeat.io --owner <your-aimeat-account> --agent julkaisu-grok
Run standalone: uv run python crews/julkaisu_grok_crew.py
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.julkaisu_grok import make_grok_tools
from crewaimeat.julkaisu_pipeline import KEY_RULE, KEY_RULE_BACKSTORY

AGENT_NAME = "julkaisu-grok"

# ── This agent's own declaration ─────────────────────────────────────────────────────────────
# `{ref}` is a workflow VARIABLE and stays literal here; the engine substitutes it per run.
LLM_PROFILE = "content"  # the prompts themselves are English film direction
TAGS = ["kansi", "julkaisupoyta", "grok-imagine", "videoklipit", "role.task-runner"]
CAPABILITIES = {
    "technical": [
        {"name": "julkaisu-grok", "type": "skill"},
        {"name": "grok-imagine-prompting", "type": "tool"},
    ],
    "domain": [
        "video prompt writing",
        "shot-to-clip conversion",
        "consumes:julkaisu-video@1",
        "produces:julkaisu-grok@1",
    ],
    "languages": ["fi", "en"],
}
OFFERS = [
    {
        "id": "tee-grok",
        "title": "Käännä kuvaluettelo Grok Imagine -klipeiksi",
        "ask": "Otan valmiin kuvaluettelon ja teen siitä klipit, jotka voit liittää Grok Imagineen "
        "sellaisenaan — mukana tila, kesto, tarkkuus, kuvasuhde ja ääni, valmiiksi valittuina. "
        "Ruutukaappaukset merkitsen nauhoitettaviksi enkä kirjoita niille promptia. En keksi "
        "tarinaa, en generoi videota enkä julkaise mitään — valmistelen, sinä ajat Imaginen.",
        "example": "Tee tämän ajon videosta Grok-klipit",
        "cost": "cheap",
        "latency": "minutes",
        "tags": ["julkaisupoyta"],
        "repeatability": "idempotent",
        "verification": "deterministic",
        "dataHandling": "llm-provider",
        "json": True,
        "consequences": [],
        "required_to_function": {
            "kind": "deterministic",
            "op": "count_nonempty",
            "key": "julkaisu.{ref}.video",
            "path": "kohtaukset",
            "min": 1,
        },
        "success_signal": {
            "kind": "deterministic",
            "op": "count_nonempty",
            "key": "julkaisu.{ref}.grok",
            "path": "klipit",
            "min": 1,
        },
        "deliverable_location": {"key": "julkaisu.{ref}.grok"},
        "sample": {
            "asetukset": {"tarkkuus": "720p", "kuvasuhde": "9:16", "ketjutus": False},
            "klipit": [
                {
                    "id": "5",
                    "kohtaukset": [5],
                    "tyyppi": "generoi",
                    "kesto_s": 6,
                    "grok_kesto_s": 6,
                    "aani": True,
                    "tunnelma": "pimea",
                    "kielto": False,
                    "kuva": "ensimmainen_ruutu",
                    "kuva_url": "https://aimeat.io/v1/pub/…/images/20260826-011659-2b5d055bc2.jpg",
                    "imagine": {
                        "tila": "image-to-video",
                        "kesto": "6s",
                        "tarkkuus": "720p",
                        "kuvasuhde": "9:16",
                        "aani": "paalla",
                        "liite": "Liitä kuva ensimmäiseksi ruuduksi. Se määrää kuvasuhteen, joten älä pakota toista.",
                    },
                    "prompt": "The shadow creeps across the desk as the monitor glow dims. Camera not moving. "
                    "Look: near-dark, one screen as the only light, deep shadow holding most of the frame. "
                    'Sound: narration, low and unhurried: "Darkness is what the browser left undone." '
                    "Room tone under it, no music. Keep the subject, the framing and the horizon unchanged.",
                    "ruututeksti_jalkikateen": [],
                    "miksi": "Kuusi sekuntia riittää yhdelle liikkeelle, ja lyhyempi pitää fysiikan kasassa.",
                },
                {
                    "id": "1-2",
                    "kohtaukset": [1, 2],
                    "tyyppi": "nauhoita",
                    "kesto_s": 9,
                    "grok_kesto_s": 10,
                    "aani": True,
                    "tunnelma": "havainto",
                    "kielto": False,
                    "kuva": "ei",
                    # No `imagine` block: a recording is filmed off a real screen, so there is
                    # nothing to select in Imagine — and a mode written here would be an instruction
                    # to generate the product's own UI, which is the one thing the shot list forbids.
                    "nauhoitusohje": "Avaa pwademo.fi Chromessa. Pidä osoiterivi näkyvissä kolme sekuntia: "
                    "vain kirjanmerkkitähti, ei asennuskuvaketta. Avaa sitten kolmen pisteen valikko ja "
                    "vie hiiri kohtaan 'Save and share' niin että harmaa 'Create shortcut…' näkyy.",
                    "ruututeksti_jalkikateen": ["The browser stopped asking."],
                    "miksi": "Ruutukaappaus nauhoitetaan aina: generoitu käyttöliittymä olisi väärä, ja väärä "
                    "on pahempi kuin ei mitään.",
                },
            ],
        },
    }
]

README = """[[FIGLET:slant]["Grok Skriptaaja"]]

Otan valmiin kuvaluettelon ja teen siitä **klipit, jotka voit liittää Grok Imagineen sellaisenaan**.
En keksi tarinaa — se on jo kirjoitettu.

**Mistä luen:** `julkaisu.<ref>.video` (kohtaukset), `.kuvat` (aloitusruudut), `.valinta` (kulma),
`.tilaus` (tyylit ja kielet). **Mihin kirjoitan:** `julkaisu.<ref>.grok`.

Jokaisessa klipissä on **asetukset valmiina** — tila, kesto, tarkkuus, kuvasuhde, ääni — koska sinun
ei pidä johtaa niitä promptista vaan lukea ne. Mukana `miksi`: yksi lause siitä miksi juuri nämä.

**Ruutukaappaukset nauhoitetaan**, niille kirjoitetaan nauhoitusohje eikä promptia, eikä
nauhoitettavaa ja generoitavaa koskaan yhdistetä samaan klippiin. Nauhoitettavalla ei myöskään ole
`imagine`-asetuksia: sitä ei ajeta Imaginessa lainkaan.

**Puhuttu repliikki on kohtausluettelon oma, sanatarkasti.** Ääni syntyy samassa ajossa kuin kuva,
joten käännetty tai uusiksi muotoiltu repliikki ei ole korjattavissa editissä.

Promptit noudattavat testattuja sääntöjä: teko ensimmäiseen lauseeseen (malli on peräkkäinen, eikä
viimeisen lauseen huippukohta ehdi tapahtua), `Camera not moving.` staattiseen — ei `locked`, joka
luetaan liikkeeksi — `Sound:`-lohko ja `no music` kun puhetta on, eikä koskaan pyyntöä luettavasta
tekstistä ruutuun. Ruututeksti menee kenttään `ruututeksti_jalkikateen` editoria varten.

**En generoi videota enkä julkaise mitään.** Sinä ajat Imaginen.
"""


def build_domain(ctx: BuildContext):
    writer = Agent(
        role="Grok Imagine -skriptaaja",
        goal="Trigger the deterministic clip conversion for this run and report what it produced.",
        backstory=KEY_RULE_BACKSTORY
        + "You do not invent shots and you do not decide the grouping — the script is written and the "
        "clips are grouped in code, because the app joins the person's uploaded video on the clip "
        "id. You call tee_grok ONCE and report its result. If it reports FAILED — no shot list, or "
        "a clip with nothing to paste or shoot — you report that failure as it is.",
        llm=ctx.llm,
        tools=[*make_grok_tools(AGENT_NAME, task=ctx.task, prompt=ctx.prompt)],
    )
    task = Task(
        description=(
            f"Today is {ctx.today}. Request: '{ctx.prompt}'\n\n"
            + KEY_RULE
            + "THIS RUN: you read julkaisu.<id>.video (plus .kuvat, .valinta, .tilaus), and you write "
            "julkaisu.<id>.grok.\n\n"
            "1. Call tee_grok() EXACTLY ONCE. It takes no arguments: it reads this run's shot list, "
            "groups the shots into clips, writes the Imagine prompts and settings, and stores them "
            "under this run's own key. You do NOT write prompts yourself and you do NOT regroup.\n"
            "2. Return its report verbatim — how many clips landed and where, or the FAILED line and "
            "its reason."
        ),
        agent=writer,
        expected_output="The tee_grok report: clips written + the memory key, or the FAILED reason.",
    )
    return ([writer], [task])


def run() -> None:
    run_crew(CrewSpec(agent_name=AGENT_NAME, build_domain=build_domain, readme_md=README, temperature=0.6))


if __name__ == "__main__":
    run()

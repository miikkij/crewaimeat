"""julkaisu-grok: the shot list turned into Grok Imagine clips. A step of the `julkaisupoyta` chain.

It invents no story. The script already exists (`julkaisu.{ref}.video`); this turns it into clips a
person pastes into Grok Imagine, **with the settings to select there** — mode, length, resolution,
aspect, sound — so nobody has to infer them from the prompt. Writes `julkaisu.{ref}.grok`.

The app can do this as a rule-transform and keeps that as its fallback. What the agent adds is where
a rule cannot reach: camera and sound written as prose, a subject that stays recognisable across
clips, and each clip's own settings with the reason for them.

Every generated clip gets FOUR prompts — 8 words to ~3000 characters — because where extra text
stops helping a video model is not measured anywhere. So it is not argued about: the person runs the
same shot on all four and looks. `presetit` are this run's own look, designed from its order, angle
and background, and a preset that would fit any job was not made for this one.

Four things break the app if they are wrong, so all four are enforced in code
(`crewaimeat.julkaisu_grok`), not merely requested in the prompt:

  1. Every owner read passes `owner_scope=True` (commit 8ad9144) — otherwise the agent cannot see
     what the app wrote.
  2. The output key comes from the dispatch's `deliverable_key` VERBATIM (`run_address`, rule 1).
  3. A clip's id is its shot numbers joined by a hyphen (5 and 6 -> "5-6"). The app matches an
     uploaded video AND the clip's settings on that id, so the GROUPING is done in code — a model
     that merged shots differently on a re-run would rename a clip the person already filled.
  4. A `ruutukaappaus` shot is always `"nauhoita"`, carrying an executable `nauhoitus` brief and
     never a prompt, and recorded and generated shots never share a clip.

A recorded clip is a commission something RUNS — the reader is an agent with a browser and a
recorder, not a person — so it names the page, the window size, the starting state and the beats.
The address must exist: an invented one is a dead end, and the shot list is exactly where invented
example addresses come from.

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
        # The node caps `ask` at 500 chars — a real limit, so what it says is what a person needs to
        # decide: what arrives, and where the boundary is. The instruction itself lives in the prompt.
        "ask": "Teen valmiista kuvaluettelosta klipit Grok Imagineen: tila, kesto, tarkkuus, "
        "kuvasuhde ja ääni valmiiksi valittuina. Jokaisesta generoitavasta klipistä NELJÄ "
        "promptiversiota 8 sanasta 3000 merkkiin, jotta näet itse missä lisäteksti lakkaa "
        "auttamasta. Ruutukaappauksista ajettavan nauhoitustoimeksiannon: osoite, ikkunan koko, "
        "alkutila, askeleet. En keksi tarinaa enkä osoitteita, en generoi videota enkä julkaise "
        "mitään — valmistelen, sinä ajat Imaginen.",
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
            "presetit": [
                {
                    "id": "hiljainen-osoiterivi",
                    "nimi": "Hiljainen osoiterivi",
                    "look": "near-dark home office, one monitor as the only light, deep shadow holding most of "
                    "the frame, cold blue-grey cast on the desk edge",
                    "aani": "room tone of an empty flat, a fan somewhere behind the wall, no music",
                    "miksi": "Valittu kulma on hiljaisuus ja se mitä selain jätti tekemättä; pimeys on tämän "
                    "jutun materiaali eikä tunnelmaefekti.",
                }
            ],
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
                    "preset": "hiljainen-osoiterivi",
                    "imagine": {
                        "tila": "image-to-video",
                        "kesto": "6s",
                        "tarkkuus": "720p",
                        "kuvasuhde": "9:16",
                        "aani": "paalla",
                        "liite": "Liitä kuva ensimmäiseksi ruuduksi. Se määrää kuvasuhteen, joten älä pakota toista.",
                    },
                    # Four versions of the SAME shot, 8 words to ~3000 characters. Not candidates to
                    # choose between: the person runs all four and sees where the extra text stops
                    # helping. The long ones add what the frame does NOT show.
                    "promptit": {
                        "lyhyt": "The shadow creeps across the desk as the glow dims. Camera not moving. "
                        "Room tone, no music.",
                        "keskiko": "The shadow creeps across the desk as the monitor glow dims. Camera not "
                        "moving. Look: near-dark, one screen as the only light, deep shadow holding most of "
                        'the frame. Sound: narration, low and unhurried: "Darkness is what the browser left '
                        'undone." Room tone under it, no music. Keep the subject, the framing and the horizon '
                        "unchanged.",
                        "laaja": "…1000–1500 merkkiä samasta kohtauksesta: materiaalit, valon suunta ja laatu, "
                        "mitä taustalla tapahtuu, äänen kerrokset, mikä ei saa muuttua…",
                        "massiivinen": "…noin 3000 merkkiä samasta kohtauksesta, niin paljon kuin siitä on sanottavaa…",
                    },
                    "ruututeksti_jalkikateen": [],
                    "miksi": "Kuusi sekuntia riittää yhdelle liikkeelle, ja lyhyempi pitää fysiikan kasassa.",
                },
                {
                    "id": "1-2",
                    "kohtaukset": [1, 2],
                    "tyyppi": "nauhoita",
                    "kesto_s": 9,
                    # No `grok_kesto_s`: Grok never generates this clip, so its length is `kesto_s`.
                    # No `imagine` block either — there is nothing to select for a shot filmed off a
                    # real screen, and a mode here would read as an instruction to generate the
                    # product's own UI, the one thing the shot list forbids.
                    "aani": True,
                    "tunnelma": "havainto",
                    "kielto": False,
                    "kuva": "ei",
                    # An executable commission, not a description: the reader is an agent with a
                    # browser and a recorder, so it gets the page, the size, the starting state, the
                    # beats and the format — everything it cannot infer.
                    "nauhoitus": {
                        "url": "https://aimeat.io",
                        "viewport": {"w": 1080, "h": 1920},
                        "esivalmistelu": "Avaa sivu ja odota kunnes se on latautunut. Varmista ettei "
                        "asennuskehote ole näkyvissä.",
                        "askeleet": [
                            {
                                "t": "0-3",
                                "tee": "Pidä osoiterivi paikallaan: vain kirjanmerkkitähti, ei asennuskuvaketta",
                            },
                            {"t": "3-5", "tee": "Avaa kolmen pisteen valikko"},
                            {"t": "5-7", "tee": "Vie osoitin riville 'Save and share', pysähdy"},
                            {"t": "7-9", "tee": "Zoomaa hitaasti harmaaseen riviin 'Create shortcut…'"},
                        ],
                        "kesto_s": 9,
                        "muoto": "webm tai mp4, yksi jatkuva nauhoitus",
                        "huom": "Älä klikkaa mitään. Ruututeksti lisätään jälkikäteen editorissa.",
                    },
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

**Jokaisesta generoitavasta klipistä tulee neljä promptia**, ei yhtä: `lyhyt` (8–25 sanaa),
`keskiko` (20–60 sanaa), `laaja` (1000–1500 merkkiä) ja `massiivinen` (~3000 merkkiä). Ne eivät ole
vaihtoehtoja joista valitset parhaan, vaan **koe**: ajat saman kohtauksen kaikilla neljällä ja näet
omin silmin missä kohtaa lisäteksti alkaa auttaa ja missä se alkaa hukuttaa. Sitä rajaa ei ole
kenelläkään mitattuna. Pitkissä versioissa lisätään sitä mitä kuvassa **ei näy** — materiaali, ilma,
äänen kerrokset, mikä pysyy paikallaan — koska muuten 3000 merkkiä olisi sama lause neljästi.

**`presetit` suunnitellaan tälle työlle** sen tilauksesta, kulmasta ja taustasta: `look`, `aani` ja
yksi lause siitä miksi tämä työ näyttää tältä. Ei kiinteitä nimiä — jos preset kelpaisi mihin
tahansa työhön, sitä ei ole tehty tästä. Useampi klippi saa viitata samaan.

**Ruutukaappaus on ajettava toimeksianto**, ei kuvaus: `nauhoitus`-olio jossa on osoite, ikkunan
koko, alkutila, aikaan sidotut askeleet ja muoto. Lukija on agentti jolla on selain ja nauhoitin,
joten se saa kaiken mitä ei voi päätellä. Osoite on **olemassa oleva** — jos aineisto ei nimeä
sellaista, käytän tuotteen omaa osoitetta enkä keksi, koska keksitty osoite pysäyttää ajajan.
Askelten viimeinen loppuaika on sama kuin klipin kesto, ja pystyvideoon nauhoitetaan pystyssä.

Nauhoitettavalla ei ole promptia, presettiä, `imagine`-asetuksia eikä `grok_kesto_s`-kenttää: Grok
ei generoi sitä, joten sen kesto on `kesto_s` eikä mikään muu.

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
            "groups the shots into clips, designs this job's presets, writes FOUR prompt versions "
            "for every generated clip plus the Imagine settings, and stores them under this run's "
            "own key. It takes minutes, because the longest version alone is ~3000 characters per "
            "clip. You do NOT write prompts yourself and you do NOT regroup.\n"
            "2. Return its report verbatim — how many clips and prompts landed, the version lengths "
            "and the preset names, or the FAILED line and its reason."
        ),
        agent=writer,
        expected_output="The tee_grok report: clips written + the memory key, or the FAILED reason.",
    )
    return ([writer], [task])


def run() -> None:
    run_crew(CrewSpec(agent_name=AGENT_NAME, build_domain=build_domain, readme_md=README, temperature=0.6))


if __name__ == "__main__":
    run()

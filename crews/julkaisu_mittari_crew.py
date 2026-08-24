"""julkaisu-mittari: what each published piece actually did. This is where the loop closes.

Runs on its OWN daily schedule, after publishing — not inside the `julkaisupoyta` workflow, because
what it measures happens hours after the workflow has finished.

For every run the human gate approved more than 24 h ago and that has not been measured yet, it
reads `GET /v1/connections/attempts` and `/attempts/:id/metrics`, writes `julkaisu.{ref}.mittaus`,
and folds the numbers plus ONE learned sentence into `julkaisu.kerrottu` — the same record the
editor reads at step 2 when it picks the next story.

Without this agent the desk is a text generator. With it, the third run is better than the first and
the reason is readable.

It never invents a number. An attempt record it cannot place on a run, or a metrics route that does
not answer, is reported as unmeasured — never recorded as a zero.

Register + approve, then restart the fleet:
  npx aimeat@latest connect --url https://aimeat.io --owner <your-aimeat-account> --agent julkaisu-mittari
Run standalone: uv run python crews/julkaisu_mittari_crew.py
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.julkaisu_desk import make_mittari_tools

AGENT_NAME = "julkaisu-mittari"

# ── This agent's own declaration ─────────────────────────────────────────────────────────────
# No `{ref}` in these signals, and that is deliberate: this agent is NOT a workflow step. It sweeps
# every published run on its own schedule, so its deliverable is the ledger, not one run's key.
LLM_PROFILE = "news"  # one Finnish sentence per measured run — the rest is arithmetic
SCHEDULE = {
    "cron": "0 7 * * *",
    "timezone": "Europe/Helsinki",
    "purpose": "measures yesterday's published pieces and folds the numbers into julkaisu.kerrottu",
}
TAGS = ["julkaisupoyta", "mittaus", "palauteluuppi", "role.task-runner"]
CAPABILITIES = {
    "technical": [
        {"name": "julkaisu-mittari", "type": "skill"},
        {"name": "connections-metrics", "type": "tool"},
    ],
    "domain": ["publishing measurement", "consumes:julkaisu-portti@1", "produces:julkaisu-kerrottu@1"],
    "languages": ["fi", "en"],
}
OFFERS = [
    {
        "id": "mittaa-julkaisut",
        "title": "Mittaa julkaistut ja kerro mitä opittiin",
        "ask": "Katson mitä yli vuorokausi sitten julkaistut tekstit oikeasti tekivät, ja kirjaan luvut "
        "julkaisupöydän muistiin yhden opitun lauseen kanssa — sen saman muistin, jota toimittaja "
        "lukee kun se valitsee seuraavan aiheen. En julkaise mitään enkä keksi lukua: ajon jota "
        "en pysty mittaamaan jätän mittaamatta ja kerron sen.",
        "example": "Mittaa eilen julkaistut",
        "cost": "cheap",
        "latency": "minutes",
        "tags": ["julkaisupoyta"],
        "repeatability": "accumulative",
        "verification": "deterministic",
        "dataHandling": "llm-provider",
        "json": True,
        "scheduleBorn": "daily 07:00 Europe/Helsinki — runs automatically",
        "consequences": [],
        "required_to_function": {"kind": "deterministic", "op": "exists", "key_glob": "julkaisu.*.portti"},
        "success_signal": {"kind": "deterministic", "op": "nonempty", "key": "julkaisu.kerrottu"},
        "deliverable_location": {"key": "julkaisu.kerrottu"},
        "sample": {
            "ref": "p1a2b3c",
            "aihe": "Decide what your AI may do the moment you connect it",
            "julkaistu": ["linkedin", "x"],
            "mittaus": {
                "linkedin": {"nayttokerrat": 1840, "klikit": 37},
                "x": {"nayttokerrat": 612, "klikit": 9},
                "haettu": "2026-08-26T07:00:00Z",
            },
            "opittu": "Ennen-tilalla avattu englanninkielinen ketju keräsi vähemmän näyttöjä mutta "
            "suhteessa enemmän klikkejä kuin suomenkielinen postaus — yhdestä ajosta ei vielä "
            "voi päätellä kumpi avaus toimii.",
        },
    }
]

README = """[[FIGLET:slant]["Mittari"]]

Kerron mitä julkaistut tekstit oikeasti tekivät — ja teen siitä muistin, jota toimittaja lukee.

Herään joka aamu klo 7 (Europe/Helsinki), poimin jokaisen ajon jonka ihminen hyväksyi portilla yli
vuorokausi sitten ja jota ei ole vielä mitattu, luen sen luvut solmulta ja kirjoitan
`julkaisu.<ref>.mittaus`. Samat luvut ja yksi opittu lause menevät `julkaisu.kerrottu`-listaan.

**Tämä on se kohta jossa luuppi sulkeutuu.** Ilman tätä pöytä on tekstigeneraattori; tämän kanssa
kolmas ajo on parempi kuin ensimmäinen ja syyn voi lukea.

**En keksi lukua.** Jos yritysmerkintää ei voi yhdistää ajoon tai lukuja ei saa, jätän ajon
mittaamatta ja kerron sen — nolla olisi väärä vastaus, ei tyhjä.
"""


def build_domain(ctx: BuildContext):
    measurer = Agent(
        role="Julkaisupöydän mittari",
        goal="Trigger the deterministic measurement sweep and report exactly what was and was not measured.",
        backstory="You do not estimate reach and you do not fill gaps. One tool call reads the gate "
        "decisions, the node's attempt records and their metrics, writes what it found and folds "
        "it into the desk's memory. You call it ONCE and report the result as it is — including "
        "the runs it could not measure, which matter more than the ones it could.",
        llm=ctx.llm,
        tools=[*make_mittari_tools(AGENT_NAME, task=ctx.task)],
    )
    task = Task(
        description=(
            f"Today is {ctx.today}. Request: '{ctx.prompt}'\n\n"
            "1. Call mittaa_julkaisut() EXACTLY ONCE. It takes no arguments: it finds every published run "
            "older than 24 h that has not been measured, reads its numbers, and records them. You do NOT "
            "estimate any figure yourself.\n"
            "2. Return its report verbatim — how many runs were measured, and which ones were left "
            "unmeasured and why. A run with nothing to measure is a normal result, not a failure."
        ),
        agent=measurer,
        expected_output="The mittaa_julkaisut report: runs measured, runs left unmeasured and why.",
    )
    return ([measurer], [task])


def run() -> None:
    run_crew(CrewSpec(agent_name=AGENT_NAME, build_domain=build_domain, readme_md=README, temperature=0.3))


if __name__ == "__main__":
    run()

"""julkaisu-x: one English X thread from the editor's angle. A step of the `julkaisupoyta` workflow.

Reads `julkaisu.{ref}.aineisto` and writes `julkaisu.{ref}.x` as `{text, notes}` — the posts
separated by a blank line.

**This one leads with the before-state.** The Finnish LinkedIn post opens on the fix; this thread
opens on what people were stuck with, and lets the fix arrive as the turn. If the two read as
translations of each other the run failed, even though both keys are non-empty. That divergence is
built into the prompt structurally — the two writers are handed the same facts through a different
door (`story_block(lead=...)`) — because "reads like a translation" is a judgement no check can make
across two languages. See the note in `crewaimeat.julkaisu_pipeline`.

It posts nothing anywhere and contacts nobody: the workflow's human-input gate is where a person
picks approve / rewrite / discard.

Register + approve, then restart the fleet:
  npx aimeat@latest connect --url https://aimeat.io --owner <your-aimeat-account> --agent julkaisu-x
Run standalone: uv run python crews/julkaisu_x_crew.py
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.julkaisu_pipeline import make_julkaisu_tools

AGENT_NAME = "julkaisu-x"
CHANNEL = "x"

# ── This agent's own declaration ─────────────────────────────────────────────────────────────
# `{ref}` is a workflow VARIABLE and stays literal here; the engine substitutes it per run.
LLM_PROFILE = "content"  # English prose — grok's one strength, and this thread is English only
TAGS = ["julkaisupoyta", "x-thread", "somekirjoitus", "role.task-runner"]
CAPABILITIES = {
    "technical": [{"name": "julkaisu-x", "type": "skill"}],
    "domain": ["X threads", "release storytelling", "consumes:julkaisu-aineisto@1"],
    "languages": ["en"],
}
OFFERS = [
    {
        "id": "kirjoita-x",
        "title": "Kirjoita X-ketju englanniksi",
        "ask": "Kirjoitan toimittajan aineistosta yhden X-ketjun englanniksi (3–6 postausta, kukin alle "
        "280 merkkiä). Avaan siitä mikä oli ennen rikki, en korjauksesta — se on suomenkielisen "
        "postauksen kulma. En ilmoittele ketjua, en pyydä seuraamaan, enkä kirjoita asioista "
        "jotka aineisto rajasi ulos. En julkaise mitään mihinkään.",
        "example": "Kirjoita tämän ajon X-ketju",
        "cost": "cheap",
        "latency": "minutes",
        "tags": ["julkaisupoyta"],
        "repeatability": "idempotent",
        "verification": "deterministic",
        "dataHandling": "llm-provider",
        "json": True,
        "consequences": [],
        "required_to_function": {"kind": "deterministic", "op": "nonempty", "key": "julkaisu.{ref}.aineisto"},
        "success_signal": {"kind": "deterministic", "op": "nonempty", "key": "julkaisu.{ref}.x"},
        "deliverable_location": {"key": "julkaisu.{ref}.x"},
        "sample": {
            "text": "Changing what your AI agent was allowed to do meant tearing the whole connection "
            "down and building it again.\n\nNot the permissions. The connection.\n\nSo people left it "
            "on whatever it happened to have, or gave up halfway through setup.\n\nThe approval window "
            "now asks before the connection completes: keep what it has, read-only, standard, full, or "
            "tick them yourself.\n\nOne choice, once, before anything is live. Next time you connect an "
            "AI service, read that window instead of clicking past it.",
            "notes": "Left out the agent-identity line and the account-scope line — the material ruled "
            "both out of this story. Opened on the rebuild, not the fix, so this is not the "
            "Finnish post in English.",
        },
    }
]

README = """[[FIGLET:slant]["Julkaisu X"]]

Kirjoitan toimittajan aineistosta yhden englanninkielisen X-ketjun: 3–6 postausta, kukin alle 280
merkkiä, tyhjä rivi väliin.

**Kulmani on ENNEN-tila.** Aloitan siitä mikä oli rikki, ja korjaus tulee käänteenä. Suomenkielinen
LinkedIn-postaus samasta aiheesta aloittaa korjauksesta — jos nämä kaksi lukevat kuin sama teksti
kahdella kielellä, ajo epäonnistui vaikka molemmat tiedostot ovat olemassa.

**Mistä luen:** `julkaisu.<ref>.aineisto`. **Mihin kirjoitan:** `julkaisu.<ref>.x` (`text` =
postaukset tyhjällä rivillä erotettuina, + `notes`).

Ensimmäinen postaus seisoo yksin väitteenä. Ei "🧵"-ilmoitusta, ei emoji-luetteloita, ei
seuraamispyyntöä lopussa — viimeinen postaus kertoo mitä lukija voi tehdä seuraavaksi.
**En julkaise mitään mihinkään.**
"""


def build_domain(ctx: BuildContext):
    writer = Agent(
        role="X Thread Write Runner",
        goal="Trigger the deterministic X-thread write for this run and report what it wrote.",
        backstory="You do not write the thread by hand and you do not choose where it goes. The run's key is "
        "already resolved in code; you call write_julkaisu ONCE and report its result. If it "
        "reports FAILED, you report that failure as it is — you never write posts yourself to "
        "cover for it, and you never claim something was written when it was not.",
        llm=ctx.llm,
        tools=[*make_julkaisu_tools(AGENT_NAME, CHANNEL, task=ctx.task, prompt=ctx.prompt)],
    )
    task = Task(
        description=(
            f"Today is {ctx.today}. Request: '{ctx.prompt}'\n\n"
            "1. Call write_julkaisu() EXACTLY ONCE. It takes no arguments: it reads the editor's material "
            "for this run, writes the English thread against the house rules — opening on the before-state, "
            "not the fix — and stores it under this run's own key. You do NOT write the posts yourself.\n"
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

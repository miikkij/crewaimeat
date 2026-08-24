"""julkaisu-x: one English X thread from one brief. A step of the `julkaisupoyta` workflow.

Reads the brief at `julkaisu.<ref>.brief` and writes `julkaisu.<ref>.x` — an object with `text` (the
posts, separated by a blank line) and `notes` (what it left out and why). It posts nothing anywhere
and contacts nobody: the workflow's human-input gate is where a person picks approve / rewrite /
discard.

The crew is a thin wrapper: the run's `ref` is resolved IN CODE from the dispatched task and bound
into the tool, the brief is read and required, and the house rules (3–6 posts, each under 280
characters, no thread announcement, no emoji bullets, no follow-bait) are checked deterministically
before anything is written. See `crewaimeat.julkaisu_pipeline`.

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
# The single source for what this agent is: its model routing, how it is discovered, and what it
# promises — including the workflow signals, so a `julkaisupoyta` step can name this offer. The
# `{ref}` in the keys is a workflow VARIABLE and stays literal here; the engine substitutes it per
# run. Hardcoding a value there would write every run into the same key.
LLM_PROFILE = "content"  # English prose — grok's one strength, and this thread is English only
TAGS = ["julkaisupoyta", "x-thread", "somekirjoitus", "role.task-runner"]
CAPABILITIES = {
    "technical": [{"name": "julkaisu-x", "type": "skill"}],
    "domain": ["X threads", "product launch copy", "consumes:julkaisu-brief@1"],
    "languages": ["en"],
}
OFFERS = [
    {
        "id": "kirjoita-x",
        "title": "Kirjoita X-ketju englanniksi",
        "ask": "Anna minulle avain julkaisu.{ref}.brief, niin kirjoitan siitä yhden X-ketjun englanniksi "
        "(3–6 postausta). En julkaise sitä mihinkään enkä ota yhteyttä kehenkään — teksti jää "
        "muistiin, ja ihminen päättää mitä sille tehdään.",
        "example": "Kirjoita X-ketju tämän viikon julkaisusta",
        "cost": "cheap",
        "latency": "minutes",
        "tags": ["julkaisupoyta"],
        "repeatability": "idempotent",
        "verification": "deterministic",
        "dataHandling": "llm-provider",
        "json": True,  # the deliverable is an object ({text, notes}); format follows when the node enum has "json"
        "consequences": [],
        "required_to_function": {"kind": "deterministic", "op": "nonempty", "key": "julkaisu.{ref}.brief"},
        "success_signal": {"kind": "deterministic", "op": "nonempty", "key": "julkaisu.{ref}.x"},
        "deliverable_location": {"key": "julkaisu.{ref}.x"},
        "sample": {
            "text": "Your AI connection now finishes only after you have decided what the agent is "
            "allowed to do.\n\nThe approval window asks up front: keep what it has, read-only, "
            "standard, full — or tick the permissions yourself.\n\nBefore, the connection was made "
            "with whatever permissions the agent happened to hold. Changing them meant finding "
            "Profile > Agents and rebuilding the whole MCP connection.\n\nThat rebuild was where new "
            "users stopped. Now the choice happens once, before the connection is done.",
            "notes": "Left out the changelog date and the list of supported clients — four posts hold "
            "one change, and the brief carries the source.",
        },
    }
]

README = """[[FIGLET:slant]["Julkaisu X"]]

Kirjoittaa yhdestä julkaisubriiffistä yhden englanninkielisen X-ketjun: 3–6 postausta, jokainen alle
280 merkkiä, ensimmäinen seisoo yksin väitteenä. Ei "🧵"-ilmoitusta, ei emoji-luetteloita, ei
seuraamispyyntöä lopussa.

**Mistä luen ja mihin kirjoitan:** briiffi `julkaisu.<ref>.brief` → ketju `julkaisu.<ref>.x`
(`text` = postaukset tyhjällä rivillä erotettuina, + `notes`). **En julkaise mitään mihinkään** —
ihminen hyväksyy, korjauttaa tai hylkää.

**Miten annat työn:** julkaisupöytä-työnkulku antaa sen itse. Käsin: kerro ajossa mikä `ref` on
(esim. "kirjoita X-ketju avaimesta julkaisu.demo1.brief").
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
            "1. Call write_julkaisu() EXACTLY ONCE. It takes no arguments: it reads this run's brief, "
            "writes the English thread against the house rules, and stores it under this run's own "
            "key. You do NOT write the posts yourself.\n"
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

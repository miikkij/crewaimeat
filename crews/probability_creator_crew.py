"""probability-creator — turns one request into a spectrum of answers.

Given any idea, question, or thing to open up, this crew returns SEVERAL distinct
answers to that same request, each tagged with a probability level that says how
likely that answer is to be the real / expected / correct one:

    0%   -> a genuine longshot: contrarian, unexpected, improbable (but real)
    ...
    100% -> the obvious, safe, near-certain answer most would land on

Default = 5 levels (0 / 25 / 50 / 75 / 100%). If the request asks for a count
N between 5 and 10, the crew uses N levels spread evenly from 0% to 100%.

Built on the crewaimeat AIMEAT scaffold: this file defines ONLY the domain agents
and their tasks (build_domain). Everything AIMEAT-related — onboarding, the task
daemon, liaison publish/complete, live progress, date injection — is provided by
crewaimeat.aimeat_crew.run_crew (background: SCAFFOLD_CANON.md).

Register first, then approve in the dashboard (Profile -> Agents):
  npx aimeat@latest connect add --agent probability-creator --mode task-runner --url https://aimeat.io --owner <your-aimeat-account>

Run:
  uv run python crews/probability_creator_crew.py
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.crew import _web_tools  # Tavily web search if TAVILY_API_KEY is set, else []

# === CUSTOMIZE 1: your AIMEAT agent identity =============================== #
AGENT_NAME = "probability-creator"  # must match `aimeat connect add --agent ...`

README = '''[[FIGLET:slant]["PROBABILITY"]]

# probability-creator — one question, a spectrum of answers

Give me a question or idea and I return several distinct answers across a probability spectrum,
from a 0% longshot (contrarian, unexpected) to the 100% obvious pick. Default is 5 levels
(0 / 25 / 50 / 75 / 100%); ask for a count N between 5 and 10 to get more.

## How to task me
Queue any question; optionally ask for a count:
- `What killed the dinosaurs? give me 7 answers`
- `Where should we hold the team offsite?`
- `Why is our build flaky?`
'''


# === CUSTOMIZE 2: your domain agents + tasks ============================== #
def build_domain(ctx: BuildContext) -> tuple[list[Agent], list[Task]]:
    """Return (agents, tasks). Tasks run in order; the LAST task's output is
    what gets published to AIMEAT memory + used as the completion summary.

    Sequential pipeline:
      Ideator -> drafts one answer per probability level (0% longshot .. 100% obvious)
      Editor  -> orders them, keeps each distinct, writes the final deliverable
    """
    llm, today, prompt = ctx.llm, ctx.today, ctx.prompt

    ideator = Agent(
        role="Probability Ideator",
        goal=(
            "For the user's request, generate several genuinely distinct answers — one per "
            "probability level — spanning from the most improbable longshot (0%) to the most "
            "obvious, near-certain answer (100%)."
        ),
        backstory=(
            "You are a lateral thinker who can deliberately answer across the whole plausibility "
            "spectrum. You can produce a wild, contrarian longshot and a safe, obvious pick for the "
            "same question, and grade everything in between by how probable each answer really is."
        ),
        tools=_web_tools(),  # available; reach for it only when a request needs a current fact
        llm=llm,
        verbose=True,
    )
    editor = Agent(
        role="Editor",
        goal=(
            "Turn the draft into a clean final deliverable: one distinct answer per probability "
            "level, ordered from 0% to 100%, each clearly labeled."
        ),
        backstory=(
            "You are a sharp editor who guarantees every probability tier is genuinely different, "
            "that plausibility rises with the percentage, and that the result is tidy and easy to read."
        ),
        llm=llm,
        verbose=True,
    )

    ideate = Task(
        description=(
            f"{today}\n\n"
            "You are given a request below — an idea to open up, a question, or anything the user "
            "wants answers to. Produce SEVERAL DISTINCT ANSWERS to that same request, each tied to a "
            "PROBABILITY LEVEL that states how likely that answer is to be the real / expected / "
            "correct one:\n"
            "  - 0%   = a genuine longshot: an answer that is almost certainly NOT it — contrarian, "
            "unexpected, improbable, yet still a real answer to the request.\n"
            "  - 100% = the obvious, safe, near-certain answer most people would land on.\n"
            "  - The levels between climb steadily in plausibility as the percentage rises.\n\n"
            "How many levels to produce:\n"
            "  - If the request asks for a specific number N of answers (anywhere from 5 to 10), use N "
            "levels spread EVENLY from 0% to 100% inclusive (e.g. N=5 -> 0/25/50/75/100; "
            "N=7 -> about 0/17/33/50/67/83/100, rounded to whole percents).\n"
            "  - Otherwise use the default 5 levels: 0%, 25%, 50%, 75%, 100%.\n\n"
            "Make each level a genuinely different answer, and let the spectrum climb from improbable "
            "to obvious as the percentage grows. Answer in the SAME LANGUAGE as the request. Reach for "
            "web search only when the request needs a current fact you would otherwise lack.\n\n"
            f"Request:\n{prompt}"
        ),
        expected_output=(
            "A draft set of answers, one per probability level (from 0% up to 100%), each labeled with "
            "its percentage and clearly distinct from the others."
        ),
        agent=ideator,
    )
    compose = Task(
        description=(
            "Take the Ideator's draft and produce the FINAL deliverable.\n"
            "  - Keep exactly the probability levels the Ideator used (default 0/25/50/75/100, or the "
            "evenly-spread N levels if the request asked for a count).\n"
            "  - Present them in ASCENDING order: 0% first, 100% last.\n"
            "  - Give ONE clear answer per level. Keep each level distinct and let plausibility rise "
            "with the percentage — 0% reads as a clear longshot, 100% as the obvious / near-certain "
            "answer. If two levels feel close, push the lower one further toward a longshot.\n"
            "  - Format each level as a labeled section, e.g. a heading '**0% — <short label>**' "
            "followed by the answer.\n"
            "  - Match the language of the original request, and keep the output to the answers "
            "themselves plus a one-line lead-in naming what was asked.\n\n"
            f"Original request (for language + intent):\n{prompt}"
        ),
        expected_output=(
            "The final set of answers, one per probability level in ascending order (0% -> 100%), each "
            "labeled with its percentage and clearly distinct."
        ),
        agent=editor,
    )

    return [ideator, editor], [ideate, compose]


def run() -> None:
    run_crew(
        CrewSpec(
            agent_name=AGENT_NAME,
            build_domain=build_domain,
            readme_md=README,
            temperature=0.7,  # creative service — enforce a warm temperature (no per-task classification)
        )
    )


if __name__ == "__main__":
    run()

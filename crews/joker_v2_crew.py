"""joker-v2: an EVOLVED variant of `joker` — four comedians each draft many jokes and keep their
best, then a ruthless editor cuts the weak ones and presents only what actually lands.

This is a deliberate A/B challenger to the original `joker` (which stays live, with its history): same
creative domain, evolved DESIGN (draft-six -> select-best -> editor craft-gate + warm temperature).
Register it as its OWN agent so its reputation accumulates separately and a selector can compare the two:
  npx aimeat@latest connect add --agent joker-v2 --mode task-runner --url https://aimeat.io --owner <your-aimeat-account>

Run: uv run python crews/joker_v2_crew.py
"""

from __future__ import annotations

import re

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew

AGENT_NAME = "joker-v2"

# --- deterministic meta-leak strip -------------------------------------------------------------
# The editor is told to keep its KEPT/CUT reasoning private; owl-alpha obeys ~9/10 but occasionally
# leaks it into the deliverable ("Let me work through these… Final lineup:"). The prompt can't fully
# guarantee it, so we ENFORCE it in code (deterministic, model-whim-proof) — same idea as the
# coordinator's _strip_leaked_directives. Passed to CrewSpec.clean_deliverable; runs just before publish.
_LINEUP_RE = re.compile(r"(?im)^\s*\**\s*final\s+lineup\b.*$")          # "**Final lineup: 2 jokes.**"
# Decision labels use a DASH ("**Punslinger — CUT.**" / "Observational — KEPT & SHARPENED"); joke labels
# use a COLON ("**Roast:**"), so keying on the dash + CUT/KEPT never eats a real joke (e.g. a "director's cut").
_DECISION_RE = re.compile(
    r"(?im)^\s*\**\s*(?:punslinger|observational|roast|storyteller)\b\s*\**\s*[—–-]\s*\**\s*(?:cut|kept)\b.*$"
)
_META_HEADER_RE = re.compile(r"(?im)^\s*\**\s*(?:what i cut and why|cut note|let me work through)\b.*$")


def _strip_editor_meta(text: str) -> str:
    """Remove the editor's leaked editing scaffolding, leaving only the intro + jokes.

    1. If the editor announced a "Final lineup", keep only what follows the last such marker
       (drops the whole analysis preamble before it). 2. Drop stray decision/meta lines.
    3. Collapse blank runs. Falls back to the original if stripping would empty it."""
    if not text:
        return text
    original = text
    markers = list(_LINEUP_RE.finditer(text))
    if markers:
        m = markers[-1]
        after, before = text[m.end():].strip(), text[:m.start()].strip()
        # "Final lineup:" can be a HEADER (jokes follow — keep `after`) or a TRAILING summary (jokes are
        # above — keep `before`). Substantial content after the marker means it's a header.
        text = after if len(after) > 80 else (before or after)
    kept = [ln for ln in text.splitlines() if not _DECISION_RE.match(ln) and not _META_HEADER_RE.match(ln)]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    return text or original

README = '''[[FIGLET:slant]["JOKER v2"]]

# joker-v2 — four comedians and a ruthless editor (evolved variant)

Send me a topic and four comics work it from four angles — a pun, an observational bit, a playful
roast, and a short anecdote. Each one writes a batch and keeps only their funniest, then an editor
cuts anything that doesn't land and presents the lineup. I match the language of your topic.

## How to task me
Queue a task with whatever you want jokes about:
- `dad jokes about Mondays`
- `roast my over-engineered side project`
- `a short funny story about a cat who refuses to use the litter box`
'''

# The actual craft. Generic one-shot jokes are the groaners; these are the levers that make jokes land.
CRAFT = (
    "What makes a joke land:\n"
    "- SPECIFIC beats generic — name the real detail, not the category.\n"
    "- The punchline must SURPRISE: misdirection, an unexpected angle, or a sharp turn. If the listener "
    "sees it coming, it's dead.\n"
    "- Tight: every word earns its place; end on the funniest, hardest-hitting word.\n"
    "- Rule of three, escalation, and callbacks are your tools.\n"
    "AVOID: clichés ('why did the X cross the road', 'I'm not saying… but'), explaining the joke, "
    "limp puns that only rhyme, and anything you've heard before. A groan is a fail, not a win."
)


def build_domain(ctx: BuildContext) -> tuple[list[Agent], list[Task]]:
    llm, topic = ctx.llm, ctx.prompt

    punslinger = Agent(
        role="Punslinger",
        goal="Land one genuinely clever pun or wordplay joke — the kind that earns a laugh, not a groan",
        backstory=(
            "You live for wordplay, but you have taste: you find the word that bends two ways and the "
            "twist nobody expects, and you throw away the lazy rhymes everyone else would settle for."
        ),
        llm=llm,
        verbose=True,
    )
    observer = Agent(
        role="Observational Comic",
        goal="Make one sharp observational joke about the small absurd truth in the topic",
        backstory=(
            "Seinfeld school. You notice the specific small absurd thing everyone lives with but nobody "
            "says out loud — and you name the exact detail that makes it click."
        ),
        llm=llm,
        verbose=True,
    )
    roaster = Agent(
        role="Roast Comic",
        goal="Deliver one sharp, playful roast of the topic that actually stings-then-laughs",
        backstory="You roast with a wink: pointed, specific, clever — never cruel, never generic. The target laughs hardest.",
        llm=llm,
        verbose=True,
    )
    storyteller = Agent(
        role="Storyteller Comedian",
        goal="Tell one short comic anecdote with a real setup and a punchline that turns",
        backstory="You build a tiny vivid scene, draw the listener in, and land an ending they didn't see coming.",
        llm=llm,
        verbose=True,
    )
    editor = Agent(
        role="Comedy Editor",
        goal="Keep only the jokes that genuinely land, sharpen them, and cut the rest without mercy",
        backstory=(
            "You are a hard-nosed comedy editor and a tough audience. You have killed more jokes than you "
            "have kept. A joke that only half-works gets cut, not padded. You would rather present two "
            "jokes that genuinely land than four that limp."
        ),
        llm=llm,
        verbose=True,
    )

    def joke_task(agent: Agent, style: str) -> Task:
        return Task(
            description=(
                f"Topic:\n{topic}\n\n"
                f"Write SIX different {style} jokes about this topic — fast, varied, push past the "
                f"obvious first ideas (the first thing that comes to mind is usually the groaner everyone "
                f"else writes). Then judge your own six honestly and output ONLY your single best one, "
                f"polished and tight.\n\n{CRAFT}\n\n"
                f"Match the language of the topic."
            ),
            expected_output="Your single funniest joke, in your style, in the language of the topic (the six drafts are scratch work — do not include them).",
            agent=agent,
        )

    t_pun = joke_task(punslinger, "pun / wordplay")
    t_obs = joke_task(observer, "observational")
    t_roast = joke_task(roaster, "playful roast")
    t_story = joke_task(storyteller, "short comic anecdote")

    t_edit = Task(
        description=(
            "Four comics have each handed you their best joke about the topic (Punslinger, Observational, "
            "Roast, Storyteller). You are the quality gate.\n\n"
            "1. Judge each joke HONESTLY against the craft below. Does it actually make you laugh, or just "
            "nod? Is the punchline a surprise, or did you see it coming? Is it specific, or generic?\n"
            "2. CUT any joke that doesn't land — a weak joke makes the whole set worse. It is fine to keep "
            "only two or three if the others limp. Never pad the set with a joke you don't believe in.\n"
            "3. SHARPEN the ones you keep: tighten the wording, fix the rhythm, make the punch-word land last.\n"
            "4. Steps 1-3 are your PRIVATE editing process — do them in your head. Your final answer is the "
            "stage-ready lineup ONLY: a one-line intro naming the topic, then each surviving joke under its "
            "label. Do NOT include your cut decisions, 'KEPT/CUT' notes, scores, or any commentary about "
            "the editing — the audience sees jokes, not the editing room. Keep the language of the topic.\n\n"
            f"{CRAFT}"
        ),
        expected_output=(
            "A one-line intro, then only the jokes that genuinely land (2-4), each sharpened and labeled. "
            "No editorial notes, no cut/keep commentary — just the intro and the jokes."
        ),
        agent=editor,
        context=[t_pun, t_obs, t_roast, t_story],
    )

    return (
        [punslinger, observer, roaster, storyteller, editor],
        [t_pun, t_obs, t_roast, t_story, t_edit],
    )


def run() -> None:
    # Comedy is a creative service — enforce a warm temperature directly (same 0.7 as v1 now, so the two
    # jokers differ in DESIGN only, not temperature). No per-task classification needed for a single-purpose
    # creative crew.
    run_crew(CrewSpec(
        agent_name=AGENT_NAME, build_domain=build_domain, readme_md=README,
        temperature=0.7,
        clean_deliverable=_strip_editor_meta,  # enforce: no leaked KEPT/CUT scaffolding in the deliverable
        self_monitor=True,  # propose an evolution if own reputation shows a WEAK/SPLIT signal (doc 20)
    ))


if __name__ == "__main__":
    run()

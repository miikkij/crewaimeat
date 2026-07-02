"""joker: four comedians each riff on the incoming task, then a host presents all four.

Generated on the AIMEAT scaffold (crewaimeat). Edit build_domain to taste; the scaffold
provides the AIMEAT wiring (see SCAFFOLD_CANON.md). Register first:
  npx aimeat@latest connect add --agent joker --mode task-runner --url https://aimeat.io --owner <your-aimeat-account>

Run: uv run python crews/joker_crew.py
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew

AGENT_NAME = "joker"

README = """[[FIGLET:slant]["JOKER"]]

# joker — four comedians and a host

Send me a topic and I riff on it from four angles — a pun, an observational bit, a playful roast,
and a short anecdote — then present them as one clean lineup. I match the language of your topic.

## How to task me
Queue a task with whatever you want jokes about:
- `dad jokes about Mondays`
- `roast my over-engineered side project`
- `a short funny story about a cat who refuses to use the litter box`
"""


def _remember_lineup(text: str) -> str:
    """clean_deliverable seam doubling as the publish hook: remember the published lineup so future
    sets can avoid retelling it, then return the text unchanged. Memory is an optional enhancement —
    open_store degrades LOUD to None and the jokes still ship."""
    from crewaimeat.pipeline_memory import open_store

    store = open_store(AGENT_NAME)
    if store and isinstance(text, str):
        store.remember(text, source="joke-lineup", metadata={"category": "jokes"})
    return text


def build_domain(ctx: BuildContext) -> tuple[list[Agent], list[Task]]:
    llm, topic = ctx.llm, ctx.prompt
    # ANTI-RERUN MEMORY: show every comedian the most similar jokes already told about this kind of
    # topic — fresh material instead of the same pun with new words. "" when memory is unavailable.
    from crewaimeat.pipeline_memory import open_store

    store = open_store(AGENT_NAME)
    told = (
        store.prior_art_block(
            topic or "jokes",
            k=4,
            label="ALREADY TOLD (past sets)",
            category="jokes",
            instruction="you have already performed these — do NOT retell or lightly rephrase any of them:",
        )
        if store
        else ""
    )

    punslinger = Agent(
        role="Punslinger",
        goal="Land one quick pun or wordplay joke about the topic",
        backstory=(
            "You live for puns and dad-joke energy. You find the word that bends two ways and snap it shut with a grin."
        ),
        llm=llm,
        verbose=True,
    )
    observer = Agent(
        role="Observational Comic",
        goal="Find the wry everyday-life angle and make one observational joke",
        backstory=(
            "Seinfeld school. You notice the small absurd thing everyone lives with but "
            "nobody says out loud, and you say it."
        ),
        llm=llm,
        verbose=True,
    )
    roaster = Agent(
        role="Roast Comic",
        goal="Deliver one sharp, playful roast of the topic",
        backstory="You roast with a wink: pointed and clever, never cruel. The target laughs hardest.",
        llm=llm,
        verbose=True,
    )
    storyteller = Agent(
        role="Storyteller Comedian",
        goal="Tell one short setup-and-punchline comic anecdote about the topic",
        backstory="You build a tiny scene, draw the listener in, and land the punchline at the end.",
        llm=llm,
        verbose=True,
    )
    host = Agent(
        role="Host",
        goal="Present all four comedians' jokes as one clean lineup",
        backstory="You are the compere who introduces the bit and hands the mic to each comic.",
        llm=llm,
        verbose=True,
    )

    def joke_task(agent: Agent, style: str) -> Task:
        return Task(
            description=(
                f"Tell exactly ONE joke about the following, in your {style} style. "
                f"Keep it tight, and match the language of the topic. Topic:\n{topic}" + (f"\n\n{told}" if told else "")
            ),
            expected_output="One joke, in your style, in the language of the topic.",
            agent=agent,
        )

    t_pun = joke_task(punslinger, "pun / wordplay")
    t_obs = joke_task(observer, "observational")
    t_roast = joke_task(roaster, "playful roast")
    t_story = joke_task(storyteller, "short comic anecdote")
    t_host = Task(
        description=(
            "Four comedians above have each told one joke about the topic. Present them as the "
            "final answer: a one-line intro naming the topic, then each joke under a label "
            "(Punslinger, Observational, Roast, Storyteller). Keep every joke intact and in the "
            "language of the topic."
        ),
        expected_output="A short intro line, then the four labeled jokes.",
        agent=host,
        context=[t_pun, t_obs, t_roast, t_story],
    )

    return (
        [punslinger, observer, roaster, storyteller, host],
        [t_pun, t_obs, t_roast, t_story, t_host],
    )


def run() -> None:
    # Comedy is a creative service — enforce a warm temperature (no per-task classification needed).
    # self_monitor: after each task, check own reputation and propose an evolution if a signal fires (doc 20).
    run_crew(
        CrewSpec(
            agent_name=AGENT_NAME,
            build_domain=build_domain,
            readme_md=README,
            temperature=0.7,
            self_monitor=True,
            clean_deliverable=_remember_lineup,
        )
    )


if __name__ == "__main__":
    run()

"""spawn-demo: the smallest honest agent that exists to prove the SPAWNED run mode end to end.

It answers one short question and stops. That is the point: the interesting thing here is not what it
says, it is that nothing of it exists between tasks. No thread, no liaison, no crew objects, no memory
— just a row in the spawner's table and one parked HTTP request. The node pushes, a process appears,
it works, it exits, and the operating system takes every byte back.

Register once:
  npx aimeat@latest connect --url https://aimeat.io --owner <owner> --agent spawn-demo

Run it the spawned way (the spawner starts this for you when work arrives):
  uv run python -m crewaimeat.spawner --agents spawn-demo
Or run exactly one cycle by hand, which is what the spawner does:
  uv run python -m crewaimeat.run_once spawn-demo
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew

AGENT_NAME = "spawn-demo"

# ── This agent's own declaration ─────────────────────────────────────────────────────────────
LLM_PROFILE = "content-free"

# HOW this agent is run. "spawn" = it is data on the node while idle; a worker PROCESS is started per
# wake and torn down after. Omit this constant (as all 49 existing crews do) and the agent stays
# "continuous" — a thread in fleet_host — so adding the field changes nothing for anyone else.
RUN_MODE = "spawn"

# The SAME knob as CrewSpec.max_concurrent_tasks — there is no second concurrency concept. 1 means
# single-flight: one task at a time. It is the right default for anything whose side effects are not
# idempotent, because a hard-killed worker leaves its task active and the next run repeats it.
MAX_CONCURRENT = 1

TAGS = ["demo", "runtime.spawn", "role.task-runner"]
CAPABILITIES = {
    "technical": [{"name": "spawn-demo", "type": "skill"}],
    "domain": ["runtime demonstration"],
    "languages": ["fi", "en"],
}
OFFERS = [
    {
        "id": "spawn-proof",
        "title": "A one-sentence answer from an agent that did not exist a second ago",
        "ask": "Ask me anything short. The answer is not the point — the lifecycle is: I am data on the "
        "node until your request arrives, then a process starts, answers, and gives its memory back.",
        "example": "miksi prosessi joka herää vasta töistä on halvempi?",
        "cost": "cheap",
        "latency": "seconds",
        "repeatability": "idempotent",
        "verification": "ungated",
        "consequences": [],
        "sample": "Koska idle ei maksa mitään: prosessia ei ole olemassa ennen kuin sille on työtä.",
    }
]

README = """[[FIGLET:slant]["SPAWN"]]

# spawn-demo — an agent that does not exist until you need it

I am the proof of crewaimeat's spawned run mode. Between tasks I am not a process: I am a row in a
table and one parked connection. When you queue me a task the node pushes it down the tunnel, a
worker process starts, answers, and exits — and the memory goes straight back to the machine.

## How to task me
Queue anything short. The answer is not the point; the lifecycle is.
"""


def build_domain(ctx: BuildContext) -> tuple[list[Agent], list[Task]]:
    answerer = Agent(
        role="Answerer",
        goal="Answer the incoming request in one or two short sentences",
        backstory=(
            "You are deliberately small. You answer plainly and briefly, in the language of the "
            "question, and you never pad. Brevity is the whole job."
        ),
        llm=ctx.llm,
        verbose=True,
    )
    task = Task(
        description=(
            "Answer this in one or two short sentences, in the language it is written in. "
            f"Do not add preamble or a summary.\n\n{ctx.prompt}"
        ),
        expected_output="One or two short sentences, in the language of the request.",
        agent=answerer,
    )
    return [answerer], [task]


def run() -> None:
    run_crew(
        CrewSpec(
            agent_name=AGENT_NAME,
            build_domain=build_domain,
            readme_md=README,
            max_concurrent_tasks=MAX_CONCURRENT,
        )
    )


if __name__ == "__main__":
    run()

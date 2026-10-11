"""tv-opas-recorder: records a live TV stream for the TV-opas app and keeps the recordings.

The owner's ruling (2026-10-10): work a node extension cannot do goes to a crewaimeat agent, a
recording is ONE MP4 file, and the agent does all of the work. So the TV-opas app
(https://tv-opas.apps.aimeat.io/) creates a task with the stream and the times as structure, and this
agent waits for the start, records, compresses to the asked quality, uploads the one file to its own
storage on the node, and keeps the catalogue the app reads. The whole contract is brief
doc-mv2t4n84hsf3; the work is `crewaimeat.tv_recorder` over the runtime tool `crewaimeat.stream_record`.

NO MODEL IS ASKED ANYTHING. Every parameter arrives in the task's scope and every step is code, so the
task is handled by `on_task`. This is an agent and not a node tool for one reason: it runs long and
unattended (a film is two hours of wall clock, then minutes of every core), with ffmpeg and a disk.

It needs ffmpeg on the computer that runs it; without it every recording fails with NO_FFMPEG.

PARKED (the leading underscore) UNTIL ITS OWNER REGISTERS IT. A live crew file with no registered agent
is a doctor ERROR on the fleet host (`registry.serve.unregistered`), which stops every commit made there.
To bring it online, in this order:
  1. register + approve, asking for the scopes the work needs (proven sufficient on a local node):
       npx aimeat@latest connect --url https://aimeat.io --owner <you> --agent tv-opas-recorder \
         --mode task-runner --scopes storage:read,storage:write,memory:read,memory:write
  2. set the agent to run always on (resident) on its page;
  3. git mv crews/_tv_opas_recorder_crew.py crews/tv_opas_recorder_crew.py, commit, restart the fleet.
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.tv_recorder import handle_task

AGENT_NAME = "tv-opas-recorder"

# ── This agent's own declaration ─────────────────────────────────────────────────────────────
# Declared because the scaffold builds a domain for every task before it asks `on_task`; no call is
# ever made on it (see handle_task).
LLM_PROFILE = "content-free"

# NO RUN LIMIT. The spawner ends a per-wake worker after an hour by default, which is the guard against
# a model stuck in a loop. Nothing here is a model, and the length of a run is the length of the
# programme plus the time to compress it: an hour's limit cuts a film at minute 60. Every step is
# bounded by the task's own `stop` instead.
RUN_TIMEOUT_S = 0

# ALWAYS ON, and the reason is two programmes at once. A per-wake worker takes the tasks that are there
# when it starts and exits when they are done; a recording asked for while a film is being recorded
# would wait for the film to end, and miss its own start. A resident agent takes it at once, up to
# MAX_CONCURRENT. The node's run mode for the agent is the owner's setting; this is what the crew asks
# for. Run per wake anyway, it still records, one batch of tasks at a time.
RUN_MODE = "resident"

# How many recordings may be in hand at once: two programmes that overlap, one waiting for its start,
# and a delete that must not queue behind a film. A capture is a stream copy and costs no CPU; the
# compression after it takes every core for some minutes, at below-normal priority.
MAX_CONCURRENT = 4

# `role:tvopas-recorder` is the switch: the app lists its owner's agents, and shows its recording
# controls only when one carries this tag.
TAGS = ["role:tvopas-recorder", "tv-opas", "recording", "role.task-runner"]
CAPABILITIES = {
    "technical": [{"name": "stream_record", "type": "tool"}, {"name": "ffmpeg", "type": "tool"}],
    "domain": [
        "records a live HLS stream to one MP4 (H.264 + AAC) in its own node storage",
        "consumes:tvopas.record@1",
        "consumes:tvopas.delete@1",
    ],
    "languages": ["en", "fi"],
}

OFFERS = [
    {
        "id": "record-tv-programme",
        "title": "Record a TV programme from the TV-opas app and keep it to watch later",
        # The node takes an `ask` of at most 500 characters and refuses the whole offers document over one more.
        "ask": "Press Record on a programme in the TV-opas app. I wait for its start, record the live stream to "
        "its end, compress it into one MP4 at the quality you chose and keep it in my own storage, where "
        "only you can play it. I take the job as structure from the app; a request written as a sentence "
        "I refuse. I do NOT publish a recording or hand it to anyone, and I delete it when its keep time "
        "ends or when you ask.",
        "example": "TV-opas: Record on 'Petolliset', Nelonen, 18:00 to 19:35, quality low, keep 14 days",
        "cost": "free",
        "latency": "long-running",
        "repeatability": "idempotent",
        "verification": "deterministic",
        "consequences": [],
        "sample": "Petolliset, Nelonen, 95 min, 248 MB",
    }
]

README = """[[FIGLET:slant]["TV Recorder"]]

I record TV programmes for the **TV-opas** app. Press Record on a programme there and I wait for its
start, record the live stream to its end, compress it into **one MP4** and keep it in my own storage,
where only you can play it. Three qualities: **low** (360p, about 160 MB an hour, the default),
**medium** (480p, about 320 MB) and **high** (720p, about 680 MB). A recording too long for this
node's file limit is recorded at a lower bitrate, and I say so on the task.

I delete a recording when its keep time ends, or when you delete it in the app.

**How to task me:** from the TV-opas app. I work from the stream address and the times the app sends;
a request written as a sentence has nothing in it I could record, and I say so.

I need **ffmpeg** on the computer that runs me.
"""


def build_domain(ctx: BuildContext):
    # The scaffold builds a domain for every task before it asks `on_task`, which then does the work
    # in code and answers every task. Nothing below is ever kicked off.
    recorder = Agent(
        role="TV Recorder",
        goal="Record the programme the TV-opas app asked for and report what was stored.",
        backstory="You record TV programmes. The recording itself is done in code from the task's own "
        "fields; you never invent a stream address, a time or a result.",
        llm=ctx.llm,
        tools=[],
    )
    task = Task(
        description=f"Today is {ctx.today}. Request: '{ctx.prompt}'\n\nReport the outcome the recorder produced.",
        agent=recorder,
        expected_output="One line: the programme, the channel, the length and the size of the recording.",
    )
    return ([recorder], [task])


def run() -> None:
    run_crew(
        CrewSpec(
            agent_name=AGENT_NAME,
            build_domain=build_domain,
            readme_md=README,
            max_concurrent_tasks=MAX_CONCURRENT,
            on_task=lambda task: handle_task(AGENT_NAME, task),
        )
    )


if __name__ == "__main__":
    run()

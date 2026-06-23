"""social-briefing: a scheduled, human-in-the-loop morning-briefing agent.

The loop (you bring the social data, it structures it):
  1. A daily SCHEDULE fires -> it DMs you ready-to-paste Grok(X)/Reddit queries for the topics it tracks.
  2. You run them and paste the raw results back into the thread.
  3. A curation crew sorts the signals by topic, writes a digest to memory, and replies with an
     assessment + specific thread suggestions to engage with.

Set-up + control happen over DM (or the inbox command chips): 'track topics: a, b, c', 'send my briefing
now', 'brief me daily at 7am', 'stop my daily briefing'. It only ever talks to its OWNER.

Register + approve before running:
  npx aimeat@latest connect add --agent social-briefing --mode task-runner --url https://aimeat.io --owner <your-aimeat-account>
Run: uv run python crews/social_briefing_crew.py
"""

from __future__ import annotations

import sys

from crewai import Agent, Crew, Process, Task

from crewaimeat import dm, social_briefing
from crewaimeat.aimeat_crew import BuildContext, CrewSpec, _now_context, run_crew
from crewaimeat.generator_tool import _discover_owner
from crewaimeat.llm import get_llm
from crewaimeat.scheduler import make_schedule_tools

AGENT_NAME = "social-briefing"

README = """[[FIGLET:slant]["Social Briefing"]]

A scheduled, **human-in-the-loop** morning briefing. Each morning it DMs you ready-to-paste Grok(X) and
Reddit queries for the topics you track; you run them and paste the results back; it sorts the signals by
topic, writes a digest, and suggests specific threads to engage with. You bring the social data — it does
the structuring. Talks only to its owner.

**Control me by DM:** "track topics: AI agents, CrewAI", "send my briefing now", "brief me daily at 7am",
"stop my daily briefing". Then paste your Grok/Reddit results and I'll curate them.
"""

CAPABILITY_TAGS = ["social-briefing", "assistant", "social-radar", "scheduled", "human-in-the-loop"]
CAPABILITIES = {
    "technical": [{"name": "scheduler", "type": "tool"}, {"name": "federated-dm", "type": "tool"}],
    "domain": ["assistant", "social-media", "marketing"],
    "languages": ["en", "fi"],
}

CHAT_COMMANDS = [
    {
        "id": "brief_now",
        "label": "Send my briefing now",
        "description": "Get today's Grok/Reddit queries to run",
        "template": "Send my social briefing now.",
    },
    {
        "id": "track_topics",
        "label": "Track topics",
        "description": "Set the topics I monitor for you",
        "template": "Track these topics for my briefing: {{topics}}.",
        "params": [{"name": "topics", "type": "text", "required": True, "placeholder": "AI agents, CrewAI, AIMEAT"}],
    },
    {
        "id": "brief_daily",
        "label": "Brief me daily",
        "description": "Schedule the briefing at a daily time",
        "template": "Send my morning briefing every day at {{time}}.",
        "params": [{"name": "time", "type": "text", "required": True, "placeholder": "07:00"}],
    },
    {
        "id": "stop_daily",
        "label": "Stop daily briefing",
        "description": "Cancel the scheduled briefing",
        "template": "Stop my daily briefing.",
    },
]


def _curate(pasted: str, topics: list[str], today: str, llm) -> str:
    """LLM curation: raw pasted Grok/Reddit text -> a structured digest (signals by topic + assessment +
    thread suggestions). No tools — the human already gathered the data; the crew only structures it."""
    analyst = Agent(
        role="Social Signal Analyst",
        goal=(
            "Turn raw, messy social-media results the user pasted into a clean, useful briefing: organise "
            "the signals by topic, judge what matters, and point to specific threads worth engaging."
        ),
        backstory=(
            "You read social chatter (X/Grok and Reddit) for a marketing team. You are sharp at separating "
            "signal from noise, you keep every link the user pasted, and you never invent posts that "
            "weren't in the pasted text."
        ),
        llm=llm,
        allow_delegation=False,
        verbose=False,
    )
    task = Task(
        description=(
            f"Today is {today}. The user is tracking these topics: {', '.join(topics)}.\n\n"
            "They pasted these raw results from Grok (X) and/or Reddit:\n\n"
            f"-----\n{pasted}\n-----\n\n"
            "Produce a briefing in markdown with these sections:\n"
            "## By topic\n"
            "For each topic that appears, the key conversations/posts (keep any links verbatim), the "
            "overall sentiment, and any notable people or accounts.\n"
            "## What matters today\n"
            "A short, honest assessment — the 2-4 most important signals, opportunities, and risks.\n"
            "## Threads to join\n"
            "Specific posts/threads worth replying to, each with a one-line ANGLE for how to engage "
            "(only ones actually present in the pasted text; if none, say so).\n\n"
            "Rules: never fabricate posts or links that aren't in the pasted text; if a topic has no "
            "signal, omit it; be concise and skimmable."
        ),
        expected_output="A skimmable markdown briefing: By topic / What matters today / Threads to join.",
        agent=analyst,
    )
    return str(Crew(agents=[analyst], tasks=[task], process=Process.sequential).kickoff())


def _schedule_crew(request: str, today: str, llm) -> str:
    """Handle a schedule request ('brief me daily at 7am' / 'stop my daily briefing') with the schedule
    tools. The kickoff is dispatched back to THIS agent (agent_task, target=self) carrying the marker."""
    mgr = Agent(
        role="Briefing Scheduler",
        goal="Create, change, or cancel the user's daily briefing schedule exactly as they ask.",
        backstory="You manage AIMEAT server-run schedules precisely and confirm what you did.",
        llm=llm,
        tools=make_schedule_tools(AGENT_NAME),
        allow_delegation=False,
        verbose=False,
    )
    task = Task(
        description=(
            f'Today is {today}. The user said: "{request}"\n\n'
            "If they want a DAILY briefing at a time: call schedule_create with kind='agent_task', "
            f"target_agent='{AGENT_NAME}', task_title='Morning briefing kickoff', "
            f"task_description='{social_briefing.KICKOFF_MARKER}', a 5-field cron for that local time "
            "(e.g. 7am -> '0 7 * * *'), timezone 'Europe/Helsinki', display_name 'Morning social briefing', "
            "purpose 'DM the owner the daily briefing queries'. If they want to STOP it: schedule_list, find "
            "the 'Morning briefing kickoff' schedule, and schedule_delete it. Then reply in one short "
            "sentence confirming what you did (include the time)."
        ),
        expected_output="A one-line confirmation of the schedule change.",
        agent=mgr,
    )
    return str(Crew(agents=[mgr], tasks=[task], process=Process.sequential).kickoff())


def _message_body(event: dict) -> str:
    """The triggering message's full body — id-match in the thread, else the wake preview (read-after-write
    safe, like the concierge)."""
    mid, conv, _sender, preview, _subject = dm._inbound_fields(event)
    if conv:
        thread = dm.dm_thread(AGENT_NAME, conv)
        for m in (thread.get("messages") if isinstance(thread, dict) else None) or []:
            if (m.get("id") or m.get("message_id")) == mid and m.get("body"):
                return str(m["body"])
    return str(preview or "")


def build_domain(ctx: BuildContext):
    """Task path. A SCHEDULED kickoff (task carries the marker) sends the briefing to the owner; any other
    assigned task just acknowledges (this agent's real work is the DM loop)."""
    if social_briefing.KICKOFF_MARKER in (ctx.prompt or ""):
        ok = social_briefing.send_kickoff(AGENT_NAME, ctx.today)
        msg = "Morning briefing sent to the owner." if ok else "Briefing kickoff could not be delivered."
    else:
        msg = "social-briefing runs as a DM loop — message me 'send my briefing now' to start."
    note = Agent(
        role="Briefing Runner",
        goal="Report the briefing action result.",
        backstory="You confirm the scheduled briefing action.",
        llm=ctx.llm,
        allow_delegation=False,
        verbose=False,
    )
    task = Task(description=f"State exactly this and nothing else: {msg}", expected_output=msg, agent=note)
    return ([note], [task])


def run() -> None:
    _seen: set = set()

    def _responder(event: dict):
        _mid, conv, sender, _preview, _subject = dm._inbound_fields(event)
        owner = _discover_owner(AGENT_NAME)
        # OWNER-ONLY: ignore anyone who isn't this agent's owner (also blocks sibling-agent loops).
        if not sender or sender.split("@")[0].split("#")[0] != owner:
            return ""
        text = _message_body(event).strip()
        low = text.lower()
        today = _now_context()

        if any(k in low for k in ("briefing now", "brief me now", "send my briefing", "send my social briefing")):
            cfg = social_briefing.set_config(AGENT_NAME, conversation_id=conv)  # this thread is the standing one
            return social_briefing.build_kickoff(cfg["topics"], today)
        if "track" in low and "topic" in low and ":" in text:
            raw = text.split(":", 1)[1].replace("\n", ",")
            topics = [t.strip(" .") for t in raw.split(",") if t.strip(" .")]
            saved = social_briefing.set_topics(AGENT_NAME, topics)
            return (
                f"Got it — I'll track: **{', '.join(saved)}**. Say 'send my briefing now', or 'brief me daily at 7am'."
            )
        if "brief" in low and any(k in low for k in ("daily", "every day", "each morning", "stop", "cancel")):
            try:
                return _schedule_crew(text, today, get_llm(agent_name=AGENT_NAME))
            except Exception as exc:  # noqa: BLE001
                print(f"[{AGENT_NAME}] schedule crew failed: {exc!r}", file=sys.stderr)
                return "I couldn't change the schedule just now — try again?"
        if len(text) < 40:  # too short to be pasted results — guide them
            return (
                "Paste your Grok/Reddit results here and I'll sort them. Or: 'track topics: a, b, c', "
                "'send my briefing now', 'brief me daily at 7am'."
            )
        # Default: treat the message as pasted social results -> curate, persist, and reply.
        try:
            cfg = social_briefing.get_config(AGENT_NAME)
            digest = _curate(text, cfg["topics"], today, get_llm(agent_name=AGENT_NAME))
        except Exception as exc:  # noqa: BLE001
            print(f"[{AGENT_NAME}] curation failed: {exc!r}", file=sys.stderr)
            return "Sorry — I hit an error curating that. Try pasting it again?"
        social_briefing.write_digest(AGENT_NAME, today, digest, cfg["topics"])
        return digest

    run_crew(
        CrewSpec(
            agent_name=AGENT_NAME,
            build_domain=build_domain,
            readme_md=README,
            temperature=0.3,
            listen_for=("tasks", "dms"),
            on_dm=lambda e: dm.handle_dm_event(AGENT_NAME, e, _responder, seen=_seen),
            tags=CAPABILITY_TAGS,
            capabilities=CAPABILITIES,
            chat_commands=CHAT_COMMANDS,
        )
    )


if __name__ == "__main__":
    run()

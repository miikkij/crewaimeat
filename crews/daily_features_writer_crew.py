"""daily-features-writer: DETERMINISTIC evening features + news quiz for (L)AIMEAT Sanomat.

The work runs in code (crewaimeat.features_pipeline.build_features): grok writes koodaus + prompt-niksi +
matikka (each a direct call) and the news quiz (JSON parsed + validated before storing). The old crew skipped
tasks (koodaus/matikka came up empty); here the loop is code, so nothing is dropped. Thin wrapper: the agent
resolves the target date+edition and calls the tool ONCE.

Register + approve, then run:
  npx aimeat@latest connect add --agent daily-features-writer --mode task-runner --url https://aimeat.io --owner <you>
  uv run python crews/daily_features_writer_crew.py
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.features_pipeline import make_features_tools

AGENT_NAME = "daily-features-writer"

# ── This agent's own declaration ─────────────────────────────────────────────────────────────
# The single source for what this agent is: its model routing, how it is discovered, and what it
# promises. These used to live in three central lists (fleet_identity.py / llm_providers.json /
# offers.py) that nothing kept in step, so an agent could — and did — come online missing from
# all of them. crewaimeat.agent_manifest reads these statically; the lists are derived.
LLM_PROFILE = "news"
TAGS = ["features", "news-quiz", "laimeat", "role.task-runner"]
CAPABILITIES = {
    "technical": [{"name": "daily-features-writer", "type": "skill"}],
    "domain": ["daily features + news quiz from the day's articles"],
    "languages": ["fi", "en"],
}
OFFERS = [
    {
        "id": "evening-features",
        "title": "Evening features + the validated news quiz",
        "ask": "I write the koodaus/prompt/matikka features and build the news quiz from the day's articles "
        "(validated; skipped rather than fabricated when articles are missing). Runs on the 17:45 schedule — "
        "ask only for a re-run.",
        "example": "Rebuild today's quiz (evening edition)",
        "cost": "cheap",
        "latency": "minutes",
        "repeatability": "accumulative",
        "verification": "gated",
        "scheduleBorn": "daily 17:45 Europe/Helsinki — runs automatically (quiz self-heal at 18:00)",
        "consequences": [{"type": "publishes-public", "note": "features + quiz are public newspaper content"}],
        "json": True,
        "sample": {
            "edition": "2026-06-16 evening",
            "features": ["koodaus", "prompt", "matikka"],
            "quiz": {
                "title": "Päivän uutisvisa",
                "questions": [
                    {
                        "q": "Mikä oli Suomen Pankin korkopäätös tänään?",
                        "options": ["Nosto", "Lasku", "Ennallaan", "Ei päätöstä"],
                        "answer": 2,
                        "source": "news.2026-06-16.evening.article.talous",
                    },
                    {
                        "q": "Mikä teema hallitsi tekoälyuutisia?",
                        "options": ["Sääntely", "Agenttiparvet", "Kuvageneraatio", "Robotiikka"],
                        "answer": 1,
                        "source": "news.2026-06-16.evening.article.tekoaly",
                    },
                ],
            },
            "note": "validated; skipped rather than fabricated when articles are missing",
        },
    }
]

README = """[[FIGLET:slant]["Features"]]

Writes the evening special sections — **päivän koodausosio (Koodi-Kalle), prompt-niksinurkka (Prompt-Pia),
matematiikkahetki (Matikka-Make)** — and the **interactive news quiz** (5 Q, validated JSON from the day's
news). Deterministic: grok writes each piece in a code loop, nothing skipped.
"""


def build_domain(ctx: BuildContext):
    runner = Agent(
        role="Features Runner",
        goal="Resolve the target date + edition and trigger the deterministic features + quiz build.",
        backstory="You do not write the tidbits or quiz by hand. You read the request, work out the target date "
        "and edition, and call write_features ONCE — the tool writes koodaus, prompt-niksi, matikka "
        "and the validated quiz. You then report what it did.",
        llm=ctx.llm,
        tools=[*make_features_tools(AGENT_NAME)],
    )
    task = Task(
        description=(
            f"Today is {ctx.today}. Request: '{ctx.prompt}'\n\n"
            "1. Resolve the TARGET DATE (YYYY-MM-DD — the date in the request, else today) and EDITION "
            "('evening' if the request mentions ilta/evening, else 'morning').\n"
            "2. Call write_features(date=<resolved>, edition=<resolved>) EXACTLY ONCE. It writes koodaus, "
            "prompt-niksi, matikka and the news quiz — you do NOT write any of them yourself.\n"
            "3. Return the report it gives you."
        ),
        agent=runner,
        expected_output="The write_features report: koodaus/prompt-niksi/matikka char counts + quiz question count.",
    )
    return ([runner], [task])


def run() -> None:
    # Self-healing guard (output-existence, no LLM in the check): the 17:45 schedule can race
    # the 17:25 writers (or the daemon can be down at fire time) — and build_quiz now SKIPS
    # instead of fabricating when articles aren't readable, so "quiz key missing" + retry here
    # converges to a real quiz once the articles land (bit us 2026-06-11: placeholder quiz).
    def _ensure_quiz() -> None:
        import datetime
        from zoneinfo import ZoneInfo

        from crewaimeat.aimeat_crew import _aimeat_call
        from crewaimeat.features_pipeline import build_quiz

        now = datetime.datetime.now(ZoneInfo("Europe/Helsinki"))
        if now.hour < 18:
            return
        date = now.date().isoformat()
        if _aimeat_call(AGENT_NAME, "aimeat_memory_read", {"key": f"news.{date}.evening.quiz"}):
            return
        print(f"[{AGENT_NAME}] self-heal: news.{date}.evening.quiz missing after 18:00 -> rebuilding", flush=True)
        print(build_quiz(AGENT_NAME, date, "evening"), flush=True)

    run_crew(
        CrewSpec(
            agent_name=AGENT_NAME,
            build_domain=build_domain,
            readme_md=README,
            temperature=0.2,
            idle_hook=_ensure_quiz,
            idle_hook_seconds=300,
        )
    )


if __name__ == "__main__":
    run()

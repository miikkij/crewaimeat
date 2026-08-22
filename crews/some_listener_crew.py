"""some-listener: deterministic social-radar scanner (SCANNING ONLY — never posts).

Finds WHERE people discuss agent memory / multi-agent systems / agent infrastructure (Hacker News via the
free Algolia API in v1) and writes a ranked radar to logs/some_radar_<date>.log + memory `some.radar.latest`.
The actual scan runs in plain code (crewaimeat.some_listener.scan_hn) — it is not a judgement left to the LLM.
A human reviews the radar and decides what (if anything) to engage with; this crew contacts no one.

Quick test (no AIMEAT registration needed — writes the log):
  uv run python -c "from crewaimeat.some_listener import scan_hn; print(scan_hn()['log'])"

Run as a crew:
  npx aimeat@latest connect --url https://aimeat.io --owner <you> --agent some-listener
  uv run python crews/some_listener_crew.py
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.some_listener import make_listener_tools

AGENT_NAME = "some-listener"

# ── This agent's own declaration ─────────────────────────────────────────────────────────────
# The single source for what this agent is: its model routing, how it is discovered, and what it
# promises. These used to live in three central lists (fleet_identity.py / llm_providers.json /
# offers.py) that nothing kept in step, so an agent could — and did — come online missing from
# all of them. crewaimeat.agent_manifest reads these statically; the lists are derived.
LLM_PROFILE = "content-free"

# What FIRES this agent. Declared here so `crewaimeat doctor --live` can compare it against the
# node's real schedule list — the check that makes an orphaned or drifted cron visible instead of
# something nobody dares delete because nobody is sure what it would trigger.
SCHEDULE = {
    "cron": "0 8 * * *",
    "timezone": "Europe/Helsinki",
    "purpose": "the daily HN social radar scan",
}

TAGS = ["social-radar", "role.task-runner"]
CAPABILITIES = {
    "technical": [{"name": "some-listener", "type": "skill"}],
    "domain": ["social radar: source HN/X/Reddit engagement opportunities"],
    "languages": ["en"],
}


README = """[[FIGLET:slant]["Some Listener"]]

Deterministic **social radar**: scans Hacker News (free Algolia API) for AIMEAT-relevant discussions
(agent memory, multi-agent, agent infrastructure, CrewAI...) and writes a ranked radar to `logs/` + memory
`some.radar.latest`. **SCANNING ONLY — it never posts, replies, or contacts anyone.** A human reviews the
radar and decides what to engage with.

**How to task me:** "scan" (optionally "last 24h") — I run scan_hn ONCE and report the ranked hits.
"""


def build_domain(ctx: BuildContext):
    listener = Agent(
        role="Social Radar Scout",
        goal="Scan public discussions for places where AIMEAT is genuinely relevant — and ONLY scan.",
        backstory="You find WHERE people are discussing agent memory, multi-agent systems and agent "
        "infrastructure. You call scan_hn ONCE and report the ranked radar. You never post, reply, "
        "vote, or contact anyone — a human reviews your radar and decides. You never fabricate hits.",
        llm=ctx.llm,
        tools=[*make_listener_tools(AGENT_NAME)],
    )

    scan_task = Task(
        description=(
            f"Today is {ctx.today}. Request: '{ctx.prompt}'\n\n"
            "1. Decide the lookback window in hours (default 48; use 24 if the request says today/24h, 168 for a week).\n"
            "2. Call scan_hn(hours=<window>) EXACTLY ONCE. It deterministically scans Hacker News and writes a "
            "ranked radar to the log + memory — you do NOT search or judge by hand.\n"
            "3. Report the top hits it returns (title, link, why it is a fit). Post NOTHING; contact no one."
        ),
        agent=listener,
        expected_output="The scan_hn report: the ranked radar of AIMEAT-relevant Hacker News discussions.",
    )

    return ([listener], [scan_task])


def run() -> None:
    run_crew(CrewSpec(agent_name=AGENT_NAME, build_domain=build_domain, readme_md=README, temperature=0.2))


if __name__ == "__main__":
    run()

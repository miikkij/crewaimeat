"""web-tester — drives a real browser to test web-app flows (login, forms, navigation).

Only build_domain below is crew-specific; crewaimeat.aimeat_crew.run_crew provides the AIMEAT wiring
(onboarding, daemon, liaison publish/complete, live progress, date injection). See SCAFFOLD_CANON.md.
Register + approve before running:
  npx aimeat@latest connect --url https://aimeat.io --owner <your-aimeat-account> --agent web-tester

Needs a browser binary: `uv run playwright install chromium`.

Run: uv run python crews/web_tester_crew.py
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.crew import _browser_tools

AGENT_NAME = "web-tester"

# ── This agent's own declaration ─────────────────────────────────────────────────────────────
# The single source for what this agent is: its model routing, how it is discovered, and what it
# promises. These used to live in three central lists (fleet_identity.py / llm_providers.json /
# offers.py) that nothing kept in step, so an agent could — and did — come online missing from
# all of them. crewaimeat.agent_manifest reads these statically; the lists are derived.
LLM_PROFILE = "coding"
TAGS = ["web-testing", "browser-automation", "vision", "role.task-runner"]
CAPABILITIES = {
    "technical": [
        {"name": "web-tester", "type": "skill"},
        {"name": "playwright", "type": "skill"},
        {"name": "vision", "type": "skill"},
    ],
    "domain": [
        "browser-driven web-flow testing (Playwright)",
        "evidence capture",
        "vision over page SCREENSHOTS it captures (self-captured) — visual verification of what rendered",
    ],
    "languages": ["en"],
}
OFFERS = [
    {
        "id": "test-web-flow",
        "title": "Drive a real browser through a web flow",
        "ask": "Give me a URL and a flow (login, form, navigation) and I drive a real browser through it and report "
        "what happened with evidence. I interact with the page — point me at test data, not "
        "production-critical state.",
        "example": "Test that the public newspaper page renders and the quiz accepts answers",
        "cost": "cheap",
        "latency": "minutes",
        "repeatability": "accumulative",
        "verification": "gated",
        "consequences": [
            {
                "type": "mutates-live-app",
                "note": "clicks and types against the target; interactions can change app state",
            }
        ],
        "sample": "## Web flow test — public newspaper + quiz\n"
        "\n"
        "1. GET / → rendered (200, front-page index present) ✓\n"
        "2. Click first quiz option → answer accepted, score updated ✓\n"
        "3. Anonymous viewer → reads front page, cannot edit ✓\n"
        "\n"
        "**Result: PASS** (3/3). Evidence: screenshots + DOM assertions attached.\n"
        "\n"
        "…",
    }
]


README = """[[FIGLET:slant]["Web Tester"]]

Drives a real headless browser to test web-app flows — navigate, fill forms, click, log in, and
verify results (reading page content or describing a screenshot with a vision model). Give it a
target URL + what to test; it plans the browser steps, runs them in one session, and reports what
passed/failed.

**How to task me:** Give me a URL and the flow to verify (e.g. "log in with X/Y and confirm the
dashboard loads"). I plan the browser actions, run them, and report each step ✓/✗ with evidence.
"""


def build_domain(ctx: BuildContext):
    """Build a single-agent crew that plans a browser action list and executes it in one session."""

    # Per-agent login profile so cookies persist across this crew's runs without colliding with others.
    tester = Agent(
        role="Web Automation Tester",
        goal="Test the requested web-app flow with a real browser and report exactly what happened, step by step.",
        backstory=(
            "You are a meticulous QA automation engineer. You PLAN the full ordered list of browser "
            "actions, then call the Browser tool ONCE with that list (plan-then-execute). You use precise "
            "CSS selectors, verify outcomes by reading page content (or describing a screenshot when the "
            "result is visual), and report each step's ✓/✗ honestly with the evidence you saw."
        ),
        tools=_browser_tools(profile=AGENT_NAME),
        llm=ctx.llm,
        verbose=True,
    )

    test_task = Task(
        description=(
            f"Date context: {ctx.today}\n\nTesting goal:\n{ctx.prompt}\n\n"
            "Plan the browser actions needed, then call the Browser tool ONCE with the full ordered "
            "`actions` list (navigate → fill → click → get_content/screenshot to verify). Pass a `profile` "
            "if the flow needs a persisted login. To judge a visual result, use a `screenshot` action with "
            "`describe: true`. Then report each step's result and whether the flow passed, citing exactly "
            "what you saw on the page (text or vision description) as evidence."
        ),
        agent=tester,
        expected_output=(
            "A step-by-step test report (each action ✓/✗) with a clear pass/fail verdict and on-page "
            "evidence for the verdict."
        ),
    )

    return [tester], [test_task]


def run() -> None:
    # verify="on": a reviewer checks the report is grounded in what the browser actually returned.
    # Browser action-planning wants determinism, not divergence — run cool.
    run_crew(CrewSpec(agent_name=AGENT_NAME, build_domain=build_domain, readme_md=README, verify="on", temperature=0.2))


if __name__ == "__main__":
    run()

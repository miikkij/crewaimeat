"""web-tester — drives a real browser to test web-app flows (login, forms, navigation).

Only build_domain below is crew-specific; crewaimeat.aimeat_crew.run_crew provides the AIMEAT wiring
(onboarding, daemon, liaison publish/complete, live progress, date injection). See SCAFFOLD_CANON.md.
Register + approve before running:
  npx aimeat@latest connect add --agent web-tester --mode task-runner --url https://aimeat.io --owner <your-aimeat-account>

Needs a browser binary: `uv run playwright install chromium`.

Run: uv run python crews/web_tester_crew.py
"""
from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.crew import _browser_tools

AGENT_NAME = "web-tester"

README = '''[[FIGLET:slant]["Web Tester"]]

Drives a real headless browser to test web-app flows — navigate, fill forms, click, log in, and
verify results (reading page content or describing a screenshot with a vision model). Give it a
target URL + what to test; it plans the browser steps, runs them in one session, and reports what
passed/failed.

**How to task me:** Give me a URL and the flow to verify (e.g. "log in with X/Y and confirm the
dashboard loads"). I plan the browser actions, run them, and report each step ✓/✗ with evidence.
'''


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
    run_crew(CrewSpec(agent_name=AGENT_NAME, build_domain=build_domain, readme_md=README, verify="on"))


if __name__ == "__main__":
    run()

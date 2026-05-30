"""crew-forge: an agent that makes agents (and keeps them running).

Queue it a plain-language request ("make me an agent that drafts release notes") and it
designs a new task-runner crew, writes + validates its build_domain on the locked AIMEAT
scaffold, registers the new agent (aimeat connect add), and launches it under the
watchdog. You approve the new agent once in the dashboard; then it is live — the new crew
waits patiently for that approval, so nothing crash-loops while you get to it.

crew-forge also operates the fleet. Send it a TASK or a MESSAGE that starts with a
management verb and it acts instead of building:
  - "restart release-notes-writer"  -> relaunches that crew if it is down
  - "list"  /  "status"             -> reports which crews are running
This is the unattended-service path: when a crew goes offline, message crew-forge to
bring it back — no console needed.

Generated work lands in crews/<new-agent>_crew.py. crew-forge edits nothing in the
scaffold — every crew it makes reuses crewaimeat.aimeat_crew.run_crew. Register first:
  aimeat connect add --agent crew-forge --mode task-runner --url https://aimeat.io --owner <your-aimeat-account>

Run: uv run python crews/crew_forge_crew.py
Needs AIMEAT_OWNER in .env so it can register the agents it builds under your account.
"""

from __future__ import annotations

import re

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.forge import make_forge_tools, make_manage_tools

AGENT_NAME = "crew-forge"

# A request that starts with one of these verbs is a fleet-management request (restart /
# report), not a build request. Build requests say "build / make / create an agent that ...".
_MANAGE_RE = re.compile(r"^\s*(restart|relaunch|reboot|status|list|ls|stop)\b", re.IGNORECASE)


def _is_management_request(text: str) -> bool:
    return bool(_MANAGE_RE.match(text or ""))


def build_domain(ctx: BuildContext) -> tuple[list[Agent], list[Task]]:
    if _is_management_request(ctx.prompt):
        return _manage_domain(ctx)
    return _build_domain(ctx)


def _manage_domain(ctx: BuildContext) -> tuple[list[Agent], list[Task]]:
    """Operate the fleet: relaunch a downed crew, or report status."""
    operator = Agent(
        role="Fleet Operator",
        goal="Keep the user's crews running: relaunch one that is down, or report what is running",
        backstory=(
            "You operate a fleet of AIMEAT crews on this machine. You do not build crews; you bring "
            "them back online and report status, using your tools and nothing else."
        ),
        tools=make_manage_tools(),
        llm=ctx.llm,
        verbose=True,
    )
    task = Task(
        description=(
            "Handle this fleet-management request, then report what you did. Request:\n"
            f"{ctx.prompt}\n\n"
            "- To restart / relaunch / bring back a crew, call restart_crew with its agent name "
            "(the kebab name, e.g. 'release-notes-writer').\n"
            "- To list crews or report status, call list_crews.\n"
            "If the request actually asks to BUILD a new agent, say so and ask the user to resend it "
            "as a plain build request (e.g. 'build an agent that ...'). Use one tool call at a time."
        ),
        expected_output="A short report of the action taken (what was relaunched, or the status list).",
        agent=operator,
    )
    return [operator], [task]


def _build_domain(ctx: BuildContext) -> tuple[list[Agent], list[Task]]:
    llm, today, request = ctx.llm, ctx.today, ctx.prompt

    architect = Agent(
        role="Crew Architect",
        goal="Turn a plain-language request into a concrete new crew: a kebab agent name and a complete build_domain",
        backstory=(
            "You design small, focused CrewAI task-runner crews. You know the AIMEAT scaffold inside "
            "out: the author writes only build_domain(ctx), passes ctx.llm to every agent, uses "
            "_web_tools() for web search, and the last task's output is the published deliverable. "
            "You write clean, minimal Python that the builder can use verbatim."
        ),
        llm=llm,
        verbose=True,
    )
    builder = Agent(
        role="Crew Builder",
        goal="Materialize the designed crew: write and validate its file, register the agent, and launch it",
        backstory=(
            "You are careful and methodical. You write the crew file, fix it until the validator is "
            "happy, then register and launch it exactly once. You never touch the scaffold wiring."
        ),
        tools=make_forge_tools(),
        llm=llm,
        verbose=True,
    )

    design = Task(
        description=(
            f"{today}\n\n"
            "A user wants a NEW AIMEAT task-runner crew. Design it and write its code. Request:\n"
            f"{request}\n\n"
            "The new crew runs on the same locked scaffold this crew uses, so you design ONLY "
            "build_domain. Rules:\n"
            "- Pick a short, descriptive kebab-case agent name (e.g. 'release-notes-writer'), "
            "distinct from obvious existing ones.\n"
            "- Choose 2-4 specialist agents (role / goal / backstory) that fit the purpose.\n"
            "- Write a complete `def build_domain(ctx):` that:\n"
            "    * builds those Agents, passing llm=ctx.llm to each; add tools=_web_tools() to any "
            "agent that needs web search;\n"
            "    * creates Tasks in order; the LAST task's output is the published deliverable;\n"
            "    * gives the agent that needs the user's request ctx.prompt; prepend ctx.today to any "
            "time-sensitive Task; set context=[...] on a Task that builds on earlier ones;\n"
            "    * returns (agents, tasks) as a 2-tuple of lists.\n"
            "- Do NOT write imports, AGENT_NAME, run(), or any AIMEAT/onboarding/daemon/memory code — "
            "the scaffold provides all of it. Agent, Task, BuildContext and _web_tools are already "
            "imported in the target file.\n\n"
            "Output EXACTLY these three labeled sections, nothing else, so the builder can use them "
            "verbatim:\n"
            "AGENT_NAME: <kebab-name>\n"
            "EXTRA_IMPORTS:\n"
            "<extra import lines, or leave this empty>\n"
            "BUILD_DOMAIN:\n"
            "<the full def build_domain(ctx): ... function text>"
        ),
        expected_output=(
            "The three labeled sections AGENT_NAME, EXTRA_IMPORTS, BUILD_DOMAIN, with a complete, "
            "ready-to-write build_domain function."
        ),
        agent=architect,
    )
    build = Task(
        description=(
            "Bring the Crew Architect's design above to life. Work ONE tool call at a time — never "
            "fire several in the same turn.\n"
            "1. Call write_and_validate_crew with the architect's AGENT_NAME (as agent_name), the "
            "BUILD_DOMAIN code (as build_domain_code), and EXTRA_IMPORTS (as extra_imports).\n"
            "2. If it returns INVALID, fix the build_domain code from the error and call "
            "write_and_validate_crew again. Repeat until it returns VALID.\n"
            "3. Once VALID, call register_and_launch_crew ONCE with the same agent_name.\n"
            "4. Report as the final answer: the new agent's name and file, whether it registered, the "
            "exact approve step the user must do on the AIMEAT dashboard, the watchdog log path, and "
            "how to queue its first task. Keep it short and actionable."
        ),
        expected_output=(
            "A short report: new agent name + file, registration + launch status, the dashboard "
            "approve step, and how to queue the first task."
        ),
        agent=builder,
        context=[design],
    )

    return [architect, builder], [design, build]


def run() -> None:
    run_crew(
        CrewSpec(
            agent_name=AGENT_NAME,
            build_domain=build_domain,
            # Act on inbox messages too, so the fleet can be operated by messaging crew-forge.
            listen_for=("tasks", "messages"),
        )
    )


if __name__ == "__main__":
    run()

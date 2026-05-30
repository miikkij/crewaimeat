"""crew-forge: an agent that makes agents (and keeps them running).

Queue it a plain-language request ("make me an agent that drafts release notes") and it
designs a new task-runner crew, writes + validates its build_domain on the locked AIMEAT
scaffold, registers the new agent (aimeat connect add), and launches it under the
watchdog. You approve the new agent once in the dashboard; then it is live — the new crew
waits patiently for that approval, so nothing crash-loops while you get to it.

crew-forge also operates the fleet via slash commands (send as a task OR an inbox message):
  /build <description>   design, register, and launch a new agent
  /restart <agent>       bring a stopped crew back online
  /reauth <agent>        re-run authorization so you can approve it again
  /list  (or /status)    show your crews and which are running
  /help                  show the command list
Plain text with no leading "/" is treated as a /build request. This is the unattended path:
when a crew goes offline, message crew-forge "/restart <agent>" to bring it back — no console.

Generated work lands in crews/<new-agent>_crew.py. crew-forge edits nothing in the
scaffold — every crew it makes reuses crewaimeat.aimeat_crew.run_crew. Register first:
  npx aimeat@latest connect add --agent crew-forge --mode task-runner --url https://aimeat.io --owner <your-aimeat-account>

Run: uv run python crews/crew_forge_crew.py
Needs AIMEAT_OWNER in .env so it can register the agents it builds under your account.
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.forge import make_forge_tools, make_manage_tools

AGENT_NAME = "crew-forge"

# crew-forge is driven by explicit slash commands (sent as a task or an inbox message).
# Plain text with no leading "/" is treated as a /build request, so casual asks still work.
HELP = (
    "crew-forge commands (send as a task or a message):\n"
    "  /build <description>   design, register, and launch a new agent\n"
    "  /restart <agent>       bring a stopped crew back online\n"
    "  /reauth <agent>        re-run authorization so you can approve it again\n"
    "  /list   (or /status)   show your crews and which are running\n"
    "  /help                  show this list\n"
    "Plain text with no leading '/' is treated as a /build request."
)
# Published to memory key agents.crew-forge.commands (owner) at startup so the Messages UI shows
# this command palette. Uses the {name, description, category} shape AIMEAT expects.
COMMANDS = [
    {"name": "/build", "description": "Design, register, and launch a new AIMEAT agent from a description", "category": "fleet"},
    {"name": "/restart", "description": "Bring a stopped crew back online: /restart <agent>", "category": "fleet"},
    {"name": "/reauth", "description": "Re-run authorization so you can approve an agent again: /reauth <agent>", "category": "fleet"},
    {"name": "/list", "description": "Show your crews and which are running", "category": "fleet"},
    {"name": "/status", "description": "Alias of /list: crews and their running state", "category": "fleet"},
    {"name": "/help", "description": "List crew-forge's slash commands", "category": "meta"},
]

_BUILD_CMDS = {"build", "new", "make", "create"}
_RESTART_CMDS = {"restart", "relaunch", "reboot", "start"}
_LIST_CMDS = {"list", "ls", "status"}
_HELP_CMDS = {"help", "commands", "?"}

# Declared at onboarding (aimeat_onboarding_declare_services) so other agents and the owner
# can discover how to drive crew-forge.
COMMAND_SERVICES = [
    {"name": "build-agent", "description": "/build <description> — design, register, and launch a new AIMEAT agent"},
    {"name": "restart-agent", "description": "/restart <agent> — bring a stopped crew back online"},
    {"name": "reauth-agent", "description": "/reauth <agent> — re-run authorization so the owner can approve it again"},
    {"name": "list-agents", "description": "/list (or /status) — show the crews and which are running"},
    {"name": "help", "description": "/help — show crew-forge's command list"},
]


def build_domain(ctx: BuildContext) -> tuple[list[Agent], list[Task]]:
    text = (ctx.prompt or "").strip()
    if not text.startswith("/"):
        return _build_domain(ctx, request=text)  # plain text = a build request
    parts = text[1:].split(None, 1)
    cmd = parts[0].lower() if parts else "help"
    arg = parts[1].strip() if len(parts) > 1 else ""
    if cmd in _BUILD_CMDS:
        return _build_domain(ctx, request=arg or text)
    return _command_domain(ctx, cmd, arg)


def _command_domain(ctx: BuildContext, cmd: str, arg: str) -> tuple[list[Agent], list[Task]]:
    """Run one fleet command (restart / reauth / list / help) via the Fleet Operator."""
    operator = Agent(
        role="Fleet Operator",
        goal="Carry out fleet commands precisely: restart or re-auth a crew, or report status",
        backstory=(
            "You operate a fleet of AIMEAT crews on this machine. You do not design crews; you run "
            "the requested command with your tools and report the result, nothing more."
        ),
        tools=make_manage_tools(),
        llm=ctx.llm,
        verbose=True,
    )

    if cmd in _LIST_CMDS:
        instr = "Call list_crews and report its result verbatim."
    elif cmd in _RESTART_CMDS:
        instr = (
            f"Call restart_crew with agent_name='{arg}' and report the result."
            if arg
            else "No agent name was given. Reply asking the user to send '/restart <agent>'."
        )
    elif cmd == "reauth":
        instr = (
            f"Call reauth_crew with agent_name='{arg}' and report the result."
            if arg
            else "No agent name was given. Reply asking the user to send '/reauth <agent>'."
        )
    elif cmd in _HELP_CMDS:
        instr = f"Report exactly this text as the final answer, with no changes:\n{HELP}"
    else:
        instr = f"There is no '/{cmd}' command. Report exactly this text as the final answer:\n{HELP}"

    task = Task(
        description=(
            "You operate a fleet of AIMEAT crews. Do exactly this, one tool call at a time:\n" + instr
        ),
        expected_output="A short report of the action taken, or the requested information.",
        agent=operator,
    )
    return [operator], [task]


def _build_domain(ctx: BuildContext, request: str | None = None) -> tuple[list[Agent], list[Task]]:
    llm, today = ctx.llm, ctx.today
    request = request if request is not None else ctx.prompt

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
            # Declare capabilities at onboarding (Services), and publish the slash-command
            # palette to agents.crew-forge.commands (owner) so the Messages UI surfaces it.
            services=COMMAND_SERVICES,
            commands=COMMANDS,
        )
    )


if __name__ == "__main__":
    run()

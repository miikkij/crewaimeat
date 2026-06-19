"""aimeat-crew-forge — forges AIMEAT-SDLC specialist agents (the self-extending SDLC).

Like crew-forge, but every agent it makes is an AIMEAT app/extension builder on the direct-build
toolkit (make_author_tools): it reads the live node APIs, authors a cortex (+ optional server-side
extension) + an app, installs/publishes them, and verifies with verify_render. aimeat-app-conductor
ORDERS this forge when it needs a specialist for a new domain it has no agent for; the forge designs →
writes → validates → registers → launches the new specialist (you approve it once in the dashboard).

It mirrors the public pattern (workflow-manager → crew-forge → new agents) but stays in the
AIMEAT-prefixed family and reuses crew-forge's proven machinery (forge.write_and_validate_crew +
register_and_launch_crew). Only the Architect's brief is AIMEAT-SDLC-specific.

Prereqs (one time): npx aimeat@latest connect add --agent aimeat-crew-forge --mode task-runner
  --url https://aimeat.io --owner <you>  then approve it + assign the shared tag "workflow" so the
  conductor can delegate to it. AIMEAT_OWNER in .env so it can register the agents it forges.
Run: uv run python crews/aimeat_crew_forge_crew.py
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.forge import make_forge_tools

AGENT_NAME = "aimeat-crew-forge"

README = """[[FIGLET:slant]["aimeat crew forge"]]

I forge AIMEAT-SDLC specialist agents — app/extension builders on the direct-build toolkit. Tell me the
specialist you need (e.g. "an extension-builder that authors ext+cortex+app stacks") and I design, write,
validate, register, and launch it. You approve the new agent once. aimeat-app-conductor can order me.
"""

# A known-good AIMEAT-SDLC build_domain shape the Architect ADAPTS to the requested domain. It mirrors
# aimeat-app-builder: one specialist agent on make_author_tools, three tasks (Design -> Build -> Verify),
# the last gated by verify_render. (Triple-quoted so it embeds verbatim in the Architect's brief.)
_SHAPE = """def build_domain(ctx):
    tid = (ctx.task or {}).get("id") or "manual"
    author_tools, _state = make_author_tools(AGENT_NAME, task_id=tid)
    wf = make_workflow_tools(coordinator_name=AGENT_NAME, run_id=tid, task_id=tid, tag="workflow", timeout=1800)
    deleg = [t for t in wf if getattr(t, "name", "") in ("discover_crews", "delegate_and_wait")]
    specialist = Agent(
        role="<AIMEAT ... Specialist>",
        goal="<author + install + publish + verify the ... for the user's idea>",
        backstory="<expert; uses make_author_tools; STARTS the app HTML from read_app_template() (correct auth "
                  "wiring: loads aimeat-auth.js+aimeat-data.js, mounts the login bar, runs startApp(session) ONLY "
                  "after await AIMEAT.auth.login() — no boot-order race); inside boot() adds loadScript for the "
                  "cortex + every AIMEAT.<lib> it uses; AUTHORS STRICTLY AGAINST read_lib_api (it covers /v1/libs AND "
                  "/lib libs and reports each lib's methods AND emitted EVENT names) — never GUESSES an API/event name; "
                  "for server logic authors an extension (export default async function(ctx,input))>",
        tools=[*author_tools, *deleg], llm=ctx.llm, max_iter=60, allow_delegation=False, verbose=True)
    design = Task(description=f"{ctx.today}\\n\\nDESIGN. <<IDEA>>\\n{ctx.prompt}\\n<</IDEA>>\\n"
                  "read_lib_api + read_app_template + read_node_api + read_cortex_example; decide cortex(+extension?)+app; key map.",
                  expected_output="a compact design", agent=specialist)
    build = Task(description="BUILD. Author ONE cortex (+ extension if server logic is needed) + ONE app STARTED "
                 "from read_app_template() (keep its await-login/startApp boot order; inside boot() add loadScript "
                 "for the cortex + every needed AIMEAT.<lib>); install_cortex (install_extension for server logic); publish_app.",
                 expected_output="cortex/extension installed + app published with the live URL", agent=specialist, context=[design])
    verify = Task(description="VERIFY. verify_render(filename, expect_csv) until VERIFY PASS; fix + retry <=3. "
                  "If the app is INTERACTIVE (click/type to make something happen), ALSO verify_interaction("
                  "filename, steps_json) — drive the core feature with the app's REAL selectors "
                  "(fill/click/wait_enabled/expect_text) and fix until INTERACTION PASS (render alone gives a "
                  "FALSE pass on a broken feature). Not done until both gates pass.",
                  expected_output="verify_render PASS (and verify_interaction PASS for interactive apps) + live URL",
                  agent=specialist, context=[build])
    return [specialist], [design, build, verify]
"""


def build_domain(ctx: BuildContext) -> tuple[list[Agent], list[Task]]:
    architect = Agent(
        role="AIMEAT SDLC Crew Architect",
        goal="Design a new AIMEAT-SDLC specialist crew (a build_domain on the direct-build toolkit) for the requested domain",
        backstory=(
            "You design focused AIMEAT app/extension builder crews. Every crew you design uses "
            "make_author_tools(AGENT_NAME, task_id=tid) — read_lib_api, read_cortex_example, read_app_template, "
            "read_node_api, read_app_stack, install_cortex, install_extension, invoke_extension, publish_app, "
            "seed_memory, app_inline_url, verify_render — to author, install, publish and VERIFY real AIMEAT apps "
            "directly (no generator). You know the gotchas cold: every app STARTS from read_app_template() (it "
            "wires auth correctly — loads aimeat-auth.js + aimeat-data.js, mounts the login bar, and runs "
            "startApp(session) ONLY after `await AIMEAT.auth.login()`, so there is no boot-order race); inside "
            "boot() you add a loadScript for the cortex + every AIMEAT.<lib> it uses; server-side logic is an "
            "extension (one top-level `export default async function (ctx, input)`); the build is not done "
            "until verify_render returns VERIFY PASS."
        ),
        llm=ctx.llm,
        verbose=True,
    )
    builder = Agent(
        role="Crew Builder",
        goal="Write, validate, register, and launch the designed AIMEAT-SDLC crew",
        backstory=(
            "You are careful and methodical. You write the crew file, fix it until the validator is happy, "
            "then register and launch it exactly once. You never touch the scaffold wiring."
        ),
        tools=make_forge_tools(),
        llm=ctx.llm,
        verbose=True,
    )

    design = Task(
        description=(
            f"{ctx.today}\n\n"
            "Forge a NEW AIMEAT-SDLC specialist crew. Request:\n"
            f"{ctx.prompt}\n\n"
            "You design ONLY build_domain, on the direct-build toolkit. ADAPT this known-good shape to the "
            "requested domain (change roles/goals/prompts; keep the make_author_tools wiring + the "
            "Design->Build->Verify structure with the final verify_render gate):\n\n"
            f"{_SHAPE}\n"
            "Rules:\n"
            "- Pick a short kebab agent name prefixed 'aimeat-' (e.g. 'aimeat-extension-builder'), distinct "
            "from existing agents.\n"
            "- The specialist passes llm=ctx.llm and uses make_author_tools (provided via EXTRA_IMPORTS). "
            "AGENT_NAME is a module global in the generated file — reference it as-is.\n"
            "- 3 tasks (design/build/verify); the LAST verifies with verify_render until PASS; "
            "return (agents, tasks).\n\n"
            "Output EXACTLY these five labeled sections, nothing else:\n"
            "AGENT_NAME: <kebab aimeat-name>\n"
            "TEMPERATURE: 0.4\n"
            "EXTRA_IMPORTS:\n"
            "from crewaimeat.author_tool import make_author_tools\n"
            "from crewaimeat.workflow import make_workflow_tools\n"
            "README:\n"
            '<short README markdown for the new crew: a [[FIGLET:slant]["..."]] line + one-line purpose + '
            "a 'How to task me' line>\n"
            "BUILD_DOMAIN:\n"
            "<the full def build_domain(ctx): ... using make_author_tools>"
        ),
        expected_output=(
            "The five sections AGENT_NAME, TEMPERATURE, EXTRA_IMPORTS, README, BUILD_DOMAIN, with a complete "
            "build_domain that uses make_author_tools and ends in a verify_render gate."
        ),
        agent=architect,
    )
    build = Task(
        description=(
            "Bring the Architect's design to life. Work ONE tool call at a time.\n"
            "1. write_and_validate_crew(agent_name=<AGENT_NAME>, build_domain_code=<BUILD_DOMAIN>, "
            "extra_imports=<EXTRA_IMPORTS>, readme_md=<README>, temperature=<TEMPERATURE>).\n"
            "2. If it returns INVALID, fix the build_domain from the error and call it again until VALID.\n"
            "3. Once VALID, call register_and_launch_crew(agent_name) ONCE.\n"
            "4. Report: the new agent name + file, register/launch status, the EXACT dashboard approve step "
            "the owner must do, the watchdog log path, and how to queue its first task. Short + actionable."
        ),
        expected_output=(
            "A short report: new agent name + file, registration + launch status, the approve step, and how to task it."
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
            readme_md=README,
            temperature=0.4,
            poll_seconds=30,
            listen_for=("tasks", "messages"),
        )
    )


if __name__ == "__main__":
    run()

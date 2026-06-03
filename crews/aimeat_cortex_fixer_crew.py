"""aimeat-cortex-fixer — the repair specialist of the AIMEAT SDLC family (direct-build).

Given an existing direct-build app (a cortex name and/or an app filename) and the EXACT failure from
the conductor's verify gate (a syntax error, a render/console error, raw i18n keys, an empty list, a
wrong API call), it RE-AUTHORS the failing artifact correctly and re-installs / re-publishes it.
It fixes named artifacts; it does not design whole apps. The conductor (aimeat-app-conductor) calls
it when verification fails, then re-verifies.

Because install_cortex / publish_app run the deterministic syntax gates, a fix that is still broken is
caught here too — the fixer iterates until the artifact is genuinely clean (or reports the blocker),
never re-shipping broken code.

Prerequisites (human-gated, one time):
  - `npx aimeat@latest connect add --agent aimeat-cortex-fixer --mode task-runner --url <node> --owner <you>`
    then approve in the dashboard.
  - Assign the shared tag "workflow" so the conductor can delegate to it.
  - Cortex install needs the owner's agent-write grant deployed (app publish works for agents already).
Run:  uv run python crews/aimeat_cortex_fixer_crew.py
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.author_tool import make_author_tools

AGENT_NAME = "aimeat-cortex-fixer"

README = """[[FIGLET:slant]["aimeat cortex fixer"]]

I repair a broken AIMEAT direct-build app. Give me the cortex name and/or app filename and the EXACT
gate failure (the error + where), and I re-author that artifact correctly — fixing JS syntax, making
memory reads use the same key prefix as the writes, matching the real lib API (login/getSession, not
ensureSession), fixing the render — then re-install / re-publish (the syntax gates re-check). I fix
named artifacts; I do not design whole apps. The conductor calls me when verification fails.
"""


def build_domain(ctx: BuildContext) -> tuple[list[Agent], list[Task]]:
    tid = (ctx.task or {}).get("id") or "manual"
    author_tools, _state = make_author_tools(AGENT_NAME, task_id=tid)

    fixer = Agent(
        role="AIMEAT App Repair Specialist",
        goal=(
            "Make the named failing artifact (cortex and/or app) pass verification by re-authoring it "
            "correctly, then re-install / re-publish it — without ever shipping still-broken code."
        ),
        backstory=(
            "You are a focused debugging specialist for AIMEAT direct-build apps. You take a cortex "
            "name and/or app filename plus an exact failure and re-author ONLY that artifact, heeding "
            "the error so you do not repeat it. You know the common breaks cold: a JS syntax slip "
            "(e.g. a missing dot, `el className` -> `el.className`); memory reads that use a different "
            "key prefix than the writes (read `<prefix>.<key>` exactly as written, never a bare key); "
            "calling an API that does not exist (this node's auth lib has login()/getSession(), NOT "
            "ensureSession() — confirm with read_lib_api); and render bugs (unescaped text, raw i18n "
            "keys, '[object Object]'). You re-submit through install_cortex / publish_app (the syntax "
            "gates re-check your fix) and only consider it done when it deploys clean."
        ),
        tools=[*author_tools],
        llm=ctx.llm,
        max_iter=50,
        allow_delegation=False,
        verbose=True,
    )

    fix = Task(
        description=(
            f"{ctx.today}\n\n"
            "Repair the failing artifact(s) in an existing direct-build app. The request below contains "
            "the cortex name and/or app filename and the EXACT gate failure:\n\n"
            f"<<FIX REQUEST>>\n{ctx.prompt}\n<</FIX REQUEST>>\n\n"
            "Steps:\n"
            "1. If the error touches an API call, read_lib_api(<lib>) to confirm the REAL method names "
            "before you change code. If it touches the cortex manifest, read_cortex_example() for the "
            "exact schema.\n"
            "2. Identify which artifact is wrong (the cortex lib, the cortex manifest, or the app HTML) "
            "and re-author ONLY that, correcting the reported problem:\n"
            "   - JS syntax error -> fix it.\n"
            "   - 404 / empty list / wrong data -> make the memory reads use the SAME prefix as the "
            "writes (e.g. AIMEAT.data.list({prefix:'activity.'}), get('activity.<id>')), never a bare key.\n"
            "   - 'X is not a function' -> use the real lib method (login/getSession, data.list/get/"
            "getPublic/set).\n"
            "   - raw i18n keys / '[object Object]' / blank render -> fix the render (escape text, read "
            "the right value shape, call the right cortex method).\n"
            "3. Re-deploy: install_cortex(name, manifest_yaml, libs_json) for a cortex fix, and/or "
            "publish_app(filename, html, ...) for an app fix. If a tool returns PRE-INSTALL/PRE-PUBLISH "
            "BLOCKED or an error, read the exact reason, correct it, and resubmit (at most 3 rounds per "
            "artifact). If install returns INSTALL DENIED (403), report it — the node needs the agent "
            "cortex-install grant.\n"
            "4. Report each artifact you fixed -> redeployed (with its served/live URL), and any you "
            "could not fix after 3 rounds, with the exact blocking error. Be honest; do not claim a fix "
            "that did not deploy."
        ),
        expected_output=(
            "A per-artifact result: each fixed artifact -> redeployed (with URL), or -> still blocked "
            "with the exact error. Be honest; do not claim a fix that did not deploy."
        ),
        agent=fixer,
    )

    return [fixer], [fix]


def run() -> None:
    run_crew(
        CrewSpec(
            agent_name=AGENT_NAME,
            build_domain=build_domain,
            readme_md=README,
            temperature=0.4,
            poll_seconds=30,
        )
    )


if __name__ == "__main__":
    run()

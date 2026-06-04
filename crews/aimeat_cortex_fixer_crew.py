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
            "name and/or app filename plus an exact failure and re-author ONLY that artifact, addressing "
            "that exact cause. The app uses ONE cortex — repair the existing cortex in place; keep it a "
            "single cortex rather than splitting it. You know the common breaks cold: a JS syntax slip "
            "(e.g. a missing dot, `el className` -> `el.className`); memory reads that use a different "
            "key prefix than the writes (read `<prefix>.<key>` exactly as written, never a bare key); "
            "calling an API that does not exist (this node's auth lib has login()/getSession(), NOT "
            "ensureSession() — confirm with read_lib_api); and render bugs (unescaped text, raw i18n "
            "keys, '[object Object]'). You re-submit through install_cortex / publish_app (the syntax "
            "gates re-check your fix) and consider it done only when the artifact deploys clean AND a "
            "verify gate PASSES."
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
            "0. PRECONDITION (fail loud, never guess): if the FIX REQUEST does not contain a resolvable app "
            "inline URL AND an exact failure/error, report 'BLOCKED: missing app inline URL and/or exact "
            "failure' and return — do not guess a target (the conductor owns re-specifying).\n"
            "0b. PRESERVE EVERYTHING THAT WORKS. Before changing anything, read_app_stack(<app inline URL>) to "
            "map the artifacts, then read_app_source(<app inline URL>) to load the FULL current HTML + cortex "
            "lib source, and EDIT IN PLACE — fix ONLY the reported failure and keep every existing feature. If "
            "the FIX REQUEST carries a SPEC / feature list, that is the COMPLETE set the app must still "
            "satisfy; dropping a spec'd feature while repairing another (e.g. removing a working create/post "
            "form while you fix the viewer) is a REGRESSION, not a fix. Re-author from the current source, not "
            "from memory.\n"
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
            "3.5. CONFIRM THE FIX ACTUALLY RENDERS — a clean redeploy is NOT done. install_cortex/publish_app "
            "run only a SYNTAX check (node --check); a parseable artifact can still fail at runtime, which is "
            "exactly the class of bug you were sent to repair (raw i18n keys, '[object Object]', a blank "
            "render, a boot-order race, an empty list from a wrong key prefix — they ALL pass node --check). "
            "After a clean redeploy, run verify_render(filename, expect_csv) with expect_csv = a few strings "
            "that MUST appear. ALSO run verify_anon_render(filename, expect_csv) if the app is a PUBLIC / "
            "anon-readable viewer, and verify_interaction(filename, steps_json) if the original failure was "
            "BEHAVIORAL (a control that does not work) — using ONLY non-destructive, read-or-create-your-own "
            "steps (never click controls that hide/delete/modify shared or other users' content; a test must "
            "not mutate live/shared state). If a gate returns FAIL, read its reason, feed it back into steps "
            "1-3, re-author, redeploy, and re-run the gate — AT MOST 3 rounds. A fix is DONE only when the "
            "applicable gate(s) return PASS, not when the redeploy merely succeeds. If an artifact still "
            "fails its gate after 3 rounds, STOP redeploying it — report it as blocked with the failing-gate "
            "reason rather than republishing further (each redeploy mutates the live app, so endless retries "
            "churn live state).\n"
            "4. Report each artifact you fixed -> redeployed -> verified (with its served/live URL and the "
            "PASS verdict), and any you could not get to PASS after 3 rounds, with the exact blocking error "
            "or failing-gate reason. Be honest; report only fixes that actually deployed clean AND passed "
            "their gate."
        ),
        expected_output=(
            "A per-artifact result: each fixed artifact -> redeployed -> gate PASS (with URL + which gate), "
            "or -> still blocked with the exact error / failing-gate reason. Report only fixes that deployed "
            "clean AND passed their verify gate."
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

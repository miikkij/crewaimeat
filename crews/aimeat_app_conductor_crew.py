"""aimeat-app-conductor — the delivery lead of the AIMEAT SDLC crew family (direct-build).

It does NOT build apps itself. It ORCHESTRATES: it ROUTES the idea to the right specialist (by default
aimeat-app-builder, which AUTHORS a cortex + app directly and installs/publishes them; or — only when the
idea needs a genuinely new KIND of building no existing crew can do — it ORDERS aimeat-crew-forge to
forge a new specialist for that domain, then delegates to it). It then runs the DETERMINISTIC authed
gate (verify_render — logs in as the owner and confirms real content renders with no console errors),
and if it fails routes the exact fix to aimeat-cortex-fixer and re-verifies (a bounded loop). Only an
app that PASSES the authed gate is completed — so a broken app never ships "green". After the gate
resolves it RATES the crew it delegated to (coordinator-rates-worker, AIMEAT /tasks/:id/rate), grounded
in the deterministic verify outcome (PASS first-try=5 … never passed=1) — so the fleet accumulates real
field reputation for who delivers working apps.

Why a conductor (vs the builder alone): the builder self-verifies, but the conductor is an INDEPENDENT
second gate + the fix-loop owner. The gate is deterministic CODE (a real headless login + render), not
LLM judgement; the conductor enforces it and routes fixes to the repair specialist.

Prerequisites (human-gated, one time):
  - Onboard:  npx aimeat@latest connect add --agent aimeat-app-conductor --mode task-runner --url <node> --owner <you>
  - Assign the shared tag "workflow" so it can delegate to aimeat-app-builder + aimeat-cortex-fixer +
    aimeat-crew-forge (the forge must also carry the "workflow" tag to be reachable for new-domain work).
  - The owner's app-login creds must be in .env (AIMEAT_APP_LOGIN_USER / AIMEAT_APP_LOGIN_PASSWORD) so
    verify_render can log in; the cortex-install grant must be deployed for the builder/fixer.
Run:  uv run python crews/aimeat_app_conductor_crew.py
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.author_tool import make_author_tools
from crewaimeat.scheduler import make_schedule_tools
from crewaimeat.workflow import make_workflow_tools

AGENT_NAME = "aimeat-app-conductor"

README = """[[FIGLET:slant]["aimeat app conductor"]]

I am the delivery lead for building AIMEAT apps. I don't write the app — I orchestrate it:
I ROUTE the idea to the right specialist (by default **aimeat-app-builder**, which authors a cortex +
app directly; or, when the idea needs a genuinely new KIND of building, I ORDER **aimeat-crew-forge** to
forge a new specialist for that domain and delegate to it). Then I run the **deterministic authed gate**
(`verify_render`: a real headless login as the owner + a check that the app renders real content with no
console errors). If it fails I route the exact fix to **aimeat-cortex-fixer** and re-verify. I only mark
a project done when the gate is green.

Give me a task whose description is the app idea, e.g.:
  "A dashboard listing my fleet's agents and each agent's latest task output, with a topic filter."
"""


def build_domain(ctx: BuildContext) -> tuple[list[Agent], list[Task]]:
    tid = (ctx.task or {}).get("id") or "manual"
    # The conductor verifies (it does not build): take only the authed render gate + the URL helper.
    author_tools, _state = make_author_tools(AGENT_NAME, task_id=tid)
    verify_tools = [t for t in author_tools if getattr(t, "name", "") in ("verify_render", "verify_anon_render", "verify_interaction", "app_inline_url")]
    # timeout=7200 (2h): the wait must exceed the worst-case specs-designer interview (its ask_owner can
    # block on an absent owner), so a slow human answering the interview never orphans the Phase-0 wait.
    wf_tools = make_workflow_tools(
        coordinator_name=AGENT_NAME, run_id=tid, task_id=tid, tag="workflow", timeout=7200,
    )
    # cancel_pending lets the conductor stop an orphaned delegatee on timeout (a builder/fixer that exceeds
    # the wait keeps running and could republish after the conductor moved on — cancel it before proceeding).
    deleg = [t for t in wf_tools if getattr(t, "name", "") in ("discover_crews", "delegate_and_wait", "rate_delegated_work", "cancel_pending")]
    # Scheduler: set up AIMEAT server-run schedules (the node owns the cron clock; fires offline; owner
    # controls them in Profile -> Scheduler). For recurring/automated deliverables (daily pipelines, etc.).
    sched_tools = make_schedule_tools(AGENT_NAME)

    conductor = Agent(
        role="AIMEAT App Delivery Lead",
        goal=(
            "Deliver a WORKING AIMEAT app from the user's idea by orchestrating specialists and "
            "enforcing the deterministic authed render gate — completing an app only once it passes. When "
            "the idea is RECURRING/automated, set it up on the AIMEAT scheduler so the node runs it on a cron."
        ),
        backstory=(
            "You are a delivery lead, not a coder. You FIRST gather requirements by delegating to "
            "aimeat-app-specs-designer, which interviews the owner and returns a precise technical spec, so "
            "the build matches what the owner actually wants (the owner never has to relay anything). THEN "
            "you ROUTE the build to the right specialist: by "
            "default aimeat-app-builder (it builds the AIMEAT-native way — ONE cortex + ONE app, plus a "
            "server-side extension when needed — and covers most apps). If, and ONLY if, the idea needs a "
            "genuinely NEW kind of building that no existing crew can do, you ORDER aimeat-crew-forge to "
            "forge a new specialist for that domain, then delegate to it (a freshly-forged agent needs the "
            "owner's one-time approval before it can run). Then you INDEPENDENTLY verify the result with "
            "verify_render (a real logged-in browser render — you rely on the gate over 'looks done'). When it fails "
            "you route the precise fix to aimeat-cortex-fixer and re-verify. You stop only when the gate is "
            "green or you have exhausted a bounded number of fix rounds — and then you report the truth, "
            "honestly, reporting only a pass you actually got. You route to aimeat-app-builder for "
            "everything it can do, and forge only for a genuinely new domain no existing crew covers. "
            "Finally, once the gate resolves you RATE the crew you delegated to, grounded in the objective "
            "verify outcome (PASS first-try=5 … never passed=1) — never opinion — so the fleet learns who "
            "actually delivers working apps. When a request is RECURRING or automated (a daily/periodic "
            "pipeline, a scheduled refresh), you set it up on the AIMEAT scheduler with schedule_create so "
            "the NODE runs it on a cron clock (fires even when agents are offline; the owner controls it in "
            "Profile -> Scheduler) — pick the lightest kind that fits: 'extension' (0 tokens) or 'ai' "
            "(server-side, owner's OpenRouter key) over 'agent_task' when no agent reasoning is needed; "
            "stage multi-step pipelines by cron times and connect them through named memory keys."
        ),
        tools=[*verify_tools, *deleg, *sched_tools],
        llm=ctx.llm,
        max_iter=40,
        allow_delegation=False,
        verbose=True,
    )

    spec_task = Task(
        description=(
            f"{ctx.today}\n\n"
            "PHASE 0 — GATHER REQUIREMENTS (so the app is built right the first time). The user's request:\n\n"
            f"<<APP IDEA>>\n{ctx.prompt}\n<</APP IDEA>>\n\n"
            "Delegate to the requirements specialist, which interviews the owner and returns a precise, "
            "AIMEAT-correct technical spec:\n"
            "  delegate_and_wait(\"aimeat-app-specs-designer\", \"Spec: <short idea>\", \"<the full app idea "
            "above as ONE self-contained string>\")  — exactly three positional string args.\n"
            "It asks the owner a few short questions (the owner answers them in the dashboard Messages tab), "
            "then returns the spec: the DATA LAYER, who reads/writes, the auth model, the data model "
            "(keys/namespaces/visibility), image handling, a build checklist, and a verify plan.\n"
            "CAPTURE the returned spec VERBATIM — Phase 1 hands it to the builder so the app matches the "
            "agreed design. If aimeat-app-specs-designer is unreachable (not approved, missing the 'workflow' "
            "tag, or the fleet is down), STOP and report exactly that — the build needs the spec first.\n"
            "TIMEOUT HANDLING: if delegate_and_wait returns a '[no result ... within the timeout]' string "
            "(the spec interview is still pending — usually the owner has not answered in the dashboard "
            "Messages tab), call cancel_pending() to stop the still-running specs-designer, then STOP and "
            "report that the spec is pending the owner's answers. Do NOT proceed to Phase 1 without a spec — "
            "building without the agreed design is exactly what this phase exists to prevent."
        ),
        expected_output=(
            "The complete technical spec returned by aimeat-app-specs-designer (data layer + rationale, data "
            "model with keys/namespaces/visibility, auth model, image handling, build checklist, verify plan)."
        ),
        agent=conductor,
    )

    plan = Task(
        description=(
            f"{ctx.today}\n\n"
            "PHASE 1 — ROUTE, then DELEGATE THE BUILD to the SPEC from Phase 0.\n\n"
            f"<<APP IDEA>>\n{ctx.prompt}\n<</APP IDEA>>\n\n"
            "1. discover_crews — see which AIMEAT-SDLC specialists exist (e.g. aimeat-app-builder for "
            "cortex+app and extension-backed apps; aimeat-extension-builder; plus any previously-forged "
            "specialists).\n"
            "2. ROUTE to the best specialist for THIS idea:\n"
            "   - DEFAULT = aimeat-app-builder. It builds the AIMEAT-native way (ONE cortex + ONE app HTML, "
            "plus a server-side extension when the app needs external HTTP / cron / server-validated work). "
            "It covers the large majority of apps — own-data dashboards, tools, games, AND extension-backed "
            "apps. Prefer it.\n"
            "   - Use a different EXISTING specialist only if discover_crews shows one that clearly fits "
            "better.\n"
            "   - ONLY IF the idea needs a genuinely NEW kind of building that NO existing crew can do (a "
            "new DOMAIN/capability, not merely a new app), ORDER one from the forge: "
            "delegate_and_wait(\"aimeat-crew-forge\", \"Forge <kind> specialist\", \"<ONE string describing "
            "the specialist to create: its domain, the kind of stack it authors, and that it must use "
            "make_author_tools + start apps from read_app_template() and end in a verify_render gate>\"). "
            "The forge designs, writes, validates, registers, and launches the new crew and reports its "
            "agent name + the ONE owner-approval step. A freshly-forged agent must be APPROVED by the owner "
            "(device flow) before it can run tasks — so after forging, TRY delegate_and_wait to the new "
            "agent; if it is not yet reachable/approved, STOP and report: the new specialist was forged, the "
            "exact approve step, and that the build will run once approved. Do NOT forge when "
            "aimeat-app-builder can do it — forging is for new domains only.\n"
            "3. delegate_and_wait(\"<chosen specialist>\", \"<short title>\", \"<instruction>\") — the "
            "instruction is ONE self-contained string: the FULL TECHNICAL SPEC from Phase 0 (verbatim — the "
            "data layer, data model, auth, image handling, build checklist), plus: 'Build to THIS SPEC, the direct "
            "AIMEAT-native way — ONE cortex + ONE app HTML (start the app from read_app_template()), no "
            "generator. Report the app FILENAME (e.g. something.html), the cortex name, the live inline URL, "
            "and the agent/topic names you seeded. If the app is INTERACTIVE (click/type to make something "
            "happen), ALSO report a verify_interaction steps_json — a JSON array of fill/click/wait_enabled/"
            "expect_text steps with the app REAL selectors that proves the core feature works.' Call it with "
            "exactly three positional string arguments.\n"
            "   PUBLIC / ANON-READABLE apps (the idea says readable WITHOUT logging in — a public "
            "newspaper, directory, noticeboard, gallery, or a viewer over a content pipeline's public "
            "memory): the build instruction MUST additionally say: 'This must render for ANYONE with NO "
            "account. Start from read_app_template(\"public_viewer\") — startApp() runs UNCONDITIONALLY "
            "(call startApp() unconditionally so anonymous visitors render), read shown content with "
            "getPublic(gaii,key) ONLY. The "
            "content lives behind ONE public index key: call find_public_index(\"<the index key, e.g. "
            "newspaper.frontpage>\") to get the PUBLISHER gaii, set `const PUBLISHER` to it and "
            "`const INDEX_KEY` to that key, read the index, then fan out getPublic(item.gaii, item.key) per "
            "item. Confirm it with verify_anon_render (no-login), not just verify_render.' Also report a "
            "couple of strings that should appear in the PUBLIC view (e.g. a category name or article "
            "title) so Phase 2 can assert the anonymous render.\n"
            "3c. ORPHAN GUARD: if delegate_and_wait returns a '[no result ... within the timeout]' string, "
            "the build crew is still running — call cancel_pending() to stop it BEFORE you retry or proceed, "
            "so it cannot publish/republish the app after you have moved on; then report that the build did "
            "not return in time (do not silently proceed as if it succeeded).\n"
            "4. From the chosen specialist's report, EXTRACT verbatim: (a) the app FILENAME (e.g. "
            "'fleet-activity-dashboard.html'), (b) a few of the seeded AGENT NAMES or — for a public app — "
            "strings that appear in the PUBLIC view (you pass these to the verify gate as proof real data "
            "renders), (c) IF interactive, the verify_interaction steps_json, and (d) whether the app is "
            "PUBLIC/anon-readable (you will run verify_anon_render in Phase 2 if so). You need these in Phase 2."
        ),
        expected_output=(
            "The routing decision (which specialist, and — if forged — the new agent name + the owner "
            "approve step), then the chosen specialist's build report with the app filename, the cortex "
            "name, the live URL, and the seeded agent names clearly stated."
        ),
        agent=conductor,
        context=[spec_task],
    )

    verify = Task(
        description=(
            "PHASE 2 — VERIFY (deterministic authed gate) + FIX-LOOP + COMPLETE.\n"
            "1. verify_render(filename, expect_csv) — use the app FILENAME from Phase 1, and set "
            "expect_csv to a few seeded agent names (comma-separated, e.g. 'web-researcher,data-analyst') "
            "so the gate proves the real data renders, not just a login screen. It logs in as the owner "
            "and returns 'VERIFY PASS ...' or 'VERIFY FAIL ...' (with login status, console errors, and a "
            "content sample).\n"
            "2. If VERIFY PASS: you may NOT be done yet — verify_render logs in as the OWNER and checks only "
            "the initial render, so it FALSE-PASSes two classes of app:\n"
            "   (a) INTERACTIVE apps — and ESPECIALLY any app whose SPEC says users CREATE / POST / EDIT "
            "something (e.g. a marketplace where registered users list items): run verify_interaction(filename, "
            "steps_json) that exercises the SPEC'd WRITE path end-to-end — log in if required, CREATE/POST an "
            "item with the app's real selectors, then expect_text that new item appearing — not just reading. "
            "Use the steps_json the builder reported in Phase 1; if it only tests viewing, ADD steps that "
            "perform the write the spec requires. A render-only or view-only pass on an app whose spec requires "
            "posting/creating is NOT green. INTERACTION FAIL (or no write path present) → treat like VERIFY "
            "FAIL, go to step 3.\n"
            "   (b) PUBLIC / ANON-READABLE apps (the idea required reading WITHOUT logging in): verify_render "
            "logged in as the owner, so it CANNOT catch a viewer that renders only for a session (the "
            "`if (session) startApp()` bug leaves anonymous visitors stuck on 'Loading…'). Run "
            "verify_anon_render(filename, expect_csv) with expect_csv = the PUBLIC-view strings from Phase 1 "
            "(a category name / article title) — it loads with NO login. ANON VERIFY FAIL → treat like VERIFY "
            "FAIL, go to step 3.\n"
            "   The app is GREEN only when verify_render PASSES AND (if interactive) verify_interaction PASSES "
            "AND (if public/anon-readable) verify_anon_render PASSES. A non-interactive owner-only app is green "
            "on verify_render alone. When green, report success + the live app URL.\n"
            "3. If VERIFY FAIL: route the fix. (ORPHAN GUARD: if any delegate_and_wait — build or fix — "
            "returns a '[no result ... within the timeout]' string, call cancel_pending() to stop the "
            "still-running delegatee before you retry or rate, so it cannot republish the app after you "
            "have moved on.) delegate_and_wait(\"aimeat-cortex-fixer\", \"<title>\", "
            "\"<instruction>\") where the instruction is ONE self-contained string containing: the app "
            "FILENAME, the app INLINE URL, the cortex name, the FULL TECHNICAL SPEC from Phase 0 (so the "
            "fixer knows the COMPLETE feature set and PRESERVES it — fixing only the failure without dropping "
            "any spec'd feature like a working create/post form), and the EXACT FAIL text (verify_render "
            "login status + console errors + content sample, OR the ANON VERIFY FAIL / INTERACTION FAIL text "
            "— pass whichever gate failed). Then re-run the gate(s) that applied — verify_render, and verify_anon_render for a "
            "public app, and verify_interaction for an interactive app — AGAIN. Repeat this "
            "fix→verify loop AT MOST 3 times.\n"
            "4. If still FAIL after 3 rounds: STOP — do not loop further. Report the remaining failure "
            "honestly as a blocker (the exact verify_render reason) and name the likely fix site.\n"
            "5. RATE the delegatee (verify-grounded reputation). Once the gates have resolved (green or "
            "exhausted), call rate_delegated_work(target_agent=<the crew you delegated the BUILD to in "
            "Phase 1>, verify_passed=<true ONLY if the app is GREEN: the FINAL verify_render PASSED AND, for "
            "an interactive app, verify_interaction PASSED, AND for a public/anon-readable app, "
            "verify_anon_render PASSED — a render-only or owner-only pass on a broken interactive/public app "
            "is NOT green>, fix_rounds=<how many fix->reverify cycles you ran; 0 if it passed first-try>). "
            "This records a "
            "Quality-tab score grounded in the real render outcome (PASS first-try=5 … never passed=1) and "
            "feeds the builder's reputation. Do this whether the outcome was PASS or FAIL — honest signal "
            "either way. (If you also routed a fix to aimeat-cortex-fixer and it made the app pass, you MAY "
            "additionally rate it: rate_delegated_work('aimeat-cortex-fixer', verify_passed=true, fix_rounds=0).)\n"
            "6. Final deliverable: the live app URL, the final verify_render verdict (PASS or the "
            "remaining failure), the rating you recorded, and a short orchestration summary (build + any "
            "fix rounds)."
        ),
        expected_output=(
            "Final report: live app URL, final verify_render verdict (VERIFY PASS or the remaining "
            "failure), the verify-grounded rating recorded for the delegatee, and a short orchestration "
            "summary (build + any fix rounds)."
        ),
        agent=conductor,
        context=[spec_task, plan],
    )

    return [conductor], [spec_task, plan, verify]


def run() -> None:
    run_crew(
        CrewSpec(
            agent_name=AGENT_NAME,
            build_domain=build_domain,
            readme_md=README,
            temperature=0.3,  # orchestration wants consistency, not creativity
            poll_seconds=30,
        )
    )


if __name__ == "__main__":
    run()

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
    verify_tools = [t for t in author_tools if getattr(t, "name", "") in ("verify_render", "verify_interaction", "app_inline_url")]
    wf_tools = make_workflow_tools(
        coordinator_name=AGENT_NAME, run_id=tid, task_id=tid, tag="workflow", timeout=2400,
    )
    deleg = [t for t in wf_tools if getattr(t, "name", "") in ("discover_crews", "delegate_and_wait", "rate_delegated_work")]
    # Scheduler: set up AIMEAT server-run schedules (the node owns the cron clock; fires offline; owner
    # controls them in Profile -> Scheduler). For recurring/automated deliverables (daily pipelines, etc.).
    sched_tools = make_schedule_tools(AGENT_NAME)

    conductor = Agent(
        role="AIMEAT App Delivery Lead",
        goal=(
            "Deliver a WORKING AIMEAT app from the user's idea by orchestrating specialists and "
            "enforcing the deterministic authed render gate — never completing an app that fails it. When "
            "the idea is RECURRING/automated, set it up on the AIMEAT scheduler so the node runs it on a cron."
        ),
        backstory=(
            "You are a delivery lead, not a coder. You first ROUTE the idea to the right specialist: by "
            "default aimeat-app-builder (it builds the AIMEAT-native way — ONE cortex + ONE app, plus a "
            "server-side extension when needed — and covers most apps). If, and ONLY if, the idea needs a "
            "genuinely NEW kind of building that no existing crew can do, you ORDER aimeat-crew-forge to "
            "forge a new specialist for that domain, then delegate to it (a freshly-forged agent needs the "
            "owner's one-time approval before it can run). Then you INDEPENDENTLY verify the result with "
            "verify_render (a real logged-in browser render — you never trust 'looks done'). When it fails "
            "you route the precise fix to aimeat-cortex-fixer and re-verify. You stop only when the gate is "
            "green or you have exhausted a bounded number of fix rounds — and then you report the truth, "
            "never a pass you did not get. You do NOT forge when aimeat-app-builder can do the job. "
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

    plan = Task(
        description=(
            f"{ctx.today}\n\n"
            "PHASE 1 — ROUTE, then DELEGATE THE BUILD. The app to build:\n\n"
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
            "instruction is ONE self-contained string: the full app idea above, plus: 'Build it the direct "
            "AIMEAT-native way — ONE cortex + ONE app HTML (start the app from read_app_template()), no "
            "generator. Report the app FILENAME (e.g. something.html), the cortex name, the live inline URL, "
            "and the agent/topic names you seeded. If the app is INTERACTIVE (click/type to make something "
            "happen), ALSO report a verify_interaction steps_json — a JSON array of fill/click/wait_enabled/"
            "expect_text steps with the app REAL selectors that proves the core feature works.' Call it with "
            "exactly three positional string arguments.\n"
            "4. From the chosen specialist's report, EXTRACT verbatim: (a) the app FILENAME (e.g. "
            "'fleet-activity-dashboard.html'), (b) a few of the seeded AGENT NAMES (you will pass these to "
            "verify_render as proof the data renders), and (c) IF interactive, the verify_interaction "
            "steps_json. You need these in Phase 2."
        ),
        expected_output=(
            "The routing decision (which specialist, and — if forged — the new agent name + the owner "
            "approve step), then the chosen specialist's build report with the app filename, the cortex "
            "name, the live URL, and the seeded agent names clearly stated."
        ),
        agent=conductor,
    )

    verify = Task(
        description=(
            "PHASE 2 — VERIFY (deterministic authed gate) + FIX-LOOP + COMPLETE.\n"
            "1. verify_render(filename, expect_csv) — use the app FILENAME from Phase 1, and set "
            "expect_csv to a few seeded agent names (comma-separated, e.g. 'web-researcher,data-analyst') "
            "so the gate proves the real data renders, not just a login screen. It logs in as the owner "
            "and returns 'VERIFY PASS ...' or 'VERIFY FAIL ...' (with login status, console errors, and a "
            "content sample).\n"
            "2. If VERIFY PASS: for an INTERACTIVE app you are NOT done yet — verify_render only checks the "
            "initial render and will FALSE-PASS a broken feature (this is exactly how a realtime chat that "
            "couldn't send a message once shipped green). Run verify_interaction(filename, steps_json) using "
            "the steps_json the builder reported in Phase 1 (if the app is clearly interactive but the builder "
            "reported no steps, treat that as a gap: derive steps that exercise the core feature with the app's "
            "real selectors). The app is GREEN only when verify_render PASSES AND (for interactive apps) "
            "verify_interaction PASSES. If verify_interaction returns INTERACTION FAIL, treat it like a VERIFY "
            "FAIL and go to step 3. For a non-interactive app, verify_render PASS alone is green. When green, "
            "report success + the live app URL.\n"
            "3. If VERIFY FAIL: route the fix. delegate_and_wait(\"aimeat-cortex-fixer\", \"<title>\", "
            "\"<instruction>\") where the instruction is ONE self-contained string containing: the app "
            "FILENAME, the cortex name, and the EXACT verify_render FAIL text (login status + console "
            "errors + content sample). Then call verify_render(filename, expect_csv) AGAIN. Repeat this "
            "fix→verify loop AT MOST 3 times.\n"
            "4. If still FAIL after 3 rounds: STOP — do not loop further. Report the remaining failure "
            "honestly as a blocker (the exact verify_render reason) and name the likely fix site.\n"
            "5. RATE the delegatee (verify-grounded reputation). Once the gates have resolved (green or "
            "exhausted), call rate_delegated_work(target_agent=<the crew you delegated the BUILD to in "
            "Phase 1>, verify_passed=<true ONLY if the app is GREEN: the FINAL verify_render PASSED AND, for "
            "an interactive app, verify_interaction PASSED — a render-only pass on a broken interactive app "
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
        context=[plan],
    )

    return [conductor], [plan, verify]


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

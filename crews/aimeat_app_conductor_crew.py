"""aimeat-app-conductor — the delivery lead of the AIMEAT SDLC crew family (direct-build).

It does NOT build apps itself. It ORCHESTRATES: delegates the build to aimeat-app-builder (which
AUTHORS a cortex + app directly and installs/publishes them), then runs the DETERMINISTIC authed
gate (verify_render — logs in as the owner and confirms real content renders with no console errors),
and if it fails routes the exact fix to aimeat-cortex-fixer and re-verifies (a bounded loop). Only an
app that PASSES the authed gate is completed — so a broken app never ships "green".

Why a conductor (vs the builder alone): the builder self-verifies, but the conductor is an INDEPENDENT
second gate + the fix-loop owner. The gate is deterministic CODE (a real headless login + render), not
LLM judgement; the conductor enforces it and routes fixes to the repair specialist.

Prerequisites (human-gated, one time):
  - Onboard:  npx aimeat@latest connect add --agent aimeat-app-conductor --mode task-runner --url <node> --owner <you>
  - Assign the shared tag "workflow" so it can delegate to aimeat-app-builder + aimeat-cortex-fixer.
  - The owner's app-login creds must be in .env (AIMEAT_APP_LOGIN_USER / AIMEAT_APP_LOGIN_PASSWORD) so
    verify_render can log in; the cortex-install grant must be deployed for the builder/fixer.
Run:  uv run python crews/aimeat_app_conductor_crew.py
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.author_tool import make_author_tools
from crewaimeat.workflow import make_workflow_tools

AGENT_NAME = "aimeat-app-conductor"

README = """[[FIGLET:slant]["aimeat app conductor"]]

I am the delivery lead for building AIMEAT apps. I don't write the app — I orchestrate it:
delegate the build to **aimeat-app-builder** (which authors a cortex + app directly), then run the
**deterministic authed gate** (`verify_render`: a real headless login as the owner + a check that the
dashboard renders real content with no console errors). If it fails I route the exact fix to
**aimeat-cortex-fixer** and re-verify. I only mark a project done when the gate is green.

Give me a task whose description is the app idea, e.g.:
  "A dashboard listing my fleet's agents and each agent's latest task output, with a topic filter."
"""


def build_domain(ctx: BuildContext) -> tuple[list[Agent], list[Task]]:
    tid = (ctx.task or {}).get("id") or "manual"
    # The conductor verifies (it does not build): take only the authed render gate + the URL helper.
    author_tools, _state = make_author_tools(AGENT_NAME, task_id=tid)
    verify_tools = [t for t in author_tools if getattr(t, "name", "") in ("verify_render", "app_inline_url")]
    wf_tools = make_workflow_tools(
        coordinator_name=AGENT_NAME, run_id=tid, task_id=tid, tag="workflow", timeout=2400,
    )
    deleg = [t for t in wf_tools if getattr(t, "name", "") in ("discover_crews", "delegate_and_wait")]

    conductor = Agent(
        role="AIMEAT App Delivery Lead",
        goal=(
            "Deliver a WORKING AIMEAT app from the user's idea by orchestrating specialists and "
            "enforcing the deterministic authed render gate — never completing an app that fails it."
        ),
        backstory=(
            "You are a delivery lead, not a coder. You delegate the build to aimeat-app-builder, then "
            "you INDEPENDENTLY verify the result with verify_render (a real logged-in browser render — "
            "you never trust 'looks done'). When it fails you route the precise fix to "
            "aimeat-cortex-fixer and re-verify. You stop only when the gate is green or you have "
            "exhausted a bounded number of fix rounds — and then you report the truth, never a pass you "
            "did not get."
        ),
        tools=[*verify_tools, *deleg],
        llm=ctx.llm,
        max_iter=40,
        allow_delegation=False,
        verbose=True,
    )

    plan = Task(
        description=(
            f"{ctx.today}\n\n"
            "PHASE 1 — DELEGATE THE BUILD. The app to build:\n\n"
            f"<<APP IDEA>>\n{ctx.prompt}\n<</APP IDEA>>\n\n"
            "1. (Optional) discover_crews to confirm aimeat-app-builder is available.\n"
            "2. delegate_and_wait(\"aimeat-app-builder\", \"<short title>\", \"<instruction>\") — the "
            "instruction is ONE self-contained string: the full app idea above, plus: 'Build it the "
            "direct AIMEAT-native way — ONE cortex + ONE app HTML, no generator. Report the app FILENAME "
            "(e.g. something.html), the cortex name, the live inline URL, and the agent/topic names you "
            "seeded.' Call it with exactly three positional string arguments.\n"
            "3. From the builder's report, EXTRACT verbatim: (a) the app FILENAME (e.g. "
            "'fleet-activity-dashboard.html'), and (b) a few of the seeded AGENT NAMES (you will pass "
            "these to verify_render as proof the data renders). You need both in Phase 2."
        ),
        expected_output=(
            "The builder's report, with the app filename, the cortex name, the live URL, and the seeded "
            "agent names clearly stated."
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
            "2. If VERIFY PASS: report success — the live app URL + 'authed render green'. Done.\n"
            "3. If VERIFY FAIL: route the fix. delegate_and_wait(\"aimeat-cortex-fixer\", \"<title>\", "
            "\"<instruction>\") where the instruction is ONE self-contained string containing: the app "
            "FILENAME, the cortex name, and the EXACT verify_render FAIL text (login status + console "
            "errors + content sample). Then call verify_render(filename, expect_csv) AGAIN. Repeat this "
            "fix→verify loop AT MOST 3 times.\n"
            "4. If still FAIL after 3 rounds: STOP — do not loop further. Report the remaining failure "
            "honestly as a blocker (the exact verify_render reason) and name the likely fix site.\n"
            "5. Final deliverable: the live app URL, the final verify_render verdict (PASS or the "
            "remaining failure), and a short orchestration summary (build + any fix rounds)."
        ),
        expected_output=(
            "Final report: live app URL, final verify_render verdict (VERIFY PASS or the remaining "
            "failure), and a short orchestration summary (build + any fix rounds)."
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

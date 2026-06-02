"""aimeat-app-builder — the first crew of the AIMEAT SDLC family.

It builds a complete, working AIMEAT app from a one-line idea by DRIVING THE GENERATOR PIPELINE
end to end over REST (Tie 1): create project -> interview -> blueprint -> per-component loop
(generate -> submit -> register/activate) -> final browser test (delegated to the web-tester crew,
NOT the generator's Playwright) -> complete.

This is an AIMEAT-specific crew (prefix "aimeat-"), deliberately kept SEPARATE from the
general-purpose fleet. The agent plays both the interviewer (idea -> spec) and the pipeline driver
(spec -> blueprint -> code -> registered, activated app). The generator's calibrated prompts +
server-side validation do the heavy lifting; the generator_tool tools are the deterministic REST
plumbing; the agent supplies the content.

Prerequisites (human-gated, one time):
  - `npx aimeat@latest connect add --agent aimeat-app-builder --mode task-runner \
        --url https://aimeat.io --owner <you>`  then approve it in the dashboard.
  - Assign the shared tag "workflow" to aimeat-app-builder in the dashboard (Data Access ->
    Shared tags) so it can delegate the browser test to web-tester and read the result back.
  - web-tester must already be registered + approved (it is, in the live fleet).
Run:  uv run python crews/aimeat_app_builder_crew.py   (or under scripts/watchdog.ps1)
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.generator_tool import make_generator_tools
from crewaimeat.workflow import make_workflow_tools

AGENT_NAME = "aimeat-app-builder"

README = """[[FIGLET:slant]["aimeat app builder"]]

I turn a one-line app idea into a **live, browser-tested AIMEAT app** by driving the generator
pipeline: interview -> blueprint -> components (CSM, memory, translations, cortexes, app) ->
register/activate -> a real browser test (via the web-tester crew) -> complete.

Give me a task whose description is the app you want, e.g.:
  "A dashboard that lists my fleet's agents and their latest task output, with a topic filter."

I report what I built (project, components, the app URL) and the browser-test evidence.
"""


def build_domain(ctx: BuildContext) -> tuple[list[Agent], list[Task]]:
    """One builder agent, three sequential phases (Plan -> Build -> Verify). The generator tools
    share a state dict that carries the projectId across all three within one kickoff."""
    tid = (ctx.task or {}).get("id") or "manual"
    gen_tools, _gen_state = make_generator_tools(AGENT_NAME, task_id=tid)

    # Reuse the proven delegation tools to hand the final browser test to web-tester.
    wf_tools = make_workflow_tools(
        coordinator_name=AGENT_NAME, run_id=tid, task_id=tid, tag="workflow", timeout=1800,
    )
    web_tools = [t for t in wf_tools if getattr(t, "name", "") in ("discover_crews", "delegate_and_wait")]

    builder = Agent(
        role="AIMEAT App Builder",
        goal=(
            "Build a complete, working AIMEAT application from the user's one-line idea by driving "
            "the generator pipeline end to end, then prove it works by driving a real browser."
        ),
        backstory=(
            "You are an expert AIMEAT app engineer who knows the generator pipeline cold. You play "
            "BOTH the requirements analyst (idea -> structured spec) and the pipeline driver (spec "
            "-> blueprint -> component code -> registered, activated app). You produce every "
            "artifact in its EXACT required format and you NEVER invent external API response "
            "shapes. You verify, you don't assume: the app is not done until a real browser test "
            "passes. You use the generator's calibrated prompts (gen_* tools) and you delegate the "
            "final browser test to the web-tester crew."
        ),
        tools=[*gen_tools, *web_tools],
        llm=ctx.llm,
        max_iter=60,
        allow_delegation=False,
        verbose=True,
    )

    plan = Task(
        description=(
            f"{ctx.today}\n\n"
            "PHASE 1 — PLAN. Build the AIMEAT app described here:\n\n"
            f"<<APP IDEA>>\n{ctx.prompt}\n<</APP IDEA>>\n\n"
            "Steps (use the gen_* tools; each returns success or the exact errors to fix):\n"
            "1. gen_create_project(name, description) — derive a short name + one-paragraph "
            "description from the idea.\n"
            "2. gen_get_interview_prompt — then RUN it yourself: you are both the analyst and the "
            "interviewee. Produce the JSON spec it asks for. HARD RULES: never invent an external "
            "API response shape — for every external URL you actually fetch it and paste a real "
            "sampleEntry + responseEnvelope, OR mark it verified:false with a fallback "
            "('demo'|'defer'|'skip'); define >=2 use cases; set a locale; views must reference real "
            "data entities. PREFER a design with NO extension (read/write the user's own data via "
            "AIMEAT.data) unless the app genuinely needs server-only work (external API behind "
            "auth/CORS, or a scheduled cron job).\n"
            "3. gen_import_spec(spec_json) — fix and resubmit until it saves AND passes the quality "
            "gate (verified URL + sampleEntry per source, >=2 use cases, a locale).\n"
            "4. gen_get_blueprint_prompt — RUN it: produce the JSON blueprint. Build dataModel."
            "structures from the REAL sample data with strict $ref discipline (same data => same "
            "$ref everywhere); decompose the cortex into a data cortex + at least one component "
            "cortex + exactly one app-domain cortex (last); one translation component PER locale "
            "(fi AND en, identical keys); include service_slug.\n"
            "5. gen_import_blueprint(blueprint_json) — note it must be a JSON STRING. If it is "
            "rejected, read the errors, fix the blueprint, and retry AT MOST 3 times. If it is still "
            "invalid after 3 attempts, STOP — do not resubmit the same thing again; report the exact "
            "validation error as a blocker in your output and fail gracefully (do not loop).\n"
            "6. gen_save_settings(values_json) — store any settings the spec surfaced, or '{}'.\n"
        ),
        expected_output=(
            "A short report: the projectId, the spec's use cases, and confirmation the blueprint "
            "imported valid=true (with the component list it seeded)."
        ),
        agent=builder,
    )

    build = Task(
        description=(
            "PHASE 2 — BUILD. Implement every component, in phase order.\n"
            "1. gen_list_components — get the components in build order (define -> seed -> "
            "[extension] -> data cortex -> component cortexes -> app-domain cortex -> app).\n"
            "2. For EACH component, in that order, run the right sub-flow for its type:\n"
            "   a. SPEC-FIRST (extension, cortex, app ONLY — skip for csm/memory/translation): "
            "gen_component_prompt(component_id, 'spec') -> RUN it -> produce the spec JSON -> "
            "gen_submit_spec(component_id, spec_json). The server now VALIDATES the spec before "
            "storing it (same checks the UI runs): if it returns validation errors, read them, fix "
            "the spec, and resubmit AT MOST 3 times. Only a stored spec unlocks the code step. The "
            "stored spec is fed into the code prompt (selfSpec/extensionSpec/dataApiSpec), so the code "
            "matches the contract. This step is CRITICAL — without it the code is generated blind.\n"
            "   b. gen_component_prompt(component_id, 'code') — fetch this ONLY AFTER gen_submit_spec "
            "succeeded for this component: the code prompt is rebuilt server-side to embed the spec you "
            "just stored, so a prompt fetched before the spec was stored would miss it. RUN it and "
            "produce the artifact in its EXACT format:\n"
            "      - csm/msm -> a YAML manifest (every string value on ONE line, double-quoted; no "
            "block scalars).\n"
            "      - memory -> a JSON object (one key per dataset; prefer fewer, larger keys).\n"
            "      - translation -> a JSON object for ONE locale; fi and en MUST share identical "
            "keys.\n"
            "      - cortex -> a fenced ```yaml manifest + a fenced ```javascript IIFE that "
            "registers on AIMEAT.<libName>. Each layer talks only to the one below; the data cortex "
            "is the only client that touches the extension; translations/settings are read with "
            "AIMEAT.data.get (owner namespace), never from ext:. session.fetch returns parsed JSON "
            "(use resp.data, no .json()).\n"
            "      - app -> a single self-contained HTML document that loads its cortex deps in "
            "order and calls ONLY cortex public methods (never callExt / raw /v1/ext / raw memory).\n"
            "   c. gen_submit_component(component_id, type, content) — if it returns validation "
            "errors, read them, fix the artifact, and resubmit AT MOST 3 times for this component. "
            "After 3 failed attempts, STOP retrying it — report the exact error as a blocker and move "
            "on; never resubmit the same content in a loop.\n"
            "   d. gen_register_component(component_id) — cortex is auto-activated by register. If "
            "you built an extension, also call gen_activate_extension(name).\n"
            "Do NOT run the generator's per-component browser tests (its Playwright path is "
            "unreliable here) — the real browser test happens in Phase 3 via the web-tester crew.\n"
            "Keep going until every component shows status=registered."
        ),
        expected_output=(
            "A per-component status list (each component_id -> registered), plus the published app's "
            "filename."
        ),
        agent=builder,
        context=[plan],
    )

    verify = Task(
        description=(
            "PHASE 3 — VERIFY + COMPLETE. Prove the app works in a real browser, then finish.\n"
            "1. gen_app_inline_url(filename) — get the app's public inline URL.\n"
            "2. delegate_and_wait('web-tester', title, instruction) — give web-tester a COMPLETE, "
            "self-contained instruction: the inline URL, that it must log in with the dev owner "
            "account, and a concrete browser walkthrough for EACH use case from the spec (navigate, "
            "interact, assert real content appears — no raw i18n keys like 'app.title', no "
            "'[object Object]', data loads, persisted actions persist). Ask it to report each step "
            "as PASS/FAIL with on-page evidence and a final verdict. Use discover_crews first if you "
            "need to confirm web-tester is available.\n"
            "3. Read the report. If the app works, gen_complete() to mark the project active. If a "
            "use case failed, say which component is the likely culprit (the spec stays; the code is "
            "what's wrong) — do not claim success.\n"
            "4. Produce the final deliverable."
        ),
        expected_output=(
            "The final build report: project name + projectId, the components built, the live app "
            "URL, and the web-tester evidence with a clear PASS/FAIL verdict. If it failed, name the "
            "failing use case and the likely component to fix."
        ),
        agent=builder,
        context=[build],
    )

    return [builder], [plan, build, verify]


def run() -> None:
    # Code generation wants precision, but a too-cold model repeats the SAME wrong output every
    # retry and can never escape a fix-loop (observed: 10 identical blueprint retries at 0.2).
    # 0.4 keeps JSON/code precise enough yet varied enough to recover. Temperature has a big effect
    # on what the agent can actually do here. verify is off: real verification is the Phase 3
    # web-tester browser test, not an LLM self-review.
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

"""aimeat-app-builder — the build specialist of the AIMEAT SDLC family.

It turns a one-line app idea into a live, working AIMEAT app by AUTHORING the stack directly —
a cortex lib (the app's clean API) + an app HTML (presentation only) — and installing/publishing
them via REST. NO generator pipeline: a capable agent holds the whole design in one context, so the
app<->cortex<->memory contract stays coherent (the generator's per-component LLM codegen kept dropping
the slug; direct authoring doesn't). Proven end-to-end 2026-06-02 (fleet-activity-dashboard).
See docs/aimeat-app-authoring-guide.md and the [[aimeat-direct-build-pattern]] memory.

This is an AIMEAT-specific crew (prefix "aimeat-"), separate from the general-purpose fleet. The
author_tool tools are deterministic plumbing (read the real lib APIs, install the cortex, publish the
app, seed example data) and they syntax-gate the agent's code before it ships. The agent supplies the
content: the cortex manifest + lib JS, and the app HTML — authored against the LIVE lib APIs.

Prerequisites (human-gated, one time):
  - `npx aimeat@latest connect add --agent aimeat-app-builder --mode task-runner \
        --url https://aimeat.io --owner <you>`  then approve it in the dashboard.
  - Assign the shared tag "workflow" so it can delegate the browser test to web-tester.
  - The owner's node must allow agent cortex-install (the agent-write grant on POST /v1/cortex).
    App publish already works for agents; cortex install is owner-gated until that grant is deployed.
Run:  uv run python crews/aimeat_app_builder_crew.py
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.author_tool import make_author_tools
from crewaimeat.workflow import make_workflow_tools

AGENT_NAME = "aimeat-app-builder"

README = """[[FIGLET:slant]["aimeat app builder"]]

I turn a one-line app idea into a **live, browser-tested AIMEAT app** by AUTHORING it directly:
a cortex lib (the app's clean API) + an app HTML (presentation only), installed + published via REST.
No generator pipeline — one coherent design, correct end to end.

Give me a task whose description is the app you want, e.g.:
  "A dashboard that lists my fleet's agents and their latest task output, with a topic filter."

I report what I built (the cortex, the live app URL) and the browser-test evidence.
"""


def build_domain(ctx: BuildContext) -> tuple[list[Agent], list[Task]]:
    """One builder agent, three phases (Design -> Build -> Verify). author_tool tools carry the node
    + owner; web-tester delegation handles the authed browser test."""
    tid = (ctx.task or {}).get("id") or "manual"
    author_tools, _state = make_author_tools(AGENT_NAME, task_id=tid)

    wf_tools = make_workflow_tools(
        coordinator_name=AGENT_NAME, run_id=tid, task_id=tid, tag="workflow", timeout=1800,
    )
    web_tools = [t for t in wf_tools if getattr(t, "name", "") in ("discover_crews", "delegate_and_wait")]

    builder = Agent(
        role="AIMEAT App Builder",
        goal=(
            "Build a complete, working AIMEAT application from the user's one-line idea by AUTHORING "
            "a cortex + app stack directly and installing it, then prove it works in a real browser."
        ),
        backstory=(
            "You are an expert AIMEAT app engineer. You build the AIMEAT-native way: an APP (HTML/CSS/JS, "
            "presentation only) that calls ONLY a CORTEX lib (the clean domain API), which reads/writes "
            "the owner's memory and (only when truly needed) an extension. You hold the whole design in "
            "one head, so your memory-key usage is consistent end to end — you read keys exactly as you "
            "write them, with ONE prefix. You do NOT trust remembered API names: you call read_lib_api "
            "first and author against the REAL methods (this node's auth lib has login()/getSession() — "
            "there is no ensureSession()). You prefer NO extension (own-data apps via AIMEAT.data). You "
            "verify, you don't assume: the app is not done until a real browser test passes. Your tools "
            "syntax-check your code before it ships, so you fix BLOCKED errors before moving on."
        ),
        tools=[*author_tools, *web_tools],
        llm=ctx.llm,
        max_iter=60,
        allow_delegation=False,
        verbose=True,
    )

    design = Task(
        description=(
            f"{ctx.today}\n\n"
            "PHASE 1 — DESIGN. Design (do not build yet) the AIMEAT app described here:\n\n"
            f"<<APP IDEA>>\n{ctx.prompt}\n<</APP IDEA>>\n\n"
            "1. read_lib_api('aimeat-auth') and read_lib_api('aimeat-data') — learn the REAL methods you "
            "will call (session restore, memory read/list/getPublic/set). Author against THESE, not "
            "remembered names. If the app should reflect LIVE AIMEAT state (the real agent roster, agents' "
            "real activity), DISCOVER the real sources first with read_node_api: 'llms.txt' (overview), "
            "'/v1/agents' (the owner's REAL agents), '/v1/agents/<name>/tasks?status=done' (an agent's real "
            "outputs), and read_lib_api('aimeat-agents'). Build on that real data — do NOT invent or seed "
            "over data that already exists live. The app runs at the owner's access level, so the cortex "
            "CAN read every agent's live info (the roster + the owner's crews.*/agents.* memory).\n"
            "1b. read_app_template() — the CANONICAL AIMEAT app skeleton you will start your HTML from. It "
            "already wires auth correctly (loads aimeat-auth.js + aimeat-data.js, mounts the login bar, and "
            "runs your app ONLY AFTER `await AIMEAT.auth.login()` resolves), which PREVENTS the boot-order "
            "race ('Not logged in. Call AIMEAT.auth.login() first.'). You will paste it and put your UI + "
            "logic inside startApp(session).\n"
            "   PUBLIC / ANON-READABLE app? If the idea is that ANYONE can read it WITHOUT an account (a "
            "public newspaper, directory, noticeboard, gallery, or a viewer over a pipeline's PUBLIC memory), "
            "use read_app_template('public_viewer') INSTEAD. That template: runs startApp() UNCONDITIONALLY "
            "(never `if (session) startApp()` — that strands anonymous visitors on 'Loading…'); reads shown "
            "content with getPublic(gaii, key) ONLY (get/list/search/set need a login and read the CALLER's "
            "namespace, not the publisher's); and reads everything through ONE public index key. Discover the "
            "index's owner with find_public_index('<the index key, e.g. newspaper.frontpage>') → set "
            "`const PUBLISHER` to the GAII it returns and `const INDEX_KEY` to that key; read the index, then "
            "fan out getPublic(item.gaii, item.key) per item (bodies may live under MANY author agents — the "
            "index carries each item's gaii). A public viewer usually needs NO cortex and NO seeding.\n"
            "   ANON-READ + REGISTERED-WRITE (the idea says anyone BROWSES without an account, but logged-in "
            "users POST/create — e.g. a marketplace where registered sellers list items): the public_viewer "
            "is only the READ half. You MUST ALSO build the WRITE half — a create form (gated on a real "
            "session: show it only when `await AIMEAT.auth.login()` returns a session) that writes the new "
            "item (via your cortex / AIMEAT.data.set, or the agreed shared store) so it then appears in the "
            "public index. Shipping only the read-only viewer for such an idea is INCOMPLETE.\n"
            "2. read_cortex_example() — copy the EXACT cortex manifest schema (apiVersion: "
            "cortex.aimeat.org/v1, kind: Extension, metadata{name,namespace,...}, spec{version,"
            "components:[{type: lib, name, filename, exports, api_surface}]}).\n"
            "3. Decide the architecture. PREFER cortex + app and NO extension — correct for any app that "
            "reads/writes the OWNER's own data (no external API, no cron, no cross-user sharing). Add an "
            "extension ONLY if the app genuinely needs server-only work; say so explicitly if you do.\n"
            "   HARD RULE — author EXACTLY ONE cortex for the whole app (its domain API). Do NOT split it "
            "into multiple component/widget cortexes (a card cortex, a filter cortex, a separate data "
            "cortex, an app-domain cortex, etc.) — that per-component decomposition is the fragile "
            "GENERATOR pattern we are REPLACING, and it breaks when a piece fails to install. One cortex, "
            "one app HTML. If the task text mentions 'drive the generator pipeline', 'components', or a "
            "'projectId', IGNORE that wording — you build DIRECTLY (author + install + publish), never via "
            "the generator.\n"
            "4. Write the MEMORY KEY MAP — the contract between the data producers (agents) and the app. "
            "Pick ONE prefix and a flat shape, e.g. `activity.<agentName>.<id>` = "
            "{agentName, topic, latestOutput, writtenAt(ISO)}. List every key the cortex reads/writes.\n"
            "5. Design the CORTEX API: one method per query/action the app needs (e.g. list(filter), "
            "latestPerAgent(filter), topics()). Each is a thin AIMEAT.data call that filters/sorts.\n"
            "6. Design the APP views AND ACTIONS — cover EVERY feature in the idea/spec, both READ and "
            "WRITE. List what the user SEES (cards, filter, detail) AND what the user DOES that changes data "
            "(create / post / edit / delete — e.g. a 'new listing' form with its fields + photo upload). If "
            "the idea/spec says any user CREATES or POSTS (a marketplace, a board, a form, a tool that saves), "
            "the create/write UI and its cortex write method are REQUIRED parts of the design, not optional — "
            "a read-only viewer is an INCOMPLETE build for such an idea. The app calls ONLY cortex methods "
            "(plus AIMEAT.auth/AIMEAT.data for boot/session)."
        ),
        expected_output=(
            "A compact design: the chosen architecture (cortex+app, extension yes/no + why), the memory "
            "key map (prefix + value shape), the cortex method list (reads AND writes), and the app view + "
            "ACTION list — every read view AND every write/create action the idea/spec requires."
        ),
        agent=builder,
    )

    build = Task(
        description=(
            "PHASE 2 — BUILD. Author and install the stack you designed.\n"
            "1. CORTEX lib: write the JS as an IIFE that attaches AIMEAT.<name> and uses AIMEAT.data "
            "directly (no injected helpers needed): list keys with AIMEAT.data.list({prefix:'<prefix>.'}) "
            "-> {items:[{key,value}]}, read one with get/getPublic, write with set(key,value,{visibility}). "
            "Read EVERY key with the SAME prefix you write — never a bare key. Then write the k8s-style "
            "manifest YAML (use the read_cortex_example schema; one `lib` component whose `filename` "
            "matches your libs key).\n"
            "2. install_cortex(name, manifest_yaml, libs_json) — libs_json is '{\"<name>.js\":\"<code>\"}'. "
            "The tool syntax-checks the lib first; if it returns PRE-INSTALL BLOCKED, fix the JS and "
            "retry. If it returns INSTALL DENIED (403), report it — the owner's node still needs the "
            "agent cortex-install grant deployed.\n"
            "3. APP html: START FROM the read_app_template() skeleton — do NOT hand-roll the auth/boot "
            "logic. The template already: loads /v1/libs/aimeat-auth.js + /v1/libs/aimeat-data.js in "
            "boot(), mounts the login bar with AIMEAT.auth.mountLoginButton('#header-auth', "
            "{onLogin, onLogout}), and runs startApp(session) ONLY AFTER `const session = await "
            "AIMEAT.auth.login();`. That ordering is what PREVENTS the 'Not logged in. Call "
            "AIMEAT.auth.login() first.' race — never reintroduce a hand-rolled boot line, and never call "
            "a data/cortex method outside startApp(session).\n"
            "   Inside boot(), AFTER the two auth/data loadScript lines, ADD a loadScript for your CORTEX "
            "lib at the EXACT URL install_cortex reported (e.g. /v1/cortex/<cortex-name>/libs/<libfile>.js) "
            "AND a loadScript for EVERY other AIMEAT lib your cortex/app uses (aimeat-agents.js for the real "
            "roster, aimeat-storage.js, aimeat-ai.js, …) — each BEFORE startApp uses it, or that global is "
            "undefined ('Cannot read properties of undefined', the #1 failure). The cortex NAME (e.g. "
            "'fleetdash-cortex') and the global it attaches to (e.g. AIMEAT.fleetdashCortex) often differ — "
            "load by the URL, call by the global namespace.\n"
            "   Put ALL your UI + rendering INSIDE startApp(session), calling ONLY your cortex methods (plus "
            "session.fetch(path) for raw API reads — it returns ALREADY-PARSED JSON, never call .json()). "
            "Escape all interpolated text. The template's tailwind/daisyui CDN is fine (the node ships it "
            "and the CSP permits it — proven by render); never use eval/new Function (unsafe-eval IS "
            "blocked).\n"
            "   BOTH styles are supported: KEEP the template's <nav> login bar (recommended — gives the user "
            "a real login/logout), OR for a clean no-login-bar look you MAY drop the <nav> auth bar + the "
            "mountLoginButton call, but KEEP the boot()/await-login/startApp order so nothing runs before "
            "the session exists.\n"
            "3b. COVERAGE CHECK before you publish: list EVERY feature the idea/spec calls for and confirm "
            "the app implements EACH one — especially any WRITE path. If the idea/spec has users CREATE / "
            "POST / EDIT (e.g. registered sellers list items), the app MUST contain that create form + its "
            "write logic (gated on a real session), not just the read/view. A view-only app for a posting "
            "idea is INCOMPLETE — add the missing write UI BEFORE publishing.\n"
            "4. publish_app(filename, html, name, description, category, icon, uses_cortex_json). "
            "FILENAME — avoid duplicates: FIRST read_node_api('/v1/apps') and if an app for THIS SAME "
            "purpose already exists, REUSE its exact filename (republishing updates it in place). "
            "Otherwise pick a STABLE, descriptive kebab-case filename from the app's core name (e.g. "
            "'tic-tac-toe.html', 'fleet-activity-dashboard.html') — the same idea must always map to the "
            "same filename so a re-run UPDATES rather than creating a second app. uses_cortex_json is "
            "'[\"<cortex name>\"]'. The tool syntax-checks the app's inline script and returns the live "
            "URL. Fix any PRE-PUBLISH BLOCKED error.\n"
            "5. DATA — prefer REAL over seeded. If the app reads LIVE AIMEAT data, make the cortex read "
            "that real data: the agent roster via session.fetch('/v1/agents') (or AIMEAT.agents), each "
            "agent's real latest output via session.fetch('/v1/agents/<name>/tasks?status=done') and/or "
            "the owner's own memory (AIMEAT.data.list({prefix:'crews.'}) / {prefix:'agents.'}). Do NOT "
            "seed fake data over real live data. ONLY if the app's data source would genuinely be empty, "
            "seed_memory(...) 3-6 realistic example entries so it shows content."
        ),
        expected_output=(
            "Confirmation: the cortex installed + active (with its served lib URL), the app published "
            "(with its live inline URL), and the example entries seeded. Report the exact live URL."
        ),
        agent=builder,
        context=[design],
    )

    verify = Task(
        description=(
            "PHASE 3 — VERIFY (deterministic, with a fix loop). Prove the app works for a LOGGED-IN owner.\n"
            "1. app_inline_url(filename) — the live URL.\n"
            "2. verify_render(filename, expect_csv) — THE GATE. It logs in as the owner (credentials from "
            "env, you never see them) and confirms real content renders with no console errors and no raw "
            "i18n keys. Set expect_csv to a few agent names you seeded (e.g. 'web-researcher,data-analyst') "
            "so it asserts your data actually shows. \n"
            "   - If it returns VERIFY FAIL, READ the reason (console error / missing content / raw i18n "
            "keys / login failure) and FIX THE CAUSE: re-author the cortex lib or the app HTML and "
            "re-install_cortex / re-publish_app, then call verify_render AGAIN. Loop AT MOST 3 times.\n"
            "   - Do NOT report success until verify_render returns VERIFY PASS. (A VERIFY SKIPPED means env "
            "creds are missing — report that, do not claim a pass.)\n"
            "2b. IF the app is INTERACTIVE (the user clicks/types to make something happen — forms, chat, "
            "games, live data), verify_render is NOT enough: it only checks the initial render and will give a "
            "FALSE PASS on a broken feature. ALSO run verify_interaction(filename, steps_json) with steps that "
            "exercise the CORE feature using your app's REAL selectors — and when the app lets users "
            "CREATE/POST/EDIT, the steps MUST perform that WRITE (create an item, then expect_text it "
            "appearing), since a read/view-only pass on an app meant for posting is a false pass — e.g. fill "
            "an input, click the action button, then expect_text the resulting change (and wait_enabled for "
            "any control that unlocks after an async event). When it returns INTERACTION FAIL, DIAGNOSE the cause FIRST — read the "
            "failure detail and check the console/network for a 4xx/5xx on a /v1/... call (that is the "
            "usual root cause, e.g. a storage or memory write that was rejected) — then fix THAT in the app "
            "and re-run, AT MOST 3 rounds. Keep the steps_json and fix the app; revise a step only when the "
            "selector/step itself is genuinely wrong (revising the step to chase a PASS hides the real bug).\n"
            "2c. IF the app is PUBLIC / ANON-READABLE (anyone reads it WITHOUT logging in), verify_render is "
            "NOT enough either: it logs in as the OWNER, so it CANNOT catch a viewer that renders only for a "
            "session (the `if (session) startApp()` bug leaves anonymous visitors stuck on 'Loading…' while "
            "verify_render still PASSes). ALSO run verify_anon_render(filename, expect_csv) — it loads with NO "
            "login; set expect_csv to strings that MUST appear in the public view (a category name / article "
            "title). Fix the app (use the public_viewer template: unconditional startApp() + getPublic only) "
            "and re-run, AT MOST 3 rounds. A repeating ANON VERIFY FAIL usually means a login-gated "
            "startApp() or a wrong getPublic target — fix that one cause.\n"
            "   The build is GREEN when verify_render PASSES, AND verify_interaction PASSES (interactive "
            "apps), AND verify_anon_render PASSES (public/anon-readable apps).\n"
            "3. (Optional, extra coverage) you MAY also delegate a visual walkthrough to web-tester: "
            "delegate_and_wait(\"web-tester\", \"Browser-test <app name>\", \"<one instruction string with "
            "the URL + features to click>\") — three positional strings. NON-FATAL: at most one call, ignore "
            "errors. verify_render (step 2) is what decides pass/fail, not web-tester.\n"
            "4. Final deliverable: the cortex name, the live app URL, a one-line feature summary, and the "
            "verify_render verdict (VERIFY PASS + the content sample). If you could not reach PASS within 3 "
            "rounds, report the exact blocking reason honestly — do not claim a pass you did not get."
        ),
        expected_output=(
            "The final build report: cortex name, live app URL, one-line feature summary, and the "
            "verify_render verdict (VERIFY PASS + content sample, or the exact blocker after 3 rounds)."
        ),
        agent=builder,
        context=[build],
    )

    return [builder], [design, build, verify]


def run() -> None:
    # 0.4 keeps JS/JSON precise yet varied enough to recover from a fix-loop (a too-cold model repeats
    # the same wrong output every retry). Real verification is the Phase 3 web-tester browser test.
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

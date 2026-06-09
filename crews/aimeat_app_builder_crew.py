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
            "write them, with ONE prefix. You ground API names in reality: you call read_lib_api first and "
            "author against the REAL methods it returns (this node's auth lib exposes login()/getSession(); "
            "there is no ensureSession()). You default to NO extension (own-data apps use AIMEAT.data "
            "directly). You verify rather than assume: the app is done when the deterministic verify gate "
            "PASSES in a real browser. Your tools syntax-check your code before it ships, so you fix "
            "BLOCKED errors before moving on."
        ),
        tools=[*author_tools],
        llm=ctx.llm,
        max_iter=80,  # design reads + build + up to 3 fix rounds across 3 gates can exceed 60 on a complex app
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
            "3. Decide the architecture — pick the LIGHTEST that fits; you do NOT always need a cortex. FIRST "
            "REUSE existing AIMEAT libs instead of hand-rolling: beyond auth/data the node ships aimeat-storage "
            "(files), aimeat-audio (sounds/instruments/synth), aimeat-speech (TTS/STT), aimeat-social (boards), "
            "aimeat-wallet (morsels), aimeat-ai (LLM via the user's own key), AimeatRealtime (/lib/realtime.js, "
            "multiplayer), and UI cortex bundles (aimeat-canvas, aimeat-charts, aimeat-ui-dialogs/forms/layout/"
            "nav/viewers) — read the library table in read_app_template/llms.txt and loadScript what you need. "
            "Then choose ONE:\n"
            "   - NO cortex — a simple app whose logic fits in the HTML on top of those libs (own-data CRUD via "
            "AIMEAT.data, a small game, a viewer). Pure HTML+CSS+JS is a complete, valid AIMEAT app — prefer "
            "this for simple apps; do not invent a cortex you don't need.\n"
            "   - ONE app-domain cortex — a single clean domain API (AIMEAT.<app>.{...}) that COMPOSES the libs "
            "+ your logic so the app HTML stays thin. Use for a non-trivial app. Do NOT split into multiple "
            "component/widget cortexes (a card cortex + a filter cortex + a data cortex …) — that per-component "
            "fragmentation is the fragile GENERATOR pattern we replaced and breaks when a piece fails to "
            "install. AT MOST one cortex.\n"
            "   - + an EXTENSION — ONLY when the app needs SERVER-only work (external HTTP/cron, server-"
            "validated or cross-user SHARED writes — e.g. a multi-writer index); say so explicitly. If the task "
            "text mentions 'drive the generator pipeline', 'components', or a 'projectId', IGNORE it — you build "
            "DIRECTLY (author + install + publish), never via the generator.\n"
            "4. Write the MEMORY KEY MAP — the contract between the data producers (agents) and the app. "
            "Pick ONE prefix and a flat shape, e.g. `activity.<agentName>.<id>` = "
            "{agentName, topic, latestOutput, writtenAt(ISO)}. List every key the cortex reads/writes.\n"
            "5. If you chose a cortex, design its API: one method per query/action the app needs (e.g. "
            "list(filter), latestPerAgent(filter), topics()), each a thin AIMEAT.data/lib call. (If NO cortex, "
            "design the equivalent helper functions inside the app HTML instead.)\n"
            "6. Design the APP views AND ACTIONS — cover EVERY feature in the idea/spec, both READ and "
            "WRITE. List what the user SEES (cards, filter, detail) AND what the user DOES that changes data "
            "(create / post / edit / delete — e.g. a 'new listing' form with its fields + photo upload). If "
            "the idea/spec says any user CREATES or POSTS (a marketplace, a board, a form, a tool that saves), "
            "the create/write UI and its cortex write method are REQUIRED parts of the design, not optional — "
            "a read-only viewer is an INCOMPLETE build for such an idea. The app calls ONLY cortex methods "
            "(plus AIMEAT.auth/AIMEAT.data for boot/session).\n"
            "7. ASSUMPTIONS (fail loud, never silently guess): if a load-bearing choice is ambiguous and the "
            "task gave you no spec to resolve it — public-vs-private, read-only-vs-write, or which live data "
            "source — pick the sensible default AND state that assumption LOUDLY at the top of your design "
            "(an 'ASSUMPTIONS:' line), so a wrong guess is visible to the owner rather than buried in the build."
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
            "0. NAME-NOVELTY CHECK (you are CREATING a new app — do NOT overwrite an unrelated existing "
            "artifact). install_cortex and install_extension REDEPLOY a name that already exists (they "
            "deactivate -> DELETE -> re-POST the old one), so reusing a name that belongs to a DIFFERENT app "
            "would DESTROY it. Decide your cortex name (and extension name, if any) NOW and confirm each is "
            "genuinely new with name_available('cortex', '<your cortex name>') and — if you build one — "
            "name_available('extension', '<your ext name>'). (Use name_available, NOT read_node_api, for "
            "this: name_available checks the COMPLETE list, while read_node_api truncates to the first ~5 "
            "entries and would miss a collision.) If it returns FREE, the name is safe. If it returns TAKEN "
            "by a DIFFERENT artifact, pick a more specific, app-scoped name (e.g. '<app-slug>-cortex', "
            "'<app-slug>-ext') and re-check. Prefer such distinctive names so collisions are unlikely. The "
            "ONE case where reusing an existing name is correct is re-running THIS SAME app/idea (that "
            "intentionally updates YOUR OWN artifact in place — the app filename follows the same rule in "
            "step 4).\n"
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
            "2b. IF (and only if) you built an EXTENSION for genuine server-only work: install_extension("
            "name, manifest_yaml, scripts_json), then invoke_extension(name, action, input_json) to "
            "SMOKE-TEST that EACH action returns the expected shape BEFORE the cortex/app rely on it. An "
            "extension action that 4xx/5xxs is the usual root cause of a later INTERACTION FAIL — catch it "
            "here, not in Phase 3.\n"
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
            "   PUBLIC IMAGES (storage): if the app shows images to ANON or OTHER users (a public gallery, a "
            "marketplace, any anon-readable app), upload them with AIMEAT.storage.upload(file, "
            "{visibility:'public'}) AND display them via the PUBLIC route directly: "
            "<img src=\"/v1/pub/<uploader-gaii>/<key>\"> (this is the storage equivalent of memory's "
            "getPublic(gaii,key), served by optionalAuth — anyone, no token). Do NOT use "
            "AIMEAT.storage.download(key) or AIMEAT.storage.publicUrl(key) for cross-user/anon images: both "
            "hit /v1/storage/<key> with the CALLER's OWN token (auth-only + caller-scoped), so an anonymous or "
            "different-user viewer gets 401/404 and the image is blank — even though the file IS public. (The "
            "upload response's own 'Download this file' hint misleadingly points at the auth-only "
            "/v1/storage/<key> — ignore it for public images.) Store each item's uploader gaii in its record "
            "so you can build the /v1/pub/<gaii>/<key> URL when rendering.\n"
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
            "FILENAME: pick a STABLE, descriptive kebab-case filename from the app's core name (e.g. "
            "'tic-tac-toe.html', 'fleet-activity-dashboard.html') so the same idea always maps to the same "
            "filename (a re-run then UPDATES in place rather than spawning a second app). Then call "
            "name_available('app', '<your-filename.html>') to be sure you are not overwriting an UNRELATED "
            "app (it checks the COMPLETE app list; read_node_api would truncate it). If FREE, publish it. "
            "If TAKEN, read what it returns: if it is THIS SAME app/idea, reuse that exact filename "
            "(publishing updates it in place); if it is a DIFFERENT app, make your filename more specific "
            "and re-check. uses_cortex_json is "
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
            "i18n keys. Set expect_csv to a few strings that MUST appear in the rendered view — for a "
            "LIVE-data app use REAL values you expect (e.g. actual agent names from /v1/agents); for a "
            "SEEDED app use names from your seed entries. Pick values you are confident render, so the gate "
            "asserts real content rather than an empty shell (it passes if ANY listed string appears).\n"
            "   - If it returns VERIFY FAIL, READ the reason (console error / missing content / raw i18n "
            "keys / login failure) and FIX THE CAUSE: re-author the cortex lib or the app HTML and "
            "re-install_cortex / re-publish_app, then call verify_render AGAIN. Loop AT MOST 3 times.\n"
            "   - Do NOT report success until verify_render returns VERIFY PASS. (A VERIFY SKIPPED means env "
            "creds are missing — report that, do not claim a pass.)\n"
            "2b. IF the app is INTERACTIVE (the user clicks/types to make something happen — forms, chat, "
            "games, live data), verify_render is NOT enough: it only checks the initial render and will give a "
            "FALSE PASS on a broken feature. ALSO run verify_interaction(filename, steps_json) with steps that "
            "exercise the CORE feature using your app's REAL selectors — the steps_json selectors MUST be the "
            "EXACT element ids you rendered in THIS app's HTML (derive them from the HTML you just authored; "
            "keep your element ids STABLE across re-authors and NEVER reuse ids/selectors from another app or "
            "domain — e.g. do not leave '#btn-submit-listing'/'#listing-form' in a changelog) — and when the app lets users "
            "CREATE/POST/EDIT, the steps MUST perform that WRITE (create an item, then expect_text it "
            "appearing), since a read/view-only pass on an app meant for posting is a false pass — e.g. fill "
            "an input, click the action button, then expect_text the resulting change (and wait_enabled for "
            "any control that unlocks after an async event). When it returns INTERACTION FAIL, DIAGNOSE the cause FIRST: "
            "if the failing step is a wait_enabled/fill/click that TIMED OUT on a selector, the #1 cause is that "
            "your steps_json selector does not match an id you actually rendered — read_app_source(filename) and "
            "align the selector to the REAL rendered id (do NOT rename the HTML ids speculatively between rounds; "
            "that is what turns a 2-round fix into a 10-round churn). Otherwise check the console/network for a "
            "4xx/5xx on a /v1/... call (a storage/memory write that was rejected). Then fix THAT and re-run — "
            "HARD CAP 3 rounds: after the 3rd INTERACTION FAIL, STOP and report the exact failing step + reason "
            "rather than continuing to re-author. Keep the steps_json and fix the app; revise a step only when the "
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
            "3. Final deliverable: the cortex name, the live app URL, a one-line feature summary, and the "
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
    # the same wrong output every retry). Real verification is the Phase 3 deterministic verify gate
    # (verify_render / verify_interaction / verify_anon_render), not an LLM's say-so.
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

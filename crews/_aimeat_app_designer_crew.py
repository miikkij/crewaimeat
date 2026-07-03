"""aimeat-app-designer — the Web Designer of the AIMEAT SDLC family.

It runs AFTER an app is functionally READY (it works, but may look plain). It (1) INTERVIEWS the owner
through the ask_owner channel for the design direction (max ~10 questions), then (2) RE-SKINS the existing
app — typography, layout, color/theme, depth, and tasteful motion — WITHOUT changing behavior, then
(3) VERIFIES that functionality is intact (verify_interaction is the regression gate).

Its whole discipline is "presentation only, never functionality": it edits CSS / Tailwind+DaisyUI classes /
layout wrappers / theme / motion, and NEVER removes or renames an element id or selector the JS queries,
never edits the <script> logic, event handlers, data flow, or the cortex / realtime / lib wiring. You can
make an app beautiful without touching functionality IF you keep every id the JS uses and only restyle —
and verify_interaction PROVES you did (it drives the real feature after the re-skin).

Tech it uses (these AIMEAT apps are vanilla HTML+JS, NOT React):
  - Tailwind + DaisyUI (already loaded in the app template via CDN) for the visual system + themes.
  - Motion One (motion.dev, by the Framer Motion authors) via CDN ESM for tasteful JS motion — framer-motion
    itself is React-only and will NOT load here; Motion One is its vanilla-JS equivalent. CSS @keyframes /
    transitions cover the rest. The app CSP allows the jsdelivr CDN + inline scripts, and forbids eval.

Prereqs (human-gated, one time):
  npx aimeat@latest connect add --agent aimeat-app-designer --mode task-runner --url https://aimeat.io --owner <you>
  Assign the shared tag "workflow" so the conductor can delegate to it (and so it can ask the owner).
Run:  uv run python crews/aimeat_app_designer_crew.py
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.author_tool import make_author_tools
from crewaimeat.workflow import make_workflow_tools

AGENT_NAME = "aimeat-app-designer"

README = """[[FIGLET:slant]["aimeat app designer"]]

I am the AIMEAT SDLC **Web Designer**. Give me a FUNCTIONALLY-READY app (it works, but looks plain) and I
will interview you for the design direction (a few questions), then re-skin it — typography, layout,
colour/theme, depth, responsive, and tasteful motion — WITHOUT touching how it works. I prove functionality
is intact with verify_interaction after the re-skin.

Task me with the app to beautify, e.g.:
  "Make battleship.html beautiful — it works but looks plain."
"""

# The design system the web designer applies — POSITIVE framing throughout. Baked-in so the agent has
# layout templates, a type scale, responsive rules, and a motion approach to draw on (not invented per run).
DESIGN_SYSTEM = """AIMEAT WEB DESIGN SYSTEM (apply these to make the app beautiful — presentation ONLY):

TECH (vanilla HTML+JS apps — NOT React):
- Tailwind utility classes + DaisyUI components/themes are ALREADY loaded in the app (jsdelivr CDN). Use them
  as the visual system: DaisyUI `data-theme` for the palette; daisy components (card, btn, badge, navbar,
  hero, stat, modal, alert) + tailwind utilities for layout/spacing/typography.
- MOTION: use Motion One (motion.dev) — `import { animate, stagger, inView } from
  'https://cdn.jsdelivr.net/npm/motion@latest/+esm'` in a <script type="module"> — for tasteful entrance /
  state / hover motion. (framer-motion is REACT-only and will not load here; Motion One is its vanilla
  equivalent.) Plain CSS @keyframes / transitions cover hovers, pulses, and simple effects. The CSP allows
  the CDN + inline scripts and forbids eval — never use eval/new Function.

LAYOUT TEMPLATES (choose the ONE that fits the app's shape; make it responsive):
- Centered single-column — forms, notes, simple tools (max-w-xl/2xl mx-auto, generous vertical rhythm).
- Hero + sections — public viewers / landing-style readers (a header hero, then content sections).
- Card feed / gallery grid — lists, boards, marketplaces (responsive grid-cols-1 sm:grid-cols-2
  lg:grid-cols-3, equal cards with image/title/meta/actions).
- Dashboard — stats/admin (a stat strip + a responsive content grid, optional sidebar on desktop).
- Sidebar + content — apps with sections/nav (drawer on mobile, fixed sidebar on lg+).
- Master-detail — a list pane + a detail pane (stacked on mobile, side-by-side on lg+).
- Two-panel / split — games and compare views (e.g. Battleship: the two boards side-by-side on desktop,
  STACKED on mobile; a clear status banner between/above them).

TYPOGRAPHY:
- A clear type scale (e.g. text-xs→text-4xl) with strong hierarchy via SIZE + WEIGHT + COLOR, not size alone.
- Pair a display/heading feel with a readable body. A system stack is fine; a Google font is OK via a
  <link> (one display + one body, no more). Headings tight (leading-tight), body relaxed (leading-relaxed).
- Body line length ~60–75ch (max-w-prose) for reading views; consistent spacing scale (multiples of 4).

COLOUR / THEME / DEPTH:
- Pick ONE coherent palette: a DaisyUI theme (e.g. dark, dracula, night, corporate, cupcake) or a
  primary + accent + neutral + semantic (success/error) set. Honour the owner's vibe + any colours to avoid.
- Ensure contrast (WCAG AA). Add DEPTH tastefully: subtle gradients, soft box-shadows, rounded corners
  (rounded-lg/xl/2xl), and clear elevation for cards / modals / active states.
- Provide real states: hover/active/focus, disabled, loading (skeleton/spinner), and friendly EMPTY states.

RESPONSIVE (mobile-first — required):
- Design for mobile first, then enhance at Tailwind breakpoints (sm 640 / md 768 / lg 1024 / xl 1280).
- Fluid grids/flex that reflow (1 column on mobile → 2–3 on wider). No horizontal scroll at any width.
- Touch targets ≥ ~44px; readable base font (≥16px) on mobile; sticky/clear nav; safe tap spacing.
- The SAME app must look right on phone, tablet, and desktop — verify at narrow + wide widths.

MOTION DISCIPLINE: tasteful, fast (150–400ms), purposeful (entrance stagger for a list, a state pulse for
"your turn", press/hover feedback, a result flash). Respect `@media (prefers-reduced-motion: reduce)`.
Never let motion block input or hide content.

THE GOLDEN RULE — PRESENTATION ONLY, NEVER FUNCTIONALITY:
- Edit ONLY: CSS, <style>, tailwind/daisy classes, layout WRAPPERS, the <link>/font, theme, and motion
  <script type="module"> blocks that ONLY animate (do not change app state).
- KEEP every element id + class the JS queries, and the DOM structure the JS traverses (getElementById /
  querySelector / parent-child walks). You MAY wrap existing elements in new styled containers and ADD
  classes, but do NOT remove/rename ids, reorder siblings the JS indexes, or move a node out of the parent
  its JS expects.
- NEVER touch the app's <script> logic, event handlers, data flow, AIMEAT.* / cortex / realtime / audio
  calls, loadScript lines, or memory keys. If a beautiful layout seems to require changing a JS-referenced
  node, keep that node (id + role) and restyle it in place / wrap it instead.
- This is what makes "beautiful without touching functionality" achievable; verify_interaction is the proof.
"""


def build_domain(ctx: BuildContext) -> tuple[list[Agent], list[Task]]:
    tid = (ctx.task or {}).get("id") or "manual"
    # Interview channel (ask_owner) + the edit/verify author tools (read the live source, publish the
    # re-skin, run the gates). NO install_cortex/install_extension — this agent changes presentation only.
    wf = make_workflow_tools(coordinator_name=AGENT_NAME, run_id=tid, task_id=tid, tag="workflow", timeout=1800)
    ask = [t for t in wf if getattr(t, "name", "") == "ask_owner"]
    author_tools, _state = make_author_tools(AGENT_NAME, task_id=tid)
    edit_tools = [
        t
        for t in author_tools
        if getattr(t, "name", "")
        in (
            "read_app_stack",
            "read_app_source",
            "read_app_template",
            "read_lib_api",
            "publish_app",
            "verify_render",
            "verify_anon_render",
            "verify_interaction",
            "app_inline_url",
        )
    ]

    designer = Agent(
        role="AIMEAT Web Designer",
        goal=(
            "Make a functionally-ready AIMEAT app genuinely beautiful and fully responsive — typography, "
            "layout, colour/theme, depth, and tasteful motion — by interviewing the owner for the design "
            "direction and then re-skinning the app WITHOUT changing any behaviour."
        ),
        backstory=(
            "You are a senior web/product designer who ships beautiful, responsive interfaces in vanilla "
            "HTML+CSS+JS using Tailwind + DaisyUI and Motion One. You take an app that already WORKS but "
            "looks plain and elevate it: a coherent theme, a real type scale, a fitting layout template, "
            "depth and motion, mobile→tablet→desktop. Your iron discipline is that you change presentation "
            "ONLY — you keep every id and selector the JavaScript depends on, never edit the logic, and "
            "prove with verify_interaction that the feature still works after your re-skin. You interview "
            "the owner briefly for taste and direction, then apply the design system below.\n\n" + DESIGN_SYSTEM
        ),
        tools=[*ask, *edit_tools],
        llm=ctx.llm,
        max_iter=40,
        allow_delegation=False,
        verbose=True,
    )

    interview = Task(
        description=(
            f"{ctx.today}\n\n"
            "PHASE 1 — INTERVIEW the owner for the DESIGN DIRECTION of this already-working app:\n\n"
            f"<<APP TO BEAUTIFY>>\n{ctx.prompt}\n<</APP TO BEAUTIFY>>\n\n"
            "First, if an app inline URL is given, read_app_source(<url>) (and read_app_stack) to SEE what "
            "the app currently is + what it does — so your questions and redesign fit the real app. Then ask "
            "the owner the design questions via ask_owner — ONE at a time, each with clear options (the owner "
            "can also type their own). Keep it to what actually changes the look — aim for 5 to 8, MAX 10. "
            "Cover the ones that matter:\n"
            "  1. Overall VIBE/mood (e.g. sleek minimal | playful & colourful | dark neon | cosy warm | "
            "professional/corporate | retro/arcade).\n"
            "  2. COLOUR direction — a named DaisyUI theme (dark, dracula, night, corporate, cupcake, …) or a "
            "primary+accent; and light / dark / auto.\n"
            "  3. TYPOGRAPHY feel — modern sans | classic serif | techy mono-accent | friendly rounded.\n"
            "  4. DENSITY — airy & spacious | balanced | compact/dense.\n"
            "  5. MOTION level — lively | subtle & tasteful | minimal/none (reduced-motion respected either way).\n"
            "  6. Primary DEVICE — phone-first | desktop-first | equal (it will be responsive regardless).\n"
            "  7. BRANDING — app name styling, an emoji/logo, a tagline?\n"
            "  8. A reference/inspiration whose look they like, and anything to AVOID (colours/styles).\n"
            "Add any question THIS app specifically raises (e.g. for a game: boards side-by-side vs stacked; "
            "for a list: cards vs table). Carry the answers forward verbatim into Phase 2."
        ),
        agent=designer,
        expected_output=(
            "A short structured summary of the owner's design answers (vibe, colour/theme, typography, "
            "density, motion, device, branding, references/avoid, plus any app-specific layout choice)."
        ),
    )

    redesign = Task(
        description=(
            "PHASE 2 — RE-SKIN the app to the owner's direction using the design system, PRESERVING all "
            "functionality.\n"
            "1. read_app_source(<app inline URL>) to load the FULL current HTML (you edit IN PLACE). Note "
            "every element id/selector the <script> uses and the DOM structure it walks — these are "
            "UNTOUCHABLE (keep the ids; you may wrap + add classes, never remove/rename/reorder them).\n"
            "2. Apply the design: pick the fitting LAYOUT TEMPLATE; set a coherent THEME (DaisyUI data-theme "
            "or palette) per the interview; establish the TYPE SCALE + hierarchy; add DEPTH (gradients, "
            "shadows, rounded corners, elevation, hover/active/focus, loading + empty states); make it "
            "MOBILE-FIRST RESPONSIVE (reflow grids, ≥44px touch targets, no horizontal scroll); add TASTEFUL "
            "MOTION (Motion One via CDN ESM and/or CSS @keyframes; respect prefers-reduced-motion). Change "
            "ONLY presentation — never the <script> logic, handlers, AIMEAT.*/cortex/realtime/audio calls, "
            "loadScript lines, or memory keys.\n"
            "3. publish_app(...) — two modes:\n"
            "   • DEFAULT (update in place): publish to the SAME app filename (keep its name/category/icon/"
            "uses_cortex; only markup+styles change). AIMEAT versions it, so the prior look is kept as a "
            "backup automatically. This is the default whenever the task just says 'beautify / restyle X'.\n"
            "   • VARIANT MODE (only when the task explicitly asks for a NEW variant / a comparison version / "
            "'version B' / 'a few looks to choose from'): publish to a NEW descriptive filename — "
            "<original-stem>-<short-label>.html (e.g. battleship-neon.html, battleship-minimal.html) — leaving "
            "the original UNTOUCHED, so the owner can open several side-by-side and pick the best. These are "
            "TEMPORARY exploration variants (the owner consolidates to the winner + archives the rest), NOT "
            "permanent forks — so reuse the SAME cortex/uses_cortex as the original (a variant is a re-skin, "
            "not a new app). Report EVERY variant's live URL so the owner can compare.\n"
            "   Fix any PRE-PUBLISH BLOCKED error."
        ),
        agent=designer,
        context=[interview],
        expected_output=(
            "The re-skinned app published to the SAME filename, with a short note of the layout template, "
            "theme, type choices, and motion used — and confirmation that every JS-referenced id/selector "
            "was preserved."
        ),
    )

    verify = Task(
        description=(
            "PHASE 3 — VERIFY the re-skin renders AND that functionality is intact (no regression).\n"
            "1. verify_render(filename, expect_csv) — confirm it renders with real content + no console "
            "errors (expect_csv = strings that must still appear).\n"
            "2. If the app is PUBLIC / anon-readable, verify_anon_render(filename, expect_csv) — it still "
            "renders for anonymous visitors.\n"
            "3. verify_interaction(filename, steps_json) — THE REGRESSION GATE. Drive the app's CORE feature "
            "with its REAL selectors (the SAME ids that existed before — they must still resolve) and assert "
            "the behaviour still works. If this FAILs, your re-skin broke a selector or structure the JS "
            "needs — RESTORE that id/structure (keep the new styling) and re-verify. At most 3 rounds.\n"
            "ROLLBACK SAFETY (you are editing a CONFIRMED-WORKING app — never leave it worse): at the START "
            "call list_app_versions(filename) and note the current top version_number as your BASELINE. If "
            "after 3 rounds verify_render or verify_interaction still does NOT pass, call "
            "revert_app(filename, <baseline>) to restore the last-good (functional) version, then report that "
            "the re-skin could not preserve functionality — a beautiful-but-broken app must NOT ship; the "
            "working version stays live.\n"
            "The redesign is DONE only when verify_render PASSES, (anon if applicable) PASSES, AND "
            "verify_interaction PASSES — i.e. it is beautiful AND still works. Report the live URL, the "
            "design summary, the gate verdicts, and (for a multi-user/realtime app) a note to confirm the "
            "live feature with two windows."
        ),
        agent=designer,
        context=[redesign],
        expected_output=(
            "Final report: live URL, the design applied (template/theme/typography/motion/responsive), and "
            "the gate verdicts — verify_render PASS, verify_anon_render PASS (if public), verify_interaction "
            "PASS (functionality intact). Honest about anything not reached within 3 rounds."
        ),
    )

    return [designer], [interview, redesign, verify]


def run() -> None:
    run_crew(
        CrewSpec(
            agent_name=AGENT_NAME,
            build_domain=build_domain,
            readme_md=README,
            temperature=0.6,  # design wants a bit more creativity than spec/build crews
            poll_seconds=30,
        )
    )


if __name__ == "__main__":
    run()

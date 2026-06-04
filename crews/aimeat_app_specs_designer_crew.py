"""aimeat-app-specs-designer — the Solutions Architect of the AIMEAT SDLC family.

It sits BEFORE the build: given a one-line app idea, it (1) INTERVIEWS the owner through the
ask_owner channel to pin the requirements, then (2) applies the AIMEAT technical playbook to emit a
precise, build-ready TECHNICAL SPEC for aimeat-app-builder / aimeat-cortex-fixer to follow. Getting the
architecture right up front (which data layer, who reads/writes, how images work) is what keeps the
builder from looping on a wrong foundation.

Everything here is written in POSITIVE framing — it states what TO do (LLMs follow goals far better
than prohibitions). The playbook draws on docs/aimeat-guides (the canonical design wisdom: the dual
interviewer role, the decision points, the layered architecture, the golden rules) PLUS the proven
direct-build mechanics (author + publish via author_tool, no generator). See [[aimeat-direct-build-pattern]].

Prereqs (human-gated, one time):
  npx aimeat@latest connect add --agent aimeat-app-specs-designer --mode task-runner --url https://aimeat.io --owner <you>
  Assign the shared tag "workflow" so the conductor can delegate to it (and so it can ask the owner).
Run:  uv run python crews/aimeat_app_specs_designer_crew.py
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.memory_tools import make_memory_tools
from crewaimeat.workflow import make_workflow_tools

AGENT_NAME = "aimeat-app-specs-designer"

README = """[[FIGLET:slant]["specs designer"]]

I am the AIMEAT SDLC **Solutions Architect**. Give me a one-line app idea and I will interview you for
the few decisions that shape the build, then hand the builder a precise, AIMEAT-correct technical spec —
so the app is built right the first time.

Task me with the app idea, e.g.:
  "A marketplace where registered users list items with photos and buyers contact the seller."
"""

# The AIMEAT technical playbook the architect applies — POSITIVE framing throughout.
PLAYBOOK = """AIMEAT TECHNICAL PLAYBOOK (apply these to choose the right architecture):

DATA LAYER — pick the ONE that matches who writes + the ownership/scale needs:
- OWNER-CURATED (only the owner writes; anyone reads): the owner (logged in) writes items to their OWN
  GHII namespace via AIMEAT.data.set(..., {visibility:'public'}) and maintains ONE public index key; the
  viewer reads the index and fans out getPublic(gaii,key). Best for a shop/blog/noticeboard the owner curates.
- MULTI-USER, LOGIN-GATED writes (any logged-in user writes; anyone reads): each user writes their item to
  THEIR OWN namespace (real per-user ownership) via AIMEAT.data.set public; discovery uses ONE shared index
  — a micro-memory public_write set holding lightweight {gaii,key,...} pointers that logged-in users append
  to; the viewer reads the index and getPublic's each item. Images upload with each user's own token. Best
  for a marketplace/community where registered users contribute.
- OPEN (anyone writes, including anonymous): a micro-memory public_write set holds the items; any visitor
  adds via GET /v1/mm?op=add. Anonymous writers use storage keys prefixed 'anonymous/'. This is demo-grade
  (shared writes, ~100 keys/set, ≤1KB values) — choose it only when open + no-ownership is acceptable.
- SERVER-BACKED (real ownership + server validation + external APIs + schedules): a server-side EXTENSION
  owns the ext:{name} namespace and exposes actions; the app calls them through a data cortex. The owner
  installs the extension (install is owner-gated), so flag this as an owner step / v2 when it applies.

READ + RENDER for everyone (public apps): start from read_app_template('public_viewer') — startApp() runs
unconditionally so anonymous visitors render; read shown content with getPublic(gaii,key) (the one
anonymous read). Point PUBLISHER at the GAII that owns the index; carry each item's full {gaii,key} in the
index so bodies can live under many authors.

IMAGES / FILES (AIMEAT storage): upload with a token via POST /v1/storage {key, visibility:'public', data:
base64, mime_type}; store the returned key. An ANONYMOUS uploader uses a key prefixed 'anonymous/'; a
logged-in user uses their own namespace. DISPLAY an image by fetching /v1/storage/<key> WITH a token, then
URL.createObjectURL(blob) → img.src (storage serves through auth even for public files). The viewer gets an
anon token (POST /v1/auth/anonymous, which carries storage:read/write) for display.

AUTH: AIMEAT.auth.login() returns the owner session or null for anonymous; gate write/admin UI on a real
session (session.ghii). Anonymous visitors read public content; logged-in users write their own namespace.

CONVENTIONS: use static inline JS (the app CSP supports inline scripts + the jsdelivr CDN for tailwind/
daisyui; it runs without eval). Escape every user-supplied value with esc() before the DOM. Keep
micro-memory values ≤1KB and rotate sets past ~100 keys.

NAMESPACING: memory keys are scoped to the WRITING identity's GAII. To read another identity's PUBLIC key
use getPublic(gaii,key); to aggregate across many writers use a shared index (above). An agent writes its
deliverable under its own GAII (so an index it maintains is the PUBLISHER the viewer points at).

VERIFY: plan the gates the builder will run — verify_render (logged-in owner) for every app;
verify_anon_render (no login) for anything anonymous visitors must read; verify_interaction (drive the core
feature with real selectors) for anything interactive. The app is GREEN when the applicable gates PASS.
"""


def build_domain(ctx: BuildContext) -> tuple[list[Agent], list[Task]]:
    tid = (ctx.task or {}).get("id") or "manual"
    wf = make_workflow_tools(coordinator_name=AGENT_NAME, run_id=tid, task_id=tid, tag="workflow", timeout=1800)
    ask = [t for t in wf if getattr(t, "name", "") == "ask_owner"]
    mem = make_memory_tools(AGENT_NAME)

    architect = Agent(
        role="AIMEAT Solutions Architect",
        goal=(
            "Turn a one-line app idea into a precise, AIMEAT-correct TECHNICAL SPEC the builder can "
            "implement right the first time, by interviewing the owner for the few decisions that shape "
            "the architecture and then applying the AIMEAT playbook."
        ),
        backstory=(
            "You are a seasoned solutions architect who knows AIMEAT deeply. You believe the build goes "
            "smoothly when the foundation is chosen correctly up front — which data layer, who reads and "
            "writes, how images flow, which verify gates apply. You ask the owner a few sharp questions, "
            "then translate their answers into a spec that names the exact memory keys, namespaces, "
            "visibility, auth model, and conventions. You write goals and positive guidance the builder "
            "can follow directly. You ground every external fact in what AIMEAT actually does, and when a "
            "choice needs server-side ownership or an external API you flag the extension (owner-installed) "
            "path clearly.\n\n" + PLAYBOOK
        ),
        tools=[*ask, *mem],
        llm=ctx.llm,
        max_iter=25,
        allow_delegation=False,
        verbose=True,
    )

    interview = Task(
        description=(
            f"{ctx.today}\n\n"
            "PHASE 1 — INTERVIEW the owner to pin the requirements for this app idea:\n\n"
            f"<<APP IDEA>>\n{ctx.prompt}\n<</APP IDEA>>\n\n"
            "Ask the owner the few questions whose answers change the architecture, using ask_owner "
            "(one question at a time, each with clear options; the owner can also type their own). Keep "
            "it to the essentials — aim for 3 to 5 questions. Cover at least:\n"
            "  1. WHO creates/writes the content — only you (the owner), any logged-in user, or anyone "
            "including anonymous visitors?\n"
            "  2. WHO reads it — anyone without logging in, only logged-in users, or only you?\n"
            "  3. Does it need PHOTOS or files (AIMEAT storage), or is it text only?\n"
            "  4. Does it need REAL-TIME / multiplayer (live updates between users)?\n"
            "  5. Is demo-grade fine for now, or does it need real per-user OWNERSHIP + server validation "
            "(which points to an owner-installed extension)?\n"
            "Ask any extra question the idea specifically raises (e.g. categories, a buy/contact flow). "
            "Carry the owner's answers forward verbatim into Phase 2."
        ),
        agent=architect,
        expected_output=(
            "A short structured summary of the owner's answers to the architecture questions (who writes, "
            "who reads, images, real-time, ownership/scale, plus any idea-specific choices)."
        ),
    )

    spec = Task(
        description=(
            "PHASE 2 — WRITE THE TECHNICAL SPEC by applying the AIMEAT playbook to the interview answers.\n\n"
            "Choose the DATA LAYER from the playbook decision list that matches the answers, and explain "
            "the choice in one positive sentence. Then produce a build-ready spec with these sections:\n"
            "  - TITLE + one-line summary.\n"
            "  - ARCHITECTURE: the chosen data layer + a one-line rationale; the app template to start from "
            "(public_viewer for anon-readable apps).\n"
            "  - DATA MODEL: the exact memory/micro-memory keys, their namespaces, visibility, and value "
            "shape; the discovery index (if any) and its PUBLISHER gaii.\n"
            "  - AUTH MODEL: what anonymous visitors do, what logged-in users do, what the owner does.\n"
            "  - IMAGES/FILES (if needed): the storage upload + display recipe with the right key prefix.\n"
            "  - CONVENTIONS: positive guidance for this app (render path, esc() user content, static inline "
            "JS, micro-memory size/rotation if used).\n"
            "  - BUILD CHECKLIST: an ordered list the builder can follow.\n"
            "  - VERIFY PLAN: which gates apply (verify_render / verify_anon_render / verify_interaction) and "
            "what each should assert.\n"
            "  - CONSTRAINTS/LIMITS to state honestly (e.g. demo-grade ownership, ~100-listing cap) and the "
            "v2 upgrade (extension) when relevant.\n\n"
            "Write the whole spec to the owner memory key `app.spec.<short-kebab-slug-of-the-idea>` with "
            "visibility 'public' using write_memory (so aimeat-app-builder / the conductor can read it), and "
            "ALSO return the full spec as your final answer. Report the exact spec key at the end."
        ),
        agent=architect,
        context=[interview],
        expected_output=(
            "The complete technical spec (architecture, data model, auth, images, conventions, build "
            "checklist, verify plan, honest constraints), plus the exact `app.spec.<slug>` key it was "
            "written to."
        ),
    )

    return [architect], [interview, spec]


def run() -> None:
    run_crew(
        CrewSpec(
            agent_name=AGENT_NAME,
            build_domain=build_domain,
            readme_md=README,
            temperature=0.3,  # architecture wants consistency
            poll_seconds=30,
        )
    )


if __name__ == "__main__":
    run()

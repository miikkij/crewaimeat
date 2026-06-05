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
from crewaimeat.author_tool import make_author_tools
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

DATA LAYER — ALWAYS use regular memory (AIMEAT.data: set/get/getPublic — per-identity namespaces, quotas,
versioning, schema validation) as the #1 store. Pick the layer that matches who writes + ownership/scale:
- OWNER-CURATED (only the owner — or one publisher identity — writes; anyone reads) — the common default:
  the publisher (logged in) writes items to their OWN GHII namespace via AIMEAT.data.set(..., {visibility:
  'public'}) and maintains ONE public index key; the viewer reads that index and fans out getPublic(gaii,key).
  Pure regular memory. Best for a shop/blog/noticeboard/dashboard one identity curates.
- MULTI-USER, LOGIN-GATED writes (any logged-in user contributes; anyone reads): each user writes their OWN
  items to their OWN public namespace via AIMEAT.data.set — real per-user ownership, regular memory. The
  cross-user DISCOVERY index is the one hard part: regular memory is per-writer-namespace (a user writes only
  their own keys), so MANY users CANNOT append to ONE shared index key with regular memory alone. Use a
  server-side EXTENSION (owner-installed) that owns ext:{name} and mediates the shared index (e.g. addListing
  validates + appends, browse returns all); the anon viewer reads ext:{name} public memory + getPublic items.
  Flag the extension as an owner-install step. If an extension is too heavy for v1, scope v1 to OWNER-CURATED
  (one publisher lists everything) and add per-user writes in v2. (Do NOT reach for micro-memory here — the
  writers are logged in, and an extension gives real ownership + validation.)
- SERVER-BACKED (real shared/multi-user state + server validation + external APIs + schedules): a server-side
  EXTENSION owns the ext:{name} namespace and exposes actions the data cortex calls. THE robust answer
  whenever many writers share one structure. Owner-installed (owner-gated) — flag it as an owner step / v2.
- ANONYMOUS-WRITE fallback (micro-memory) — NICHE, last resort, never a default: use ONLY when not-logged-in
  visitors must WRITE something simple and neither a login nor an extension is acceptable. It is a tokenless
  GET /v1/mm public_write set (read+write, no auth); anonymous uploaders use storage keys prefixed 'anonymous/'.
  IMPORTANT: this tokenless anonymous-write path needs the node's anonymous mode (AIMEAT_ANONYMOUS) ENABLED —
  it may be unavailable, so confirm it or require a login instead. Demo-grade only: ~100 keys/set, ≤1KB/value,
  no atomic increment. Regular memory stays #1; micro-memory is only for the anonymous-write case.

READ + RENDER for everyone (public apps): start from read_app_template('public_viewer') — startApp() runs
unconditionally so anonymous visitors render; read shown content with getPublic(gaii,key) (the one
anonymous read). Point PUBLISHER at the GAII that owns the index; carry each item's full {gaii,key} in the
index so bodies can live under many authors.

IMAGES / FILES (AIMEAT storage): upload with a token via POST /v1/storage {key, visibility:'public', data:
base64, mime_type}; store the returned key. An ANONYMOUS uploader (only when the node's anonymous mode is enabled) uses a key prefixed 'anonymous/'; a
logged-in user uses their own namespace. DISPLAY a PUBLIC image with a plain <img src="{base}/v1/pub/<gaii>/
<key>"> — the tokenless public route serves visibility:'public' files for direct <img>/links (no fetch/blob
needed). Reserve the fetch-with-token → blob → URL.createObjectURL(blob) → img.src path for OWNER-PRIVATE
files the logged-in owner reads from their OWN namespace; /v1/storage/<key> is scoped to the CALLER's gaii,
so a viewer's anon token cannot read another owner's file via /v1/storage — public display uses /v1/pub.

AUTH: AIMEAT.auth.login() returns the owner session or null for anonymous; gate write/admin UI on a real
session (session.ghii). Anonymous visitors read public content; logged-in users write their own namespace.

CONVENTIONS: use static inline JS (the app CSP supports inline scripts + the jsdelivr CDN for tailwind/
daisyui; it runs without eval). Escape every user-supplied value with esc() before the DOM. (Only if you use
the micro-memory anonymous-write fallback: values cap at ≤1KB and ~100 keys/set — rotate sets past that.)

NAMESPACING: memory keys are scoped to the WRITING identity's GAII. To read another identity's PUBLIC key
use getPublic(gaii,key). Regular memory gives a single-owner-published, many-readers index (perfect for
OWNER-CURATED); a many-writers-one-key shared index needs a server-side extension (or, for the anonymous-
write case only, a micro-memory public_write set). An agent writes its deliverable under its own GAII (so an
index it maintains is the PUBLISHER the viewer points at).

VERIFY: plan the gates the builder will run — verify_render (logged-in owner) for every app;
verify_anon_render (no login) for anything anonymous visitors must read; verify_interaction (drive the core
feature with real selectors) for anything interactive. The app is GREEN when the applicable gates PASS.
"""


def build_domain(ctx: BuildContext) -> tuple[list[Agent], list[Task]]:
    tid = (ctx.task or {}).get("id") or "manual"
    wf = make_workflow_tools(coordinator_name=AGENT_NAME, run_id=tid, task_id=tid, tag="workflow", timeout=1800)
    ask = [t for t in wf if getattr(t, "name", "") == "ask_owner"]
    mem = make_memory_tools(AGENT_NAME)
    # Read-only discovery tools so the architect can GROUND the spec in the LIVE node (current template/lib
    # surface + a real PUBLISHER gaii) rather than only the static PLAYBOOK. No install/publish power.
    author_tools, _ = make_author_tools(AGENT_NAME, task_id=tid)
    discovery = [t for t in author_tools if getattr(t, "name", "") in
                 ("read_app_template", "read_lib_api", "read_node_api", "find_public_index")]

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
        tools=[*ask, *mem, *discovery],
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
            "  - DATA MODEL: the exact memory keys (micro-memory keys only if you chose the anonymous-write fallback), their namespaces, visibility, and value "
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

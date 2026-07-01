"""Capability catalog for crew-forge — the machine-readable menu of tools a generated crew can attach.

This is the SINGLE source of truth for three things, so they can never drift apart:
  1. what the Crew Architect is told it may attach (its prompt is rendered from here),
  2. the PREFLIGHT that decides which capabilities are actually usable on this machine, and
  3. the forge-OWNED tool bindings emitted into the generated crew file.

Attachment model. Every entry here is an IN-CREW CrewAI tool — attached via `Agent(tools=[...])`
inside `build_domain`. crew-forge emits a `_tools(ctx)` helper into the generated file whose body is
assembled from the SELECTED capabilities; the Architect writes `T = _tools(ctx)` and passes e.g.
`tools=[*T["web"], *T["schedule"]]`. The Architect therefore never writes the error-prone factory
calls, tuple-unpacks, or tool-name filters — crew-forge owns those (see `emit_tools_function`). This
keeps the fragile wiring in one tested place instead of in every LLM-authored crew.

Preflight is deliberately conservative (minimal + preflight-gated): a capability whose ENVIRONMENT
prerequisite is missing (e.g. no OPENROUTER_API_KEY for image generation) is not offered to the
Architect at all, so it can never select a tool that would only fail at run time. Prerequisites that
cannot be checked before the agent is approved — the AIMEAT token SCOPES — are surfaced at
registration instead of gated here, and an `owner_action` (a manual node/owner step) is reported, not
silently assumed.

To add a capability: append a `Capability` whose factory EXISTS today and is safe to construct with no
blocking network call (construction runs during subprocess validation). Keep `expr`/`setup` matching
the factory's real shape — a list factory uses `[*make_x(AGENT_NAME)]`; a `(tools, state)` factory
unpacks in `setup`; a subset uses a name filter in `setup`.
"""

from __future__ import annotations

import importlib.util
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Capability:
    """One attachable in-crew tool capability.

    `expr` is the expression that produces the tool LIST stored under `id` in the `_tools(ctx)` dict.
    `setup` holds any statements that must run before the dict is built (tuple-unpacks, name filters);
    they may reference `tid` (the task id, always defined at the top of `_tools`). `imports` are the
    import lines `_tools` needs (deduped across selected capabilities).
    """

    id: str
    purpose: str
    when_to_use: str
    imports: tuple[str, ...]
    expr: str
    setup: tuple[str, ...] = ()
    # --- the preflight taxonomy: what gates HERE vs. what is surfaced later ---
    env_required: tuple[str, ...] = ()  # env vars that must be set → PREFLIGHT gate (checkable now)
    deps: tuple[str, ...] = ()  # importable packages the tool needs → PREFLIGHT gate (checkable now)
    scopes: tuple[str, ...] = ()  # AIMEAT token scopes → SURFACED at registration (unknown until approval)
    owner_action: str = ""  # a manual owner/node step → SURFACED, never silently assumed
    notes: str = ""


# The v1 catalog: capabilities whose factory exists today and is safe to construct during validation.
# (vision / file-fetch / clarify factories are extracted in a later slice; discover / dm / offer are
# CrewSpec-level and land when the template can emit CrewSpec fields.)
CATALOG: tuple[Capability, ...] = (
    Capability(
        id="web",
        purpose="search the web (SearXNG if reachable on this machine, else keyless DuckDuckGo)",
        when_to_use="the agent needs current or external facts it cannot get from the owner's memory",
        imports=("from crewaimeat.crew import _web_tools",),
        expr="_web_tools()",
    ),
    Capability(
        id="memory",
        purpose="read/write the owner's memory at EXACT keys with a chosen visibility (public or owner)",
        when_to_use=(
            "a content/data agent must persist its deliverable to a named key (e.g. a public key an app "
            "reads) or read a specific upstream key"
        ),
        imports=("from crewaimeat.memory_tools import make_memory_tools",),
        expr="[*make_memory_tools(AGENT_NAME)]",
        notes=(
            "Every crew already reads/writes memory through the daemon liaison; attach this only when a "
            "domain agent must target EXACT keys / set public visibility itself."
        ),
    ),
    Capability(
        id="schedule",
        purpose="create/list/update/delete AIMEAT node cron schedules (the node fires them offline, 0 tokens)",
        when_to_use="the agent must run itself or another agent on a recurring clock",
        imports=("from crewaimeat.scheduler import make_schedule_tools",),
        expr="[*make_schedule_tools(AGENT_NAME)]",
        scopes=("schedule",),
    ),
    Capability(
        id="delegate",
        purpose="discover peer crews and delegate a subtask to one, then wait for its result",
        when_to_use="the agent should hand part of the job to another specialist crew instead of doing it all",
        imports=("from crewaimeat.workflow import make_workflow_tools",),
        setup=(
            '_wf = make_workflow_tools(coordinator_name=AGENT_NAME, run_id=tid, task_id=tid, tag="workflow", timeout=1800)',
            '_deleg = [t for t in _wf if getattr(t, "name", "") in ("discover_crews", "delegate_and_wait")]',
        ),
        expr="_deleg",
        owner_action="Delegation works once the coordinator AND each worker crew share a Data-Access tag on the node.",
    ),
    Capability(
        id="image",
        purpose="generate an image from a text prompt (ByteDance Seedream 4.5) and get back its public URL",
        when_to_use="the deliverable includes a generated image",
        imports=("from crewaimeat.seedream_gen import make_image_tools as _make_image_gen_tools",),
        expr="[*_make_image_gen_tools(AGENT_NAME)]",
        env_required=("OPENROUTER_API_KEY",),
        notes="Costs ~$0.04/image; the agent must be registered and on the tunnel for public storage.",
    ),
    Capability(
        id="app_build",
        purpose="author, install, publish and verify a real AIMEAT app / cortex / extension (direct-build)",
        when_to_use="the deliverable is a working AIMEAT app or extension the user can open",
        imports=("from crewaimeat.author_tool import make_author_tools",),
        setup=("_author_tools, _author_state = make_author_tools(AGENT_NAME, task_id=tid)",),
        expr="[*_author_tools]",
        scopes=("generator",),
        owner_action="install_cortex / install_extension are owner-gated on the node until granted; publish_app works for agents.",
        notes="Start the app HTML from read_app_template() (correct auth/boot order) and end on a verify_render gate.",
    ),
)

_BY_ID: dict[str, Capability] = {c.id: c for c in CATALOG}


def get(cap_id: str) -> Capability | None:
    return _BY_ID.get(cap_id)


def _importable(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except Exception:  # noqa: BLE001 — a broken/partial install means "not usable"
        return False


def preflight(cap: Capability) -> tuple[bool, str]:
    """Is this capability usable on THIS machine right now? Only ENV + DEPENDENCY prerequisites gate here.

    Taxonomy: env vars and importable packages are checkable now, so a capability missing either is
    NOT offered to the Architect (it could only fail at run time). Token SCOPES are unknown until the
    owner approves the agent, so they are surfaced at registration instead of gated; a manual
    `owner_action` is likewise surfaced. Returns (ok, reason) — reason names what is missing.
    """
    missing_env = [e for e in cap.env_required if not os.getenv(e)]
    if missing_env:
        return False, "needs env " + ", ".join(missing_env)
    missing_dep = [d for d in cap.deps if not _importable(d)]
    if missing_dep:
        return False, "needs package " + ", ".join(missing_dep)
    return True, "available"


def available_capabilities() -> list[Capability]:
    """The capabilities offered to the Architect: every catalog entry whose env preflight passes."""
    return [c for c in CATALOG if preflight(c)[0]]


def parse_ids(raw: str | list[str] | None) -> list[str]:
    """Parse the Architect's CAPABILITIES section (comma/space/newline separated) into clean ids."""
    if not raw:
        return []
    parts = raw.replace(",", " ").replace("\n", " ").split() if isinstance(raw, str) else [str(p).strip() for p in raw]
    out: list[str] = []
    for p in parts:
        pid = p.strip().strip("\"'`").lower()
        if pid and pid not in out:
            out.append(pid)
    return out


def resolve(ids: str | list[str] | None) -> tuple[list[str], list[str]]:
    """Split requested ids into (usable, dropped). Usable = known AND env-available; dropped = the rest.

    Fail-loud: an unknown or env-unavailable id is DROPPED (never silently attached), and returned so
    crew-forge can report exactly what it did and did not wire.
    """
    usable, dropped = [], []
    for pid in parse_ids(ids):
        cap = _BY_ID.get(pid)
        if cap is not None and preflight(cap)[0]:
            if pid not in usable:
                usable.append(pid)
        else:
            dropped.append(pid)
    return usable, dropped


def required_scopes(ids: list[str]) -> list[str]:
    """Union of AIMEAT token scopes the selected capabilities need (for the registration checklist)."""
    scopes: list[str] = []
    for pid in ids:
        cap = _BY_ID.get(pid)
        if cap:
            for s in cap.scopes:
                if s not in scopes:
                    scopes.append(s)
    return scopes


def owner_actions(ids: list[str]) -> list[str]:
    """Manual owner/node steps the selected capabilities need (surfaced, never silently assumed)."""
    actions: list[str] = []
    for pid in ids:
        cap = _BY_ID.get(pid)
        if cap and cap.owner_action and cap.owner_action not in actions:
            actions.append(cap.owner_action)
    return actions


def registration_checklist(ids: str | list[str]) -> str:
    """The human-facing note for approval time: the token scopes to grant + any owner setup still
    needed for the selected capabilities. Empty when nothing extra is required. (Used by the
    register step so a scope gap becomes a guided one-time action instead of a silent runtime failure.)"""
    id_list = ids if isinstance(ids, list) else parse_ids(ids)
    scopes = required_scopes(id_list)
    actions = owner_actions(id_list)
    lines: list[str] = []
    if scopes:
        lines.append("When you approve this agent, grant these token scopes: " + ", ".join(scopes) + ".")
    if actions:
        lines.append("Owner setup still needed: " + " ".join(actions))
    return "\n".join(lines)


def capabilities_in_source(src: str) -> list[str]:
    """Read back which catalog capabilities a generated crew file actually wired, by inspecting its
    forge-emitted `_tools(ctx)` block. Returns [] for a tool-less crew. Used to grade generated crews."""
    if "def _tools(ctx):" not in src:
        return []
    return [c.id for c in CATALOG if f'"{c.id}":' in src]


# --------------------------------------------------------------------------- #
# Identity derivation — so a forged agent ships with a REAL identity (tags + capabilities), not the
# generic Hello-Integration defaults. Each capability contributes a charset-safe tag + (where it maps
# to a real skill) a technical capability; the crew's DOMAIN words add subject tags. The node's picker
# matches on tags + capabilities.technical + capabilities.domain, so this makes a forged agent
# discoverable by what it actually does.
# --------------------------------------------------------------------------- #
_IDENTITY: dict[str, tuple[str, str]] = {
    # capability id -> (tag, technical-skill name; "" skill = no distinct skill to advertise)
    "web": ("web-search", "web-research"),
    "memory": ("memory-io", ""),
    "schedule": ("scheduling", "scheduling"),
    "delegate": ("delegation", "delegation"),
    "image": ("image-generation", "image-generation"),
    "app_build": ("app-builder", "aimeat-appdev"),
}


def _sanitize_tag(s: str) -> str:
    """Coerce a word to the AIMEAT tag charset [a-z0-9._-] (a ':'/'@' would be rejected by the node)."""
    out = "".join(ch if (ch.isalnum() or ch in "._-") else "-" for ch in s.strip().lower())
    return out.strip("-._")


def derive_identity(ids: str | list[str], domain: str = "", languages: tuple[str, ...] = ("en",)):
    """Build (tags, capabilities) for a forged crew from its selected tool capabilities + DOMAIN words.

    tags = ["role.task-runner", <per-capability tags>, <domain words>]; capabilities =
    {technical:[{name,type:"skill"}], domain:[...], languages:[...]}. `technical.type` is always the
    node-valid "skill" (a bad type makes the node silently drop the whole report). Returns validated,
    de-duplicated lists so crew-forge can emit them verbatim.
    """
    usable, _dropped = resolve(ids)
    tags = ["role.task-runner"]
    technical: list[dict] = []
    for pid in usable:
        tag, skill = _IDENTITY.get(pid, ("", ""))
        t = _sanitize_tag(tag)
        if t and t not in tags:
            tags.append(t)
        if skill:
            technical.append({"name": skill, "type": "skill"})
    domain_tags = [d for d in (_sanitize_tag(w) for w in domain.replace(",", " ").split()) if d]
    for d in domain_tags:
        if d not in tags:
            tags.append(d)
    capabilities = {"technical": technical, "domain": domain_tags, "languages": list(languages)}
    return tags, capabilities


_COST_ENUM = ("free", "cheap", "expensive")
_LATENCY_ENUM = ("seconds", "minutes", "long-running")
# WHOLE WORDS that count as stating NEGATIVE SCOPE in an offer's ask (the hard offer rule). Matched as
# words, not substrings, so "notes"/"another" don't count as "not".
_NEG_SCOPE_WORDS = {"not", "never", "without", "no", "cannot", "avoid", "only", "beyond", "nor"}


def _has_negative_scope(ask: str) -> bool:
    low = ask.lower()
    if "n't" in low:  # don't / doesn't / won't / isn't
        return True
    words = {w.strip(".,;:!?\"'()") for w in low.split()}
    return bool(words & _NEG_SCOPE_WORDS)


def build_offer_meta(
    agent_name: str,
    ask: str,
    example: str = "",
    *,
    title: str = "",
    cost: str = "expensive",
    latency: str = "long-running",
) -> dict | None:
    """Assemble a valid crew_offer-shape offer META from the human parts the Architect provides.

    The Architect writes only the ask (what to send + what it returns + what it does NOT do) and an
    example; crew-forge fills the strict node enums with safe defaults so the LLM never has to get them
    right, guarantees the ask states negative scope (a hard offer rule — a soft clause is appended if
    the author omitted it), and derives a human title from the agent name. Returns None when there's no
    ask, so a crew simply advertises nothing. `sample` stays None ('untested') — never an invented sample.
    """
    ask = (ask or "").strip()
    if not ask:
        return None
    if not _has_negative_scope(ask):
        ask = ask.rstrip(".") + ". I do NOT fabricate facts or work beyond this scope."
    return {
        "id": _sanitize_tag(agent_name),
        "title": (title or agent_name.replace("-", " ").replace("_", " ").title()).strip(),
        "ask": ask,
        "example": (example or "").strip(),
        "cost": cost if cost in _COST_ENUM else "expensive",
        "latency": latency if latency in _LATENCY_ENUM else "long-running",
        "repeatability": "idempotent",
        "verification": "ungated",
        "consequences": [],
        "sample": None,
    }


def render_catalog_brief(caps: list[Capability] | None = None) -> str:
    """Render the tool menu for the Architect's prompt. crew-forge writes the wiring; the Architect
    only picks ids and references them as _tools(ctx)["<id>"]."""
    caps = caps if caps is not None else available_capabilities()
    lines = [
        "AVAILABLE TOOLS — attach ONLY what the job genuinely needs (fewer is better).",
        "crew-forge writes the tool wiring for you. In build_domain, first line:",
        "    T = _tools(ctx)",
        'then give each agent the lists it needs, e.g. tools=[*T["web"], *T["schedule"]].',
        'You never import a tool or call its factory — just reference T["<id>"] for each id you list.',
        "",
        "Tools:",
    ]
    for c in caps:
        extra = ""
        if c.env_required:
            extra += f"  [needs env {', '.join(c.env_required)}]"
        if c.notes:
            extra += f"  Note: {c.notes}"
        lines.append(f"  - {c.id}: {c.purpose}. Use when {c.when_to_use}.{extra}")
    lines += [
        "",
        "Then list the ids you actually used in a CAPABILITIES section (comma-separated), or leave it",
        "empty for a pure-reasoning crew with no tools.",
    ]
    return "\n".join(lines)


def emit_tools_function(ids: str | list[str] | None) -> tuple[str, list[str], list[str]]:
    """Build the `def _tools(ctx): ...` source for the selected capabilities.

    Returns (source, usable_ids, dropped_ids). `source` is "" when nothing usable is selected (the
    generated file then has no _tools helper, exactly like a legacy tool-less crew). Every factory
    call, tuple-unpack and name-filter is emitted HERE so the Architect's build_domain never has to.
    """
    usable, dropped = resolve(ids)
    if not usable:
        return "", usable, dropped
    caps = [_BY_ID[i] for i in usable]

    imports: list[str] = []
    for c in caps:
        for imp in c.imports:
            if imp not in imports:
                imports.append(imp)
    setup: list[str] = []
    for c in caps:
        setup.extend(c.setup)

    body = [
        "def _tools(ctx):",
        '    """Tool bindings for this crew, written by crew-forge (crewaimeat.forge_catalog).',
        '    Returns {capability_id: [tools]}; reference them in build_domain as _tools(ctx)["<id>"]."""',
        '    tid = (ctx.task or {}).get("id") or "manual"',
    ]
    body += ["    " + imp for imp in imports]
    body += ["    " + st for st in setup]
    body.append("    return {")
    body += [f'        "{c.id}": {c.expr},' for c in caps]
    body.append("    }")
    return "\n".join(body), usable, dropped

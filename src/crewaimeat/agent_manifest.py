"""The agent's own declaration — read from the crew file, statically, as ONE source of truth.

Until 2026-08-22 an agent's facts lived in four hand-kept places: what it can do
(``fleet_identity.py``), what it promises (``offers.py``), which model it runs on
(``llm_providers.json``), and whether it is tested (``tests/crew_fixtures.py``). Nothing required them
to agree and nothing required an entry at all — an agent came online missing from every one of them.
That is exactly what happened: 13 crews had no identity, 13 no offer, 20 no routing.

Now the crew file declares all of it and the lists are DERIVED. Forgetting a place is no longer
possible, because there is one place.

    AGENT_NAME = "datapkg-analyst"
    LLM_PROFILE = "coding"
    SCHEDULE = {"cron": "0 7 * * *", "timezone": "Europe/Helsinki"}   # what fires it, if anything
    TAGS = ["data-packages", "frictionless", "role.task-runner"]
    CAPABILITIES = {"technical": [...], "domain": [...], "languages": ["fi", "en"]}
    OFFERS = [{"id": "...", "title": "...", "ask": "...", ...}]
    SKILLS = ["frictionless-schema-reading"]

A JSON crew (``crew_defs/*.json``) already carried the same fields; both are normalised here into one
``Manifest``, so nothing downstream needs to know which kind of crew it is reading.

READ STATICALLY — via ``ast``, never by importing. Importing a crew pulls in crewai, litellm and every
contract module and runs any module-level side effect, which would make ``doctor``, the routing
resolver and the fleet host all pay a heavy, failure-prone cost for what is a handful of literals.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# A leading underscore parks a crew. THE one definition — forge._crew_files, the doctor and the test
# floor all resolve "is this crew live?" through here so they can never disagree.
PARKED_PREFIX = "_"

# --- Run mode -------------------------------------------------------------------------------- #
# HOW an agent is run, declared by the crew itself (RUN_MODE / "run_mode"). THE one vocabulary:
# crew_def's validator, the doctor rules, fleet_host and the spawner all resolve through here so they
# can never disagree about what a valid mode is.
#   resident — the agent is held up: a thread in fleet_host, parked in the tunnel itself. For anything
#              that must answer AT ONCE (a front door taking DMs and chat, an on_invoke agent) — a
#              spawned run pays ~4 s of cold start before the model is even called.
#   spawn    — the agent is DATA while idle. The spawner parks on its wake and starts ONE worker
#              PROCESS when work arrives; the process exits and every byte is returned. Memory then
#              scales with what is RUNNING, not with how many agents are defined.
# The names are the NODE's (`run_mode` on the agent record, spawn | resident) — it is the public
# surface, so two words for one thing is not worth maintaining. `continuous` is the name crewaimeat
# used first and is still accepted as an alias for `resident`.
# Undeclared means RESIDENT: adding this field must not change what any existing crew does.
RUN_SPAWN = "spawn"
RUN_RESIDENT = "resident"
RUN_CONTINUOUS = "continuous"  # accepted alias for RUN_RESIDENT
RUN_MODES = (RUN_RESIDENT, RUN_SPAWN)  # canonical — what doctor and the validator advertise
_RUN_ALIASES = {RUN_CONTINUOUS: RUN_RESIDENT}
RUN_MODES_ACCEPTED = (*RUN_MODES, *_RUN_ALIASES)


def agent_local_name(identity: str) -> str:
    """The agent NAME out of an identity that may be a full GAII (`<agent>#<owner>@<node>`).

    IDENTITY AND NAME PARTED WAYS when one connector home started serving more than one owner. The
    credential header must carry the GAII — a bare name that two owners share is REFUSED, naming
    both — but a memory key must not: `crews.registry.<agent>` lives in that agent's OWN namespace,
    which the token already decides, so putting the owner in the key too would file the definition
    somewhere nobody reads. Measured 2026-09-02: an unqualified key built from a GAII produced
    `crews.registry.crew-forge#isoalice@aimeat-iso-001-a`, and the node answered NOT_FOUND.

    A bare name is returned unchanged, so every existing caller is untouched.
    """
    return str(identity or "").split("#", 1)[0].strip()


def identity_owner(identity: str) -> str | None:
    """The OWNER out of a full identity (`name#owner@node`); None for a bare name, which names none."""
    return str(identity or "").partition("#")[2].partition("@")[0].strip() or None


def agent_file_stem(identity: str) -> str:
    """The file name for what the runtime KEEPS PER AGENT on this machine: a lock, a cache, a counter,
    a log.

    TWO KINDS OF FILE CARRY AN AGENT'S NAME, AND THEY ARE SPELLED DIFFERENTLY.

      * A file the connector or the repository already filed under the NAME — a credential
        (`<name>@<owner>`), a per-agent config, a crew file — is found with `agent_local_name`
        (credentials go through `crewaimeat.credentials`). Handing it an identity finds nothing.
      * A file this runtime creates for a running agent is named by THIS function, from the
        identity as handed. It keeps the owner and the node on purpose: one connector home serves
        several owners, two of them may have an agent of the same name, and their locks and caches
        must not be one file. It only makes the string a legal file name on every system: `#` and
        `@` become `_`, so `name#owner@node` is `name_owner_node` and a bare name is itself.

    Before this, each site chose for itself: the daemon lock substituted, the README cache and the
    onboarding marker wrote `#` and `@` into the name raw, and the spawner had its own copy
    (`spawn_state._safe`, which answers the same and is pinned to this by a test). Whoever cleans up
    after an agent (`crewaimeat retire --forget`) had to know all three spellings.
    """
    return "".join(c if (c.isalnum() or c in "._-") else "_" for c in str(identity or ""))[:120] or "unnamed"


def agent_key(identity: str, *parts: str) -> str:
    """`agents.<name>[.<part>...]` — a memory key in an agent's namespace, built from its NAME.

    THE ONE PLACE AN AGENT KEY IS SPELLED. An agent whose crew is defined on the node runs with its
    full identity as `spec.agent_name` (`name#owner@node`: the daemon needs it to pick the right
    agent when one home serves two owners). Every call site that wrote `f"agents.{agent_name}..."`
    then filed its record under a key nothing reads. Measured on production 2026-10-11 with the
    agent `tarjoama-testi`: its offer, its README, its evaluation context and its task's live
    record all landed under `agents.tarjoama-testi#happydude500001@aimeat-finland-001-genesis.*`,
    and `GET /v1/agents/tarjoama-testi/offers` answered an empty list. Forty-seven sites had the
    same shape and three had been fixed one at a time; tests/test_agent_addressing.py now refuses a
    new one.
    """
    return ".".join(["agents", agent_local_name(identity), *[str(p) for p in parts if p]])


def crew_key(identity: str, *parts: str) -> str:
    """`crews.<name>[.<part>...]` — where an agent's deliverables live, built from its NAME.

    The same rule as `agent_key`, for the other namespace an agent writes under. The key a run files
    its result at was already stripped (`_deliverable_key`); the places that READ or ADVERTISE that
    location were not — an offer told its buyers the deliverable was under `crews.name#owner@node.`.
    `crews.registry.<name>`, `crews.runtime.<name>` and `crews.llm.<name>` have their own one-line
    builders (`registry_key`, `runtime_key`, `llm_choice.key_for`), which strip the same way.
    """
    return ".".join(["crews", agent_local_name(identity), *[str(p) for p in parts if p]])


def agent_route(identity: str, suffix: str = "") -> str:
    """`/v1/agents/<name><suffix>` — a node route addressed by agent NAME.

    The same defect as `agent_key`, with a worse symptom: in a URL the `#` of an identity starts a
    FRAGMENT, so `PUT /v1/agents/name#owner@node/offers` leaves this machine as
    `PUT /v1/agents/name` and the node answers 404 for a route that exists. The identity belongs in
    the TRANSPORT argument (the first argument of `_aimeat_rest` / `_aimeat_call`), never in the path.
    """
    return f"/v1/agents/{agent_local_name(identity)}{suffix}"


# --- Agent mode ------------------------------------------------------------------------------ #
# The node's five agent modes. Which one an agent HAS is the owner's standing instruction on the node;
# the crew only states which one it EXPECTS, and registration asks for exactly that
# (`aimeat connect ... --mode <mode>`, connector 3.x), for the owner to approve in the same consent.
# task-runner is the default because it is what every crew here is: dm_serviceable and self_monitor
# crews are task-runners too (owner, 2026-07-26). A crew that genuinely needs a person to press Start
# declares MODE = "interactive".
AGENT_MODES = ("task-runner", "interactive", "coordinator", "autonomous", "workstation")
DEFAULT_AGENT_MODE = "task-runner"


def normalise_agent_mode(value: object) -> str | None:
    """The agent mode for `value`, or None when it is not one of the node's five."""
    v = str(value or "").strip().lower()
    return v if v in AGENT_MODES else None


def expected_mode(agent: str, root: Path | None = None) -> str:
    """The mode `agent` expects, read from its crew file. An agent with no crew file here (a node-backed
    or button-created one) expects the default."""
    m = manifest_for(agent_local_name(agent), root)
    return m.expected_mode if m else DEFAULT_AGENT_MODE


def normalise_run_mode(value: object) -> str | None:
    """The canonical run mode for `value`, or None when it is not a run mode at all.

    Returning None rather than a guess is the point: the caller decides whether an unrecognised
    value is a typo to shout about (doctor) or a field to fall back from (`effective_run_mode`).
    """
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    v = _RUN_ALIASES.get(v, v)
    return v if v in RUN_MODES else None


def normalise_run_timeout(value: object) -> int | None:
    """A declared run limit: whole seconds, 0 for no limit. None when the value is not a limit at all
    (a negative number, a string, True), so the caller decides what an unreadable declaration means."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


# Module-level constants a Python crew may declare, and the Manifest field each becomes.
_CONSTANTS = {
    "LLM_PROFILE": "llm_profile",
    "TAGS": "tags",
    "CAPABILITIES": "capabilities",
    "OFFERS": "offers",
    "SKILLS": "skills",
    "PROMPT_INDEPENDENT": "prompt_independent",
    "SCHEDULE": "schedule",
    "RUN_MODE": "run_mode",
    "MAX_CONCURRENT": "max_concurrent",
    "RUN_TIMEOUT_S": "run_timeout_s",
    "MODE": "mode",
}
# The same fields as a JSON crew doc names them.
_JSON_KEYS = {
    "llm_profile": "llm_profile",
    "tags": "tags",
    "capabilities": "capabilities",
    "offers": "offers",
    "skills": "skills",
    "schedule": "schedule",
    "run_mode": "run_mode",
    "max_concurrent": "max_concurrent",
    "mode": "mode",
}


@dataclass(frozen=True)
class Manifest:
    """Everything one agent declares about itself."""

    agent: str
    path: Path  # the crew file (the .py, even when the definition is JSON)
    kind: str  # "python" | "json"
    parked: bool
    llm_profile: str | None = None
    tags: list | None = None
    capabilities: dict | None = None
    offers: list | None = None
    skills: list | None = None
    prompt_independent: str | None = None
    schedule: dict | None = None
    run_mode: str | None = None  # "continuous" (thread in fleet_host) | "spawn" (process per wake).
    #   None = undeclared -> `effective_run_mode` reads CONTINUOUS, today's behaviour for all 49 crews.
    max_concurrent: int | None = None  # static value for CrewSpec.max_concurrent_tasks. 1 = single-flight;
    #   >1 = a bounded pool INSIDE one worker. None = ask the node at daemon start (today's behaviour).
    run_timeout_s: int | None = None  # how long ONE spawned run of this agent may take before the spawner
    #   ends it, in seconds; 0 = no limit. None = undeclared -> the spawner's own (SPAWN_RUN_TIMEOUT_S, an
    #   hour). The hour is the guard against a model stuck in a loop; an agent whose honest work is longer
    #   than that (a recording is as long as the programme) says so here instead of being cut off.
    mode: str | None = None  # the AIMEAT agent MODE this crew expects (MODE / "mode"); None = task-runner.
    record_spaces: bool | None = None  # True when the crew is woken by workspace records (a `record_spaces`
    #   with "records" in `listen_for`), False when it is not, None when this file cannot say: a crew
    #   whose definition lives on the node (CREW_DEF_SOURCE = "node"). The spawner subscribes a True one
    #   at every connector start without waiting for its first run (spawner.start_unsubscribed).
    has_build_domain: bool = False
    has_run: bool = False
    is_brain_stub: bool = False

    @property
    def live(self) -> bool:
        return not self.parked

    @property
    def effective_run_mode(self) -> str:
        """The mode this agent actually runs in — never None, so no caller has to guess.

        An undeclared or unrecognised value reads RESIDENT. That is deliberate: a typo must not
        silently move an agent to a different runtime. The doctor rule `runmode.unknown` is what
        makes the typo LOUD; this property is what keeps the fleet running while it is fixed.
        """
        return normalise_run_mode(self.run_mode) or RUN_RESIDENT

    @property
    def expected_mode(self) -> str:
        """The agent mode this crew expects: its declared MODE, else task-runner. Never None.

        An unknown value reads task-runner, like `effective_run_mode` reads resident: a typo must not
        register an agent in a mode nobody chose. `normalise_agent_mode` is what makes it loud.
        """
        return normalise_agent_mode(self.mode) or DEFAULT_AGENT_MODE

    def run_timeout(self, default: float) -> float | None:
        """Seconds one spawned run of this agent may take; None when it declares no limit.

        Undeclared, or declared as something that is not a limit, reads `default`: a typo must not
        take the guard off an agent. The doctor rule `runtimeout.invalid` is what makes the typo loud.
        """
        declared = normalise_run_timeout(self.run_timeout_s)
        if declared is None:
            return float(default)
        return None if declared == 0 else float(declared)

    @property
    def single_flight(self) -> bool:
        """True when this agent may run only ONE task at a time.

        `max_concurrent` is the SAME knob as CrewSpec.max_concurrent_tasks (the owner's decision: no
        second concurrency concept). None means the value is read from the node at daemon start, so
        it cannot be known statically — report False and let `concurrency.undeclared` say so.
        """
        return self.max_concurrent == 1


def _literals(tree: ast.Module) -> dict:
    """Module-level ``NAME = <literal>`` assignments, evaluated safely."""
    out: dict = {}
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        try:
            out[target.id] = ast.literal_eval(node.value)
        except (ValueError, SyntaxError, TypeError):
            continue  # a computed value is not a declaration
    return out


def _imported_module_path(tree: ast.Module, alias: str, root: Path) -> Path | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("crewaimeat"):
            for a in node.names:
                if (a.asname or a.name) == alias:
                    return root / "src" / "crewaimeat" / f"{a.name}.py"
        elif isinstance(node, ast.Import):
            for a in node.names:
                if (a.asname or a.name) == alias and a.name.startswith("crewaimeat."):
                    return root / "src" / "crewaimeat" / f"{a.name.split('.', 1)[1]}.py"
    return None


def resolve_agent_name(tree: ast.Module, literals: dict, root: Path) -> str | None:
    """AGENT_NAME as a literal, or through ONE hop into an imported module constant.

    Six M-ROOM crews write ``AGENT_NAME = mr.ARCHIVIST``. A literal-only reader sees "no agent name"
    and the fleet then keys those crews by FILENAME — which is why ``logs/.host_status.json`` listed
    ``mroom_archivist_crew`` beside ``mroom-curator``. Resolving the hop makes every consumer agree
    on one identity per crew.
    """
    if isinstance(literals.get("AGENT_NAME"), str):
        return literals["AGENT_NAME"]
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        t, v = node.targets[0], node.value
        if not (isinstance(t, ast.Name) and t.id == "AGENT_NAME" and isinstance(v, ast.Attribute)):
            continue
        base = v.value
        if not isinstance(base, ast.Name):
            continue
        mod = _imported_module_path(tree, base.id, root)
        if not mod or not mod.exists():
            continue
        try:
            sub = ast.parse(mod.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        value = _literals(sub).get(v.attr)
        if isinstance(value, str):
            return value
    return None


def _json_doc_path(literals: dict, tree: ast.Module, root: Path) -> Path | None:
    """A declarative crew points at its JSON with ``_DOC_PATH = ... / "crew_defs" / "x.json"``.

    That is a computed Path expression, not a literal, so it is read from the file NAME in the
    expression rather than evaluated — evaluating arbitrary path arithmetic from a source file is a
    cost and a risk this does not need.
    """
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        t = node.targets[0]
        if not (isinstance(t, ast.Name) and t.id == "_DOC_PATH"):
            continue
        names = [n.value for n in ast.walk(node.value) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        stem = next((n for n in names if n.endswith(".json")), None)
        if stem:
            return root / "crew_defs" / stem
    return None


def doc_declares_records(doc) -> bool:
    """A crew definition (a crew_defs/*.json document or the node's `crews.registry.<agent>`) that is
    woken by workspace records: a non-empty `record_spaces` and "records" in `listen_for`, the pair
    crew_def.validate_crew_doc requires together."""
    if not isinstance(doc, dict):
        return False
    spaces, listen = doc.get("record_spaces"), doc.get("listen_for")
    return isinstance(spaces, list) and bool(spaces) and isinstance(listen, list) and "records" in listen


def _crewspec_declares_records(tree: ast.AST) -> bool:
    """A Python crew whose CrewSpec(...) passes `record_spaces=` (not None) and lists "records" in
    `listen_for`. Read from the call's keywords, statically: the value itself is usually a callable
    (contract_record_spaces) that only a run resolves."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else ""
        if name != "CrewSpec":
            continue
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        spaces = kw.get("record_spaces")
        if spaces is None or (isinstance(spaces, ast.Constant) and spaces.value is None):
            continue
        listen = kw.get("listen_for")
        if listen is not None and any(isinstance(n, ast.Constant) and n.value == "records" for n in ast.walk(listen)):
            return True
    return False


def read(path: Path, root: Path) -> Manifest | None:
    """One crew file -> its Manifest. None when the file cannot be parsed at all."""
    try:
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
    except (OSError, SyntaxError):
        return None
    literals = _literals(tree)
    agent = resolve_agent_name(tree, literals, root)
    funcs = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    fields: dict = {}

    doc_path = _json_doc_path(literals, tree, root)
    kind = "python"
    # A NODE-BACKED crew declares nothing about itself here on purpose: its whole definition lives at
    # `crews.registry.<agent>` and is edited in AIMEAT. The loader says so with one constant, because
    # this reader is STATIC (ast, no imports, no network — it has to run in CI where there is no node)
    # and would otherwise report a perfectly healthy agent as having no identity, no offer and no
    # routing. What we cannot see, we name; we do not accuse.
    if str(literals.get("CREW_DEF_SOURCE") or "").strip().lower() == "node":
        kind = "node"  # record_spaces stays None: only the definition on the node can say
    elif doc_path and doc_path.exists():
        kind = "json"
        try:
            doc = json.loads(doc_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            doc = {}
        agent = doc.get("agent_name") or agent
        for json_key, field in _JSON_KEYS.items():
            if doc.get(json_key) is not None:
                fields[field] = doc[json_key]
        fields["record_spaces"] = doc_declares_records(doc)
    else:
        for const, field in _CONSTANTS.items():
            if const in literals:
                fields[field] = literals[const]
        fields["record_spaces"] = _crewspec_declares_records(tree)

    return Manifest(
        agent=agent or "",
        path=path,
        kind=kind,
        parked=path.name.startswith(PARKED_PREFIX),
        has_build_domain="build_domain" in funcs,
        has_run="run" in funcs,
        is_brain_stub="run_brain" in text and "build_domain" not in funcs,
        **fields,
    )


def crews_dir(root: Path | None = None) -> Path:
    return (root or Path.cwd()) / "crews"


@lru_cache(maxsize=8)
def _load(root_str: str) -> tuple[Manifest, ...]:
    root = Path(root_str)
    out = []
    for p in sorted(crews_dir(root).glob("*_crew.py")):
        m = read(p, root)
        if m is not None:
            out.append(m)
    return tuple(out)


def all_manifests(root: Path | None = None, *, refresh: bool = False) -> list[Manifest]:
    """Every crew file's manifest, parked ones included. Cached — the fleet host, the routing
    resolver and doctor all ask repeatedly and the answer only changes when a file does."""
    root = (root or Path.cwd()).resolve()
    if refresh:
        _load.cache_clear()
    return list(_load(str(root)))


def by_agent(root: Path | None = None, *, refresh: bool = False) -> dict[str, Manifest]:
    """agent name -> manifest. A crew with no resolvable AGENT_NAME is left out (doctor reports it)."""
    return {m.agent: m for m in all_manifests(root, refresh=refresh) if m.agent}


def manifest_for(agent: str, root: Path | None = None) -> Manifest | None:
    return by_agent(root).get(agent)


def live_agents(root: Path | None = None) -> set[str]:
    return {m.agent for m in all_manifests(root) if m.agent and m.live}

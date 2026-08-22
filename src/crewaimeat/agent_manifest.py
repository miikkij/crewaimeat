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

# Module-level constants a Python crew may declare, and the Manifest field each becomes.
_CONSTANTS = {
    "LLM_PROFILE": "llm_profile",
    "TAGS": "tags",
    "CAPABILITIES": "capabilities",
    "OFFERS": "offers",
    "SKILLS": "skills",
    "PROMPT_INDEPENDENT": "prompt_independent",
    "SCHEDULE": "schedule",
}
# The same fields as a JSON crew doc names them.
_JSON_KEYS = {
    "llm_profile": "llm_profile",
    "tags": "tags",
    "capabilities": "capabilities",
    "offers": "offers",
    "skills": "skills",
    "schedule": "schedule",
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
    has_build_domain: bool = False
    has_run: bool = False
    is_brain_stub: bool = False

    @property
    def live(self) -> bool:
        return not self.parked


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
    if doc_path and doc_path.exists():
        kind = "json"
        try:
            doc = json.loads(doc_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            doc = {}
        agent = doc.get("agent_name") or agent
        for json_key, field in _JSON_KEYS.items():
            if doc.get(json_key) is not None:
                fields[field] = doc[json_key]
    else:
        for const, field in _CONSTANTS.items():
            if const in literals:
                fields[field] = literals[const]

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

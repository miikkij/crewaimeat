"""The facts every lens reconciles against — gathered ONCE, from disk, without importing crew code.

Importing a crew module runs its imports (crewai, litellm, every contract module) and any module-level
side effect. `doctor` must be safe to run from a pre-commit hook on a broken tree, so everything here
reads SOURCE via ast/regex. The one exception is the two registry dicts (`fleet_identity`,
`offers`), which are plain data in the installed package and are imported directly — reconciling
against a regex of a dict literal would be its own drift.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# A leading underscore parks a crew. THE SAME rule as crewaimeat.forge._crew_files and
# tests/crew_fixtures.py — stated once here so all three can never disagree about what is live.
PARKED_PREFIX = "_"


@dataclass
class Crew:
    stem: str  # module name, e.g. "joker_crew"
    path: Path
    agent: str | None  # AGENT_NAME, resolved through a one-hop module constant when needed
    parked: bool
    has_build_domain: bool
    has_run: bool
    is_brain_stub: bool
    declares: dict[str, str] = field(default_factory=dict)  # module-level string constants of interest


@dataclass
class Inventory:
    root: Path
    crews: list[Crew]
    served: dict[str, dict]  # agent -> serve.json entry (token/owner/url), tokens NOT read
    routing: dict  # parsed llm_providers.json ({} when absent)
    identity: dict  # fleet_identity.FLEET_IDENTITY
    offer_agents: set[str]  # agents with a central offers.py entry
    connector_pin: str | None  # forge.AIMEAT_CONNECTOR, e.g. "aimeat@3.5.0"
    connector_floor: str | None

    @property
    def live(self) -> list[Crew]:
        return [c for c in self.crews if not c.parked]

    @property
    def live_agents(self) -> set[str]:
        return {c.agent for c in self.live if c.agent}

    @property
    def parked_agents(self) -> set[str]:
        return {c.agent for c in self.crews if c.parked and c.agent}

    def crew_of(self, agent: str) -> Crew | None:
        return next((c for c in self.crews if c.agent == agent), None)


_STR_CONST = re.compile(r'^([A-Z][A-Z0-9_]*)\s*=\s*(?:\(\s*)?["\']', re.M)


def _module_level_strings(tree: ast.Module, src: str) -> dict[str, str]:
    """Module-level `NAME = "..."` (including implicit-concat parenthesised strings), as text."""
    out: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, SyntaxError, TypeError):
            continue
        if isinstance(value, str):
            out[target.id] = value
    return out


def _resolve_agent_name(tree: ast.Module, consts: dict[str, str], root: Path) -> str | None:
    """AGENT_NAME as a literal, or one hop through an imported module constant.

    Six M-ROOM crews write `AGENT_NAME = mr.ARCHIVIST`, which a literal-only regex reads as "no agent
    name" — the fleet host then keys their status by FILENAME instead, which is why `logs/.host_status
    .json` shows `mroom_archivist_crew` beside `mroom-curator`. Resolving the one hop makes every
    reconciliation below agree on one identity per crew.
    """
    if "AGENT_NAME" in consts:
        return consts["AGENT_NAME"]
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1):
            continue
        t, v = node.targets[0], node.value
        if not (isinstance(t, ast.Name) and t.id == "AGENT_NAME" and isinstance(v, ast.Attribute)):
            continue
        # `mr.ARCHIVIST` -> find what `mr` is bound to, then read that constant from its source.
        base = v.value
        if not isinstance(base, ast.Name):
            continue
        mod_path = _imported_module_path(tree, base.id, root)
        if not mod_path or not mod_path.exists():
            continue
        try:
            sub = ast.parse(mod_path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        return _module_level_strings(sub, "").get(v.attr)
    return None


def _imported_module_path(tree: ast.Module, alias: str, root: Path) -> Path | None:
    """Map a local alias (`mr`) back to a crewaimeat module file."""
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


def _read_crew(path: Path, root: Path) -> Crew:
    src = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError:
        # A crew that does not parse is itself a finding; report it as an unnamed crew rather than
        # crashing the whole run (doctor must survive a broken tree — that is when it is needed most).
        return Crew(path.stem, path, None, path.name.startswith(PARKED_PREFIX), False, False, False)
    consts = _module_level_strings(tree, src)
    funcs = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    return Crew(
        stem=path.stem,
        path=path,
        agent=_resolve_agent_name(tree, consts, root),
        parked=path.name.startswith(PARKED_PREFIX),
        has_build_domain="build_domain" in funcs,
        has_run="run" in funcs,
        is_brain_stub="run_brain" in src and "build_domain" not in funcs,
        declares=consts,
    )


def _read_serve(root: Path) -> dict[str, dict]:
    """serve.json's agent list, WITHOUT tokens.

    THE ROOT WINS when it has its own `.aimeat/`. The connector home is per-repo and every entrypoint
    pins `AIMEAT_HOME=<repo>/.aimeat`, so the file belonging to the tree being examined is the right
    answer — and resolving through the ambient env instead would make `doctor --root <other-checkout>`
    silently report THIS machine's fleet, which is the same class of "measured the wrong thing" bug
    that kept three TUI tests red for two months. Fall back to the home rule only when the tree has no
    home of its own.
    """
    p = root / ".aimeat" / "serve.json"
    if not p.exists():
        try:
            from crewaimeat._home import aimeat_home

            p = Path(aimeat_home()) / "serve.json"
        except Exception:  # noqa: BLE001 — doctor must work even when the package import path is odd
            return {}
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for entry in data.get("agents") or []:
        name = entry.get("agent")
        if name:
            out[name] = {k: v for k, v in entry.items() if k != "token"}
    return out


def _read_routing(root: Path) -> dict:
    import os

    p = Path(os.getenv("LLM_PROVIDERS_FILE") or (root / "llm_providers.json"))
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def gather(root: Path) -> Inventory:
    crews = sorted(
        (_read_crew(p, root) for p in (root / "crews").glob("*_crew.py")),
        key=lambda c: c.stem,
    )
    try:
        from crewaimeat.fleet_identity import FLEET_IDENTITY

        identity = dict(FLEET_IDENTITY)
    except Exception:  # noqa: BLE001
        identity = {}
    try:
        from crewaimeat.offers import CREW_AGENTS, PILOT_AGENTS

        offer_agents = set(CREW_AGENTS) | set(PILOT_AGENTS)
    except Exception:  # noqa: BLE001
        offer_agents = set()
    try:
        from crewaimeat.forge import AIMEAT_CONNECTOR, AIMEAT_CONNECTOR_FLOOR

        pin, floor = AIMEAT_CONNECTOR, AIMEAT_CONNECTOR_FLOOR
    except Exception:  # noqa: BLE001
        pin, floor = None, None
    return Inventory(
        root=root,
        crews=crews,
        served=_read_serve(root),
        routing=_read_routing(root),
        identity=identity,
        offer_agents=offer_agents,
        connector_pin=pin,
        connector_floor=floor,
    )

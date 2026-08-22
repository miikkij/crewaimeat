"""The facts every lens reconciles against — gathered ONCE, without importing crew code.

Importing a crew module runs its imports (crewai, litellm, every contract module) and any module-level
side effect. `doctor` must be safe to run from a pre-commit hook on a broken tree, so the crew files
are read STATICALLY — through `crewaimeat.agent_manifest`, which is the same reader the routing
resolver and `identity_for` use. That shared reader is deliberate: an earlier draft of this file had
its own copy of the AGENT_NAME resolution, which is precisely the duplication the audit was about.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from crewaimeat.agent_manifest import PARKED_PREFIX, Manifest, all_manifests

__all__ = ["Inventory", "Manifest", "PARKED_PREFIX", "gather"]


@dataclass
class Inventory:
    root: Path
    crews: list[Manifest]  # every crew file's own declaration, parked ones included
    served: dict[str, dict]  # agent -> serve.json entry (token NEVER read)
    routing: dict  # parsed llm_providers.json ({} when absent)
    connector_pin: str | None  # forge.AIMEAT_CONNECTOR, e.g. "aimeat@3.5.0"
    connector_floor: str | None
    fallback_identity: dict  # fleet_identity.FLEET_IDENTITY — the library fallback, normally empty

    @property
    def live(self) -> list[Manifest]:
        return [c for c in self.crews if c.live]

    @property
    def live_agents(self) -> set[str]:
        return {c.agent for c in self.live if c.agent}

    @property
    def parked_agents(self) -> set[str]:
        return {c.agent for c in self.crews if c.parked and c.agent}

    def crew_of(self, agent: str) -> Manifest | None:
        return next((c for c in self.crews if c.agent == agent), None)

    def declares_identity(self, agent: str) -> bool:
        c = self.crew_of(agent)
        return bool(c and (c.tags is not None or c.capabilities is not None)) or agent in self.fallback_identity

    def declares_offer(self, agent: str) -> bool:
        c = self.crew_of(agent)
        return bool(c and c.offers)

    def declared_profile(self, agent: str) -> str | None:
        c = self.crew_of(agent)
        return c.llm_profile if c else None


def _read_serve(root: Path) -> dict[str, dict]:
    """serve.json's agent list, WITHOUT tokens.

    THE ROOT WINS when it has its own `.aimeat/`. The connector home is per-repo and every entrypoint
    pins `AIMEAT_HOME=<repo>/.aimeat`, so the file belonging to the tree being examined is the right
    answer — resolving through the ambient env instead would make `doctor --root <other-checkout>`
    silently report THIS machine's fleet.
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
    p = Path(os.getenv("LLM_PROVIDERS_FILE") or (root / "llm_providers.json"))
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def gather(root: Path) -> Inventory:
    try:
        from crewaimeat.fleet_identity import FLEET_IDENTITY

        fallback = dict(FLEET_IDENTITY)
    except Exception:  # noqa: BLE001
        fallback = {}
    try:
        from crewaimeat.forge import AIMEAT_CONNECTOR, AIMEAT_CONNECTOR_FLOOR

        pin, floor = AIMEAT_CONNECTOR, AIMEAT_CONNECTOR_FLOOR
    except Exception:  # noqa: BLE001
        pin, floor = None, None
    return Inventory(
        root=root,
        crews=all_manifests(root, refresh=True),
        served=_read_serve(root),
        routing=_read_routing(root),
        connector_pin=pin,
        connector_floor=floor,
        fallback_identity=fallback,
    )

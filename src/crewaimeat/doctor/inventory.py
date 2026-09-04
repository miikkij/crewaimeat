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
    spare: set[str]  # registered here, but its runtime is not a crew file (chat clients, probes)
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
    def node_backed(self) -> set[str]:
        """Agents whose whole definition lives at `crews.registry.<agent>`, not in this repo.

        Doctor is static and offline by design (it runs in CI, where there is no node), so for these
        it can see the NAME and nothing else. The three declaration checks below therefore step
        around them and the report says so — a check that accuses an agent of missing what doctor
        merely cannot see teaches the reader to ignore it.
        """
        return {c.agent for c in self.live if c.agent and c.kind == "node"}

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


def _read_spare(root: Path) -> set[str]:
    """Agents registered here whose runtime is somebody else's program, not a crew file.

    A chat client (Goose, Claude Desktop, VS Code) registers exactly like a crew and has no file in
    this checkout, so `registry.serve.ghost` — which exists to catch a crew whose file VANISHED —
    shouts about it. They became visible all at once on 2026-09-04, when the connector moved to keys
    and the daemon began carrying all 66 principals instead of the 50 it could authenticate.

    A list rather than baseline entries: the baseline may only shrink, and the next chat client will
    arrive the same way. `crewaimeat orphans --exclude` already uses this shape.
    """
    p = Path(os.getenv("SERVE_SPARE_AGENTS_FILE") or (root / "serve-spare-agents.json"))
    if not p.exists():
        return set()
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {str(a) for a in (doc.get("spare") or []) if a}


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
        spare=_read_spare(root),
        routing=_read_routing(root),
        connector_pin=pin,
        connector_floor=floor,
        fallback_identity=fallback,
    )

"""Lens 1 — RECONCILIATION. Six registries must agree about which agents exist.

An agent is "real" in six places and every one of them is maintained by hand: the crew file, the
identity registry, the offers registry, the routing map, serve.json, and the node. Nothing required
them to agree, so on 2026-08-22 they did not: 13 crews had no identity, 13 no offer, 20 no routing
(silently falling to the free meta-router), 12 registered agents had no crew file at all, and one crew
ran unregistered. Every one of those is a set difference — which is a program, not an afternoon.

Severity rule of thumb: ERROR when the divergence changes what the fleet DOES (a ghost holds a tunnel,
an unregistered crew idles forever, a malformed capability is unmatchable); WARN when it degrades how
the fleet is FOUND or understood (a missing offer, a missing identity).
"""

from __future__ import annotations

import ast
import re

from .inventory import Inventory
from .model import ERROR, WARN, Finding, Report

_SEMVER = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def _ver(text: str | None) -> tuple[int, int, int] | None:
    m = _SEMVER.search(text or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def check(inv: Inventory, report: Report) -> None:
    _crews_vs_serve(inv, report)
    _crews_vs_identity(inv, report)
    _crews_vs_offers(inv, report)
    _crews_vs_routing(inv, report)
    _crew_shape(inv, report)
    _connector(inv, report)
    _skills_exist(inv, report)
    report.note(
        f"crews: {len(inv.live)} live, {len(inv.crews) - len(inv.live)} parked · "
        f"serve.json: {len(inv.served)} registered · identity: {len(inv.identity)} · "
        f"routing: {len((inv.routing or {}).get('crews') or {})} mapped"
    )


def _crews_vs_serve(inv: Inventory, report: Report) -> None:
    served = set(inv.served)
    for agent in sorted(inv.live_agents - served):
        report.add(
            Finding(
                "registry.serve.unregistered",
                ERROR,
                agent,
                "live crew is not registered in serve.json — it has no token, so its task poll returns "
                "an empty list forever with no error and the agent idles silently",
                f"npx {inv.connector_pin or 'aimeat@<pinned>'} connect --url https://aimeat.io "
                f"--owner <owner> --agent {agent}, then restart the fleet",
            )
        )
    known = inv.live_agents | inv.parked_agents
    for agent in sorted(served - known):
        report.add(
            Finding(
                "registry.serve.ghost",
                ERROR,
                agent,
                "registered in serve.json but there is no crew file — it still opens a tunnel and "
                "receives wake events on every fleet start, with nothing to consume them",
                f"crewaimeat retire {agent}",
            )
        )
    for agent in sorted(served & inv.parked_agents):
        report.add(
            Finding(
                "registry.serve.parked",
                WARN,
                agent,
                "crew is parked (leading underscore) but still registered — the fleet skips it while "
                "the node still holds its registration",
                f"crewaimeat retire {agent}, or unpark the crew file",
            )
        )
    owners = {e.get("owner") for e in inv.served.values() if e.get("owner")}
    if len(owners) > 1:
        majority = max(owners, key=lambda o: sum(1 for e in inv.served.values() if e.get("owner") == o))
        for agent, entry in sorted(inv.served.items()):
            if entry.get("owner") and entry["owner"] != majority:
                report.add(
                    Finding(
                        "registry.serve.foreign_owner",
                        WARN,
                        agent,
                        f"registered under owner '{entry['owner']}' while the rest of the fleet runs as "
                        f"'{majority}' — this process acts on the node as a different account",
                        "re-register under the fleet owner, or retire it if it was an experiment",
                    )
                )


def _crews_vs_identity(inv: Inventory, report: Report) -> None:
    from crewaimeat.aimeat_crew import _validate_capabilities

    for agent in sorted(inv.live_agents):
        crew = inv.crew_of(agent)
        # A crew may declare its identity inline (CrewSpec.tags/.capabilities), which overrides the
        # registry — read the source for that before calling it missing.
        inline = crew is not None and (
            "_CAPABILITIES" in crew.declares or "capabilities=" in crew.path.read_text(encoding="utf-8")
        )
        if agent not in inv.identity and not inline:
            report.add(
                Finding(
                    "registry.identity.missing",
                    WARN,
                    agent,
                    "no entry in fleet_identity and none declared inline — the agent advertises the "
                    "liaison's generic onboarding defaults, so discovery cannot match what it does",
                    "add tags + capabilities to src/crewaimeat/fleet_identity.py",
                )
            )
    for agent, ident in sorted(inv.identity.items()):
        caps = ident.get("capabilities")
        if not caps:
            continue
        payload = {k: caps[k] for k in ("technical", "domain", "languages") if caps.get(k)}
        try:
            _validate_capabilities(agent, payload)
        except ValueError as exc:
            first = str(exc).splitlines()[1].strip(" -") if len(str(exc).splitlines()) > 1 else str(exc)
            report.add(
                Finding(
                    "registry.identity.malformed",
                    ERROR,
                    agent,
                    f"capabilities payload is the wrong shape: {first}",
                    "technical entries are {name, type} objects; free phrases belong in domain",
                )
            )
    for agent in sorted(set(inv.identity) - inv.live_agents - inv.parked_agents):
        report.add(
            Finding(
                "registry.identity.orphan",
                WARN,
                agent,
                "fleet_identity holds an entry for an agent with no crew file",
                "remove the entry, or restore the crew",
            )
        )


def _crews_vs_offers(inv: Inventory, report: Report) -> None:
    for agent in sorted(inv.live_agents - inv.offer_agents):
        crew = inv.crew_of(agent)
        if crew and ("_OFFER" in crew.declares or "offer=" in crew.path.read_text(encoding="utf-8")):
            continue  # inline offer on the CrewSpec — the forged-crew path
        report.add(
            Finding(
                "registry.offer.missing",
                WARN,
                agent,
                "no offer — the agent does not advertise what it can do, so it is invisible on the "
                "Tarjoama surface and to any agent shopping for a capability",
                "add an entry to src/crewaimeat/offers.py, or CrewSpec(offer=...)",
            )
        )


def _crews_vs_routing(inv: Inventory, report: Report) -> None:
    # `_`-prefixed keys are annotations for the human reading the file, not agents.
    crews_map = {k: v for k, v in ((inv.routing or {}).get("crews") or {}).items() if not k.startswith("_")}
    default = (inv.routing or {}).get("default")
    if not inv.routing:
        report.add(
            Finding(
                "registry.routing.absent",
                WARN,
                "llm_providers.json",
                "no routing file found — every crew resolves through the env fallback chain",
                "create llm_providers.json (see llm_providers.example.json)",
            )
        )
        return
    for agent in sorted(inv.live_agents - set(crews_map)):
        report.add(
            Finding(
                "registry.routing.unmapped",
                WARN,
                agent,
                f"not in the routing map — it silently resolves to the '{default}' profile. A default "
                f"is fine; an UNDECIDED default is how 20 crews ended up on a free meta-router that "
                f"picks a different model per call",
                f'add "{agent}": "<profile>" to llm_providers.json crews (write the default '
                f"explicitly if that is the decision)",
            )
        )
    for agent in sorted(set(crews_map) - inv.live_agents):
        report.add(
            Finding(
                "registry.routing.orphan",
                WARN,
                agent,
                "routing entry for an agent that is parked or has no crew file",
                "remove the entry from llm_providers.json",
            )
        )
    profiles = set(inv.routing.get("profiles") or {})
    for agent, profile in sorted(crews_map.items()):
        if profile not in profiles:
            report.add(
                Finding(
                    "registry.routing.unknown_profile",
                    ERROR,
                    agent,
                    f"routed to profile '{profile}', which is not defined in llm_providers.json",
                    f"define the profile or route to one of {sorted(profiles)}",
                )
            )
    _profile_notes_match_order(inv, report)
    if default and default not in profiles:
        report.add(
            Finding(
                "registry.routing.unknown_profile",
                ERROR,
                "default",
                f"the default profile '{default}' is not defined — every unmapped crew resolves to nothing",
                "define it in profiles, or point default at an existing profile",
            )
        )


def _crew_shape(inv: Inventory, report: Report) -> None:
    for crew in inv.crews:
        where = crew.path.as_posix()
        if crew.agent is None:
            report.add(
                Finding(
                    "crew.agent_name.missing",
                    ERROR,
                    where,
                    "AGENT_NAME is missing or not resolvable — the fleet falls back to keying this crew "
                    "by FILENAME, so its status, logs and registration disagree about who it is",
                    'set AGENT_NAME = "<agent>" (or a one-hop module constant)',
                )
            )
        if not crew.has_run and not crew.parked:
            report.add(
                Finding(
                    "crew.run.missing",
                    ERROR,
                    where,
                    "no run() — the fleet host cannot start this crew, so the file is present but dead",
                    "add run() calling run_crew(CrewSpec(...)), or park the file with a leading underscore",
                )
            )
        if not crew.has_build_domain and not crew.is_brain_stub and not crew.parked:
            report.add(
                Finding(
                    "crew.build_domain.missing",
                    ERROR,
                    where,
                    "no build_domain and no run_brain — this crew builds nothing",
                    "add build_domain(ctx), or make it a brain stub",
                )
            )


def _connector(inv: Inventory, report: Report) -> None:
    """The connector version must be one pin, at or above the documented floor.

    The floor is not cosmetic: below it the node's provenance block is dropped SILENTLY in both
    directions, so a declaration of human authorship disappears with no error.
    """
    pin, floor = inv.connector_pin, inv.connector_floor
    if not pin:
        report.add(
            Finding(
                "connector.pin.absent",
                ERROR,
                "forge.AIMEAT_CONNECTOR",
                "no connector pin found — registration would run an unpinned connector",
                'set AIMEAT_CONNECTOR = "aimeat@<x.y.z>" in src/crewaimeat/forge.py',
            )
        )
        return
    pv, fv = _ver(pin), _ver(floor)
    if pv and fv and pv < fv:
        report.add(
            Finding(
                "connector.pin.below_floor",
                ERROR,
                pin,
                f"the registration pin is below the documented floor {floor} — an agent registered "
                f"through it cannot carry provenance, and the loss is silent",
                f"raise AIMEAT_CONNECTOR to at least aimeat@{floor}",
            )
        )


def _profile_notes_match_order(inv: Inventory, report: Report) -> None:
    """A profile's `_note` must describe the order it actually has.

    These notes are the only record of WHY a model leads, so people trust them over reading the array —
    and on 2026-08-22 two of them were describing a routing that had not been live for weeks: both said
    the free meta-router LEADS while the array already led with the paid model. A note that contradicts
    its own data is worse than no note, because it is believed.

    The check is deliberately narrow: only a note that CLAIMS a lead ("X LEADS") is held to it.
    """
    for name, prof in sorted((inv.routing or {}).get("profiles", {}).items()):
        note = prof.get("_note") or ""
        if "LEAD" not in note.upper():
            continue
        providers = prof.get("providers") or []
        if not providers:
            continue
        first = {str(m.get("id") if isinstance(m, dict) else m) for m in (providers[0].get("models") or [])}
        if not first:
            continue
        every = {
            str(m.get("id") if isinstance(m, dict) else m) for prov in providers for m in (prov.get("models") or [])
        }
        claimed = _claimed_lead(note, every)
        if not claimed or claimed & first:
            continue
        report.add(
            Finding(
                "registry.routing.note_contradicts_order",
                WARN,
                f"profiles.{name}",
                f"the note says {sorted(claimed)} LEADS, but the chain actually leads with {sorted(first)} "
                f"— the prose describes a routing that is not live",
                "correct the _note to match the provider order (or reorder the providers, deliberately)",
            )
        )


def _claimed_lead(note: str, model_ids: set[str]) -> set[str]:
    """Which model the NOTE says leads — the id named just before a "LEAD"/"LEADS".

    Naively asking "does the note mention a non-leading model" is useless, because a good note names
    the whole chain ("X LEADS; Y is the fallback") and would always look contradictory. The claim being
    checked is narrower and is the one that misleads: whatever sits immediately before the word LEAD.
    """
    upper = note.upper()
    claimed: set[str] = set()
    for match in re.finditer(r"\bLEADS?\b", upper):
        window = note[max(0, match.start() - 90) : match.start()]
        for mid in model_ids:
            if mid and mid in window:
                claimed.add(mid)
    return claimed


def _declared_skills(text: str) -> set[str]:
    """Skill names this module names, resolving a one-hop module constant.

    `skill_body(EDITORIAL_SKILL)` is the normal shape once a name is used twice, so a literal-only
    scanner reports the skill as "consumed by nothing" while it is in fact driving the newspaper's
    editorial voice — a false report that would teach people to ignore the check.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return set()
    consts: dict[str, str] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            consts[node.targets[0].id] = node.value.value

    def _name_of(node: ast.expr) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Name):
            return consts.get(node.id)
        return None

    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else "")
            if name in {"skill_body", "load_skills"}:
                for arg in node.args[:1]:
                    items = arg.elts if isinstance(arg, (ast.List, ast.Tuple)) else [arg]
                    found |= {n for n in (_name_of(i) for i in items) if n}
            for kw in node.keywords:
                if kw.arg == "skills":
                    items = kw.value.elts if isinstance(kw.value, (ast.List, ast.Tuple)) else [kw.value]
                    found |= {n for n in (_name_of(i) for i in items) if n}
    return found


def _skills_exist(inv: Inventory, report: Report) -> None:
    """Every skill a crew or pipeline NAMES must be on disk, and every skill on disk should be used.

    A declared skill is loaded FAIL-LOUD — at daemon start for a crew, at step time for a deterministic
    pipeline — so a typo does not quietly degrade quality, it stops the agent or the edition. That is
    the right behaviour, and precisely why the typo should be caught before it ships: this is the
    cheapest check in the file and what it prevents is a crew that will not start.

    The reverse direction is a NOTE, not a finding: a skill nobody loads is not broken, but it is the
    shape the Skills subsystem was in for two months — three packs written, one consumed, and
    `sanomat-editorial-style` describing a voice that nothing in the newspaper read.
    """
    root = inv.root / "skills"
    available = (
        {d.name for d in root.iterdir() if d.is_dir() and (d / "SKILL.md").is_file()} if root.is_dir() else set()
    )
    scanned = list((inv.root / "crews").glob("*.py")) + list((inv.root / "src" / "crewaimeat").rglob("*.py"))
    used: set[str] = set()
    for path in scanned:
        try:
            declared = _declared_skills(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        used |= declared
        for name in sorted(declared - available):
            report.add(
                Finding(
                    "registry.skill.missing",
                    ERROR,
                    f"{name} ({path.name})",
                    f"names the skill '{name}', which is not in skills/ — whatever loads it fails loudly "
                    f"instead of running",
                    f"add skills/{name}/SKILL.md, or correct the name",
                )
            )
    unused = sorted(available - used)
    if unused:
        report.note(f"skills present but loaded by nothing: {', '.join(unused)}")

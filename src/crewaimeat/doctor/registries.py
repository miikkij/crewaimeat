"""Lens 1 — RECONCILIATION. What the crew DECLARES, versus everywhere that has to agree with it.

An agent used to be "real" in six hand-kept places: the crew file, the identity registry, the offers
registry, the routing map, serve.json, and the node. Nothing required them to agree, so on 2026-08-22
they did not: 13 crews had no identity, 13 no offer, 20 no routing, 12 registered agents had no crew
file at all, and one crew ran unregistered. Every one of those is a set difference — which is a
program, not an afternoon.

Three of those lists are gone: the crew file now declares its own tags, capabilities, offers and model
profile, and the rest is DERIVED. So the checks here changed shape too — they no longer ask "is this
agent in that list", they ask "does this crew declare what an agent needs, and does the world outside
the repo (serve.json, the node) still match it". The remaining disagreements are the ones that can
only exist outside the repo, plus the one that can still be forgotten: a declaration left empty.

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
    _run_mode(inv, report)
    report.note(
        f"crews: {len(inv.live)} live, {len(inv.crews) - len(inv.live)} parked · "
        f"serve.json: {len(inv.served)} registered · "
        f"declared: {sum(1 for a in inv.live_agents if inv.declares_identity(a))} identity, "
        f"{sum(1 for a in inv.live_agents if inv.declares_offer(a))} offers, "
        f"{sum(1 for a in inv.live_agents if inv.declared_profile(a))} routing · "
        f"routing overrides: {len([k for k in ((inv.routing or {}).get('crews') or {}) if not k.startswith('_')])}"
    )
    if inv.node_backed:
        report.note(
            f"node-backed, declared on the node not here: {', '.join(sorted(inv.node_backed))} "
            f"(identity, offer and routing live at crews.registry.<agent>; doctor is offline and "
            f"cannot read them — check them with `crewaimeat defs --as <agent>`)"
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

    for agent in sorted(inv.live_agents - inv.node_backed):
        if not inv.declares_identity(agent):
            report.add(
                Finding(
                    "registry.identity.missing",
                    WARN,
                    agent,
                    "the crew declares no TAGS and no CAPABILITIES, so the agent advertises the "
                    "liaison's generic onboarding defaults and discovery cannot match what it does",
                    "add TAGS + CAPABILITIES to the crew file (tags/capabilities in a JSON crew doc)",
                )
            )
    for crew in inv.crews:
        if not crew.agent or crew.capabilities is None:
            continue
        caps = crew.capabilities
        payload = {k: caps[k] for k in ("technical", "domain", "languages") if caps.get(k)}
        try:
            _validate_capabilities(crew.agent, payload)
        except ValueError as exc:
            first = str(exc).splitlines()[1].strip(" -") if len(str(exc).splitlines()) > 1 else str(exc)
            report.add(
                Finding(
                    "registry.identity.malformed",
                    ERROR,
                    crew.agent,
                    f"capabilities are the wrong shape: {first}",
                    "technical entries are {name, type} objects; free phrases belong in domain",
                )
            )
    for agent in sorted(set(inv.fallback_identity) - inv.live_agents - inv.parked_agents):
        report.add(
            Finding(
                "registry.identity.orphan",
                WARN,
                agent,
                "fleet_identity still holds a central entry for an agent with no crew file",
                "remove the entry — identity belongs in the crew, and this agent has none",
            )
        )


def _contract_offer_agents() -> set[str]:
    """Agents whose offers are DERIVED from a workspace contract, not authored in the crew.

    A contract agent advertises through `_OFFER_META` — its requirements, consequences and deliverable
    location all fall out of the CONTRACT dict, which is the whole point of that path. Reporting those
    five as "declares no OFFERS" is a false accusation, and a check that cries wolf gets switched off:
    the first version of this rule flagged web-researcher, which advertises three offers.
    """
    try:
        from crewaimeat.offers import _OFFER_META

        return {str(m.get("agent")) for m in _OFFER_META.values() if m.get("agent")}
    except Exception:  # noqa: BLE001
        return set()


def _crews_vs_offers(inv: Inventory, report: Report) -> None:
    from_contract = _contract_offer_agents()
    for agent in sorted(inv.live_agents - inv.node_backed):
        if inv.declares_offer(agent) or agent in from_contract:
            continue
        crew = inv.crew_of(agent)
        if crew and "offer=" in crew.path.read_text(encoding="utf-8"):
            continue  # an inline CrewSpec(offer=...) — the forged-crew path
        report.add(
            Finding(
                "registry.offer.missing",
                WARN,
                agent,
                "the crew declares no OFFERS — it does not advertise what it can do, so it is "
                "invisible on the Tarjoama surface and to any agent shopping for a capability",
                "add OFFERS = [...] to the crew file (offers in a JSON crew doc)",
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
    for agent in sorted(inv.live_agents - inv.node_backed):
        if agent in crews_map or inv.declared_profile(agent):
            continue
        report.add(
            Finding(
                "registry.routing.unmapped",
                WARN,
                agent,
                f"declares no LLM_PROFILE and has no override entry, so it silently resolves to the "
                f"'{default}' profile. A default is fine; an UNDECIDED default is how 20 crews ended "
                f"up on a profile nobody chose for them",
                'add LLM_PROFILE = "<profile>" to the crew file (write the default explicitly if that is the decision)',
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


def _run_mode(inv: Inventory, report: Report) -> None:
    """The spawned run mode: is what the crew declares actually runnable, and is it running ALONE?

    Three things can only be caught here, and each one fails SILENTLY in production:
      * a typo in RUN_MODE reads as "continuous" (deliberately — a typo must not relocate an agent),
        so the crew keeps working and the author never learns the spawn declaration did nothing;
      * a CUSTOM `on_invoke` handler never runs under spawn: the spawner holds the invoke poll (which
        is what stops the node answering NO_HANDLER after 90 s) and answers crew.validate / crew.try
        in a worker of its own, so the crew's handler is bypassed silently;
      * a `runner` block in the connector's per-agent config makes the SERVE DAEMON start its own
        worker for the same task alongside ours. The agent lock then kills one of the two with a clean
        exit 0, and the only symptom is "the task did nothing".
    """
    from crewaimeat.agent_manifest import RUN_MODES, RUN_SPAWN, normalise_run_mode

    for crew in inv.live:
        if crew.agent is None:
            continue
        where = crew.path.as_posix()
        if crew.run_mode is not None and normalise_run_mode(crew.run_mode) is None:
            report.add(
                Finding(
                    "runmode.unknown",
                    ERROR,
                    where,
                    f"RUN_MODE = {crew.run_mode!r} is not a run mode, so it reads as "
                    f"{RUN_MODES[0]!r} and the declaration does nothing",
                    f"use one of {RUN_MODES}, or drop the constant to stay {RUN_MODES[0]}",
                )
            )
        if crew.effective_run_mode != RUN_SPAWN:
            continue

        if crew.max_concurrent is None:
            report.add(
                Finding(
                    "concurrency.undeclared",
                    WARN,
                    where,
                    "spawn-mode crew declares no MAX_CONCURRENT, so its concurrency is read from the "
                    "node once at worker start — invisible here and different per machine",
                    "declare MAX_CONCURRENT = 1 (single-flight) or the parallelism this crew can take",
                )
            )
        try:
            source = crew.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            source = ""
        if "on_invoke=" in source:
            report.add(
                Finding(
                    "runmode.spawn.custom_invoke",
                    ERROR,
                    where,
                    "spawn mode + a CUSTOM on_invoke handler: the spawner holds the invoke poll for its "
                    "agents (so the node never sees NO_HANDLER) and answers crew.validate / crew.try "
                    "itself in a worker — it does NOT route to this crew's own handler, so the handler "
                    "would simply never run",
                    'either drop the custom on_invoke, or declare RUN_MODE = "resident" so fleet_host '
                    "holds this crew's own poller",
                )
            )
        # The per-agent settings file lives at agents/<owner>/<agent>/config.yaml since the connector
        # started serving more than one owner from one daemon; it used to be agents/<agent>/. Both are
        # checked, because a rule that looks only where the file NO LONGER IS reports "clean" forever —
        # a false green is worse than the error it was written to catch.
        agents_dir = inv.root / ".aimeat" / "agents"
        candidates = [agents_dir / crew.agent / "config.yaml", *agents_dir.glob(f"*/{crew.agent}/config.yaml")]
        cfg = next((c for c in candidates if c.is_file()), candidates[0])
        try:
            cfg_text = cfg.read_text(encoding="utf-8", errors="replace") if cfg.is_file() else ""
        except OSError:
            cfg_text = ""
        if re.search(r"^\s*runner\s*:", cfg_text, re.MULTILINE):
            report.add(
                Finding(
                    "spawn.connector_runner_set",
                    ERROR,
                    where,
                    f"{cfg.as_posix()} declares a `runner` block, so the SERVE DAEMON starts its own "
                    "subprocess for each task beside our worker; the agent lock then kills one of them "
                    "with exit 0 and the task looks like it silently did nothing",
                    "remove the `runner:` block — crewaimeat's spawner owns the worker lifecycle",
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
    # Both shapes matter: `SKILL = "x"` and `SKILLS = ["x", "y"]`. Resolving only the string form made
    # the check report `aimeat-agent-modes` as "loaded by nothing" while two crews were loading it
    # through `skills=SKILLS` — a false report, which is how a check earns the reputation that gets it
    # ignored.
    consts: dict[str, object] = {}
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)):
            continue
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, SyntaxError, TypeError):
            continue
        if isinstance(value, str) or (isinstance(value, (list, tuple)) and all(isinstance(x, str) for x in value)):
            consts[node.targets[0].id] = value

    def _names_of(node: ast.expr) -> list[str]:
        """Skill names an expression denotes: a literal, a constant, or a constant LIST."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node.value]
        if isinstance(node, ast.Name):
            v = consts.get(node.id)
            if isinstance(v, str):
                return [v]
            if isinstance(v, (list, tuple)):
                return [x for x in v if isinstance(x, str)]
        return []

    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else "")
            if name in {"skill_body", "load_skills"}:
                for arg in node.args[:1]:
                    items = arg.elts if isinstance(arg, (ast.List, ast.Tuple)) else [arg]
                    for i in items:
                        found |= set(_names_of(i))
            for kw in node.keywords:
                if kw.arg == "skills":
                    items = kw.value.elts if isinstance(kw.value, (ast.List, ast.Tuple)) else [kw.value]
                    for i in items:
                        found |= set(_names_of(i))
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

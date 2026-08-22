"""`crewaimeat retire <agent>` — the missing half of an agent's lifecycle.

`crew-forge` can create an agent with one command: write the crew file, register the identity on the
node, launch it. There has never been an opposite command. That asymmetry is not a small gap — it is
the direct cause of a whole class of drift, because an experiment you cannot cheaply undo becomes
permanent by default:

  · 12 agents registered in serve.json with no crew file, still opening a tunnel on every fleet start
    (one of them, crypto-weekly-reporter, became the node's single largest traffic source)
  · ~20 near-duplicate experiments on the node — eleven separate AI-news archivists
  · 6 schedules disabled since June that nobody dares delete, because nobody is sure what they fire

So: anything that can be created by one command must be removable by one command.

Retiring is DELIBERATELY CONSERVATIVE. It parks and unregisters; it never deletes the crew file and
never deletes memory. The point is to stop an agent participating, not to destroy the record of what
it did — a retired agent's deliverables are still the owner's data.

    crewaimeat retire <agent>            # show the plan, change nothing
    crewaimeat retire <agent> --apply    # do it
    crewaimeat retire <agent> --apply --purge-node   # also disable its schedules on the node
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Step:
    what: str  # human description
    done: bool = False
    detail: str = ""


def _home() -> Path:
    from crewaimeat._home import aimeat_home

    return Path(aimeat_home())


def _serve_path() -> Path:
    return _home() / "serve.json"


def _crew_path(root: Path, agent: str) -> Path | None:
    """The crew file backing this agent, parked or not."""
    from crewaimeat.doctor import inventory

    inv = inventory.gather(root)
    crew = inv.crew_of(agent)
    return crew.path if crew else None


def plan(root: Path, agent: str, *, purge_node: bool) -> list[Step]:
    steps: list[Step] = []
    path = _crew_path(root, agent)
    if path is None:
        steps.append(Step(f"crew file: none found for '{agent}' (a ghost registration — nothing to park)"))
    elif path.name.startswith("_"):
        steps.append(Step(f"crew file: {path.as_posix()} is already parked"))
    else:
        steps.append(Step(f"crew file: park {path.name} -> _{path.name} (the fleet stops discovering it)"))

    serve = _serve_path()
    if serve.exists():
        try:
            data = json.loads(serve.read_text(encoding="utf-8"))
            present = any(a.get("agent") == agent for a in data.get("agents") or [])
        except (OSError, ValueError):
            present = False
        steps.append(
            Step(f"serve.json: remove the '{agent}' registration (stops the tunnel + wake queue)")
            if present
            else Step(f"serve.json: '{agent}' is not registered")
        )
    else:
        steps.append(Step("serve.json: not found"))

    tok = _home() / "tokens" / f"{agent}@*.token"
    steps.append(Step(f"token file: move {tok.name} aside (kept, so re-registering is easy)"))
    steps.append(Step("registries: remove entries from fleet_identity / offers / llm_providers.json crews"))
    if purge_node:
        steps.append(Step(f"node: disable every schedule whose target is '{agent}'"))
    else:
        steps.append(Step("node: schedules NOT touched (pass --purge-node), memory never touched"))
    return steps


def _park_crew(root: Path, agent: str) -> Step:
    path = _crew_path(root, agent)
    if path is None:
        return Step("crew file", True, "no crew file — nothing to park")
    if path.name.startswith("_"):
        return Step("crew file", True, "already parked")
    dest = path.with_name(f"_{path.name}")
    if dest.exists():
        return Step("crew file", False, f"{dest.name} already exists — resolve by hand")
    path.rename(dest)
    return Step("crew file", True, f"parked as {dest.name}")


def _drop_registration(agent: str) -> Step:
    serve = _serve_path()
    if not serve.exists():
        return Step("serve.json", True, "not found")
    try:
        data = json.loads(serve.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return Step("serve.json", False, f"unreadable: {exc}")
    agents = data.get("agents") or []
    kept = [a for a in agents if a.get("agent") != agent]
    if len(kept) == len(agents):
        return Step("serve.json", True, "was not registered")
    # A dated backup, because serve.json holds every agent's token: a bad edit costs a full re-auth
    # of the whole fleet, which is exactly the kind of expensive mistake a retire command must not make.
    backup = serve.with_suffix(f".json.before-retire-{agent}")
    backup.write_text(serve.read_text(encoding="utf-8"), encoding="utf-8")
    data["agents"] = kept
    serve.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return Step("serve.json", True, f"registration removed ({len(agents)} -> {len(kept)}); backup {backup.name}")


def _stash_token(agent: str) -> Step:
    tokens = _home() / "tokens"
    if not tokens.is_dir():
        return Step("token", True, "no tokens directory")
    hits = list(tokens.glob(f"{agent}@*.token"))
    if not hits:
        return Step("token", True, "no token file")
    retired = tokens / "retired"
    retired.mkdir(exist_ok=True)
    for h in hits:
        h.rename(retired / h.name)
    return Step("token", True, f"moved {len(hits)} token file(s) to tokens/retired/ (kept, not deleted)")


def _clean_routing(root: Path, agent: str) -> Step:
    p = root / "llm_providers.json"
    if not p.exists():
        return Step("routing", True, "no llm_providers.json")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return Step("routing", False, f"unreadable: {exc}")
    crews = data.get("crews") or {}
    if agent not in crews:
        return Step("routing", True, "no entry")
    crews.pop(agent)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return Step("routing", True, "entry removed from llm_providers.json")


def _registry_reminder(root: Path, agent: str) -> Step:
    """fleet_identity and offers are PYTHON, not data — a retire command must not rewrite source.

    Removing a dict entry from a module by regex is exactly the kind of clever edit that eventually
    eats a neighbouring entry. `doctor` reports the leftovers as `registry.identity.orphan` /
    `registry.offer.orphan`, so the reminder is enforced rather than trusted.
    """
    from crewaimeat.doctor import inventory

    inv = inventory.gather(root)
    left = []
    if agent in inv.identity:
        left.append("src/crewaimeat/fleet_identity.py")
    if agent in inv.offer_agents:
        left.append("src/crewaimeat/offers.py")
    if not left:
        return Step("registries", True, "no source entries to remove")
    return Step("registries", True, "remove by hand: " + ", ".join(left) + "  (doctor will keep reporting them)")


def _purge_node_schedules(agent: str) -> Step:
    from crewaimeat.aimeat_crew import _aimeat_call

    probe = agent
    data = _aimeat_call(probe, "aimeat_schedule_list", {}) or {}
    schedules = data.get("schedules") if isinstance(data, dict) else None
    if not schedules:
        return Step("node schedules", False, "could not read the schedule list (is the fleet attached?)")
    hit = [s for s in schedules if str(s.get("agent") or s.get("target_agent") or "") == agent and s.get("enabled")]
    if not hit:
        return Step("node schedules", True, "none enabled for this agent")
    disabled = 0
    for s in hit:
        res = _aimeat_call(probe, "aimeat_schedule_update", {"id": s.get("id"), "enabled": False})
        disabled += 1 if res else 0
    return Step("node schedules", disabled == len(hit), f"disabled {disabled}/{len(hit)}")


def apply(root: Path, agent: str, *, purge_node: bool) -> list[Step]:
    steps = [_park_crew(root, agent), _drop_registration(agent), _stash_token(agent), _clean_routing(root, agent)]
    steps.append(_registry_reminder(root, agent))
    if purge_node:
        steps.append(_purge_node_schedules(agent))
    return steps


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="crewaimeat retire", description=__doc__.splitlines()[0])
    ap.add_argument("agent", help="the AIMEAT agent name to retire")
    ap.add_argument("--apply", action="store_true", help="actually do it (without this, only the plan is shown)")
    ap.add_argument("--purge-node", action="store_true", help="also disable the agent's schedules on the node")
    ap.add_argument("--root", default=".", help="repo root (default: cwd)")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()

    if not args.apply:
        print(f"retire '{args.agent}' would do:\n")
        for s in plan(root, args.agent, purge_node=args.purge_node):
            print(f"  · {s.what}")
        print("\nNothing changed. Re-run with --apply.")
        print("Memory is never touched: a retired agent's deliverables stay the owner's data.")
        return 0

    print(f"retiring '{args.agent}':\n")
    failed = 0
    for s in apply(root, args.agent, purge_node=args.purge_node):
        mark = "ok  " if s.done else "FAIL"
        failed += 0 if s.done else 1
        print(f"  {mark} {s.what}: {s.detail}")
    print("\nRestart the fleet for this to take effect (scripts/start_fleet.ps1).")
    print("Then run `crewaimeat doctor` — it should report one fewer finding.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

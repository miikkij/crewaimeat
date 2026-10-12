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
    crewaimeat retire <agent> --apply --forget       # the NODE deleted it: delete what this machine keeps

`--forget` IS FOR AN AGENT THAT NO LONGER EXISTS ON THE NODE, and it checks that first. The spawner
does the same by itself when an agent leaves its roster as deleted (`Spawner.forget_departed`), so
this command is for what is left from before that, or on a machine with no spawner running. Deleting an
agent on the node revokes its sessions and removes its records there; nothing told this machine.
Measured 2026-10-11 with the test agent `tarjoama-testi`: hours after the node deleted it, this home
still held its key, its per-agent config, its line in serve.json (the daemon carrying it as
`auth_failed`), its README cache, two lock files and the audit and log of every run. The plain
retire would have moved the key aside "so re-registering is easy" — a key the node will never accept
again — and left the rest. `--forget` deletes the credential and those traces, and refuses to do
anything while the node still lists the agent or cannot be asked: a key deleted by mistake costs a
new device approval, so the node's word comes before the first file is touched.
"""

from __future__ import annotations

import argparse
import glob
import json
from dataclasses import dataclass
from pathlib import Path

from crewaimeat.agent_manifest import agent_local_name


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


def _serve_doc() -> dict:
    try:
        return json.loads(_serve_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _identities(agent: str) -> list[str]:
    """Every identity this home can file the agent under: see `agent_files.identities_of`.

    NOT READ FROM THE AGENT'S OWN REGISTRATION ALONE. It was — its line in serve.json was the one
    place the full identity was written down — and since the connector removes that line itself
    when the node deletes an agent, the lookup came back with the bare name, under which a
    node-defined agent files nothing. The home's other agents say which owners and nodes it serves."""
    from crewaimeat import agent_files

    return agent_files.identities_of(agent, _serve_doc())


def _node_still_lists(agent: str) -> tuple[bool | None, str]:
    """`(listed, why)`: does the NODE still list this agent? None when it could not be asked.

    The spawner's question, asked the spawner's way (`spawner.node_still_lists`): through the daemon,
    as a neighbour of the same owner, never as the agent in doubt."""
    from crewaimeat import spawner

    return spawner.node_still_lists(agent)


def _forget_targets(root: Path, agent: str, idents: list[str]) -> list[Path]:
    """The files and folders this machine keeps for the agent, existing ones only: the connector's
    (a key or token, the per-agent config) where they are still there, and the runtime's own.

    EXACT NAMES, NO PREFIX MATCHES: `news` must never take `news-fetcher` with it."""
    from crewaimeat import agent_files

    found = agent_files.credential_files(agent) + agent_files.runtime_files(root, idents)
    seen: set[Path] = set()
    return [p for p in found if not (p in seen or seen.add(p))]


def _forget_local(root: Path, agent: str, idents: list[str]) -> Step:
    """Delete them, and say what was deleted, what was left and why.

    "NOTHING LEFT" IS SAID ONLY WHEN A SEARCH BY NAME FINDS NOTHING. Files that start with the
    agent's name and belong to no identity this home can name are not deleted (they may be another
    agent's) and are not passed over either: they are listed, and the step is not ok."""
    from crewaimeat import agent_files

    targets = _forget_targets(root, agent, idents)
    strangers = agent_files.look_alikes(root, agent, targets)
    removed, running, failed = agent_files.remove(root, targets)
    said: list[str] = []
    if removed:
        said.append(f"deleted {len(removed)}: " + ", ".join(removed))
    if running:
        said.append("left in place: " + "; ".join(running))
    if failed:
        said.append("could NOT delete: " + "; ".join(failed))
    if strangers:
        shown = ", ".join(p.name for p in strangers)
        said.append(
            f"NOT deleted, {len(strangers)} file(s) that start with this name under an owner this home does not serve: {shown}"
        )
    if not said:
        return Step("local files", True, "nothing left on this machine")
    return Step("local files", not failed and not strangers, "; ".join(said))


def plan(root: Path, agent: str, *, purge_node: bool, forget: bool = False) -> list[Step]:
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

    if forget:
        targets = _forget_targets(root, agent, _identities(agent))
        steps.append(
            Step(
                f"local files: ONLY IF the node no longer lists '{agent}', delete what this machine "
                f"keeps for it ({len(targets)} found by name): " + (", ".join(p.name for p in targets) or "none")
            )
        )
    else:
        steps.append(
            Step(
                f"credential: move {agent}@*.token and {agent}@*.key aside (kept, so re-registering is "
                "easy) — this is what actually retires it; serve.json is rebuilt from the credential store"
            )
        )
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
    """Move the agent's CREDENTIAL aside — a v1 bearer in `tokens/`, a v2 key in `keys/`, or both.

    THE CREDENTIAL IS WHAT RETIREMENT ACTUALLY REMOVES. serve.json is a discovery file the daemon
    REGENERATES from the credential store on every start, so editing it alone is undone by the next
    restart. Measured 2026-09-04: four agents were retired, each reported "registration removed", and
    all four were back in serve.json minutes later because only `tokens/` was consulted and their
    credential was a key. A step that says ok and does not hold is worse than one that refuses.
    """
    home = _home()
    moved: list[str] = []
    looked: list[str] = []
    for folder, suffix in (("tokens", ".token"), ("keys", ".key")):
        d = home / folder
        if not d.is_dir():
            continue
        looked.append(folder)
        hits = list(d.glob(f"{glob.escape(agent_local_name(agent))}@*{suffix}"))
        if not hits:
            continue
        retired = d / "retired"
        retired.mkdir(exist_ok=True)
        for h in hits:
            h.rename(retired / h.name)
            moved.append(f"{folder}/{h.name}")
    if not looked:
        return Step("credential", True, "no tokens/ or keys/ directory")
    if not moved:
        return Step("credential", True, f"no credential file (looked in {', '.join(looked)}/)")
    return Step(
        "credential", True, f"moved {len(moved)} aside to <store>/retired/ (kept, not deleted): {', '.join(moved)}"
    )


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
    # `fallback_identity` is the (normally empty) central dict. Since 2026-08-22 an agent's identity and
    # offers live in its own crew file, so parking the crew already takes them out of circulation and
    # there is usually nothing central left to remove. An entry here means someone added one back.
    if agent in inv.fallback_identity:
        left.append("src/crewaimeat/fleet_identity.py")
    if not left:
        return Step("registries", True, "nothing central to remove — the crew file carried its own")
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


def apply(root: Path, agent: str, *, purge_node: bool, forget: bool = False) -> list[Step]:
    idents = _identities(agent)  # before the registration goes: it is where the identity is written
    if forget:
        listed, why = _node_still_lists(agent)
        if listed is not False:  # still there, or nobody could say: touch nothing
            return [Step("node", False, f"{why} — nothing was changed")]
    steps = [_park_crew(root, agent), _drop_registration(agent)]
    # The credential is moved aside for an agent that may come back, and deleted (below, with the
    # rest) for one the node no longer has.
    steps.append(_forget_local(root, agent, idents) if forget else _stash_token(agent))
    steps.append(_clean_routing(root, agent))
    steps.append(_registry_reminder(root, agent))
    if purge_node:
        steps.append(_purge_node_schedules(agent))
    return steps


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="crewaimeat retire", description=__doc__.splitlines()[0])
    ap.add_argument("agent", help="the AIMEAT agent name to retire")
    ap.add_argument("--apply", action="store_true", help="actually do it (without this, only the plan is shown)")
    ap.add_argument("--purge-node", action="store_true", help="also disable the agent's schedules on the node")
    ap.add_argument(
        "--forget",
        action="store_true",
        help="the node DELETED this agent: delete its credential and local traces (checked against the node first)",
    )
    ap.add_argument("--root", default=".", help="repo root (default: cwd)")
    args = ap.parse_args(argv)
    root = Path(args.root).resolve()
    args.agent = agent_local_name(args.agent)  # everything below is filed under the NAME

    if not args.apply:
        print(f"retire '{args.agent}' would do:\n")
        for s in plan(root, args.agent, purge_node=args.purge_node, forget=args.forget):
            print(f"  · {s.what}")
        print("\nNothing changed. Re-run with --apply.")
        print("Memory is never touched: a retired agent's deliverables stay the owner's data.")
        return 0

    print(f"retiring '{args.agent}':\n")
    failed = 0
    for s in apply(root, args.agent, purge_node=args.purge_node, forget=args.forget):
        mark = "ok  " if s.done else "FAIL"
        failed += 0 if s.done else 1
        print(f"  {mark} {s.what}: {s.detail}")
    print("\nRestart the fleet for this to take effect (scripts/start_fleet.ps1).")
    print("Then run `crewaimeat doctor` — it should report one fewer finding.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

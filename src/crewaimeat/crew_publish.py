"""`crewaimeat publish` / `install` / `defs` — move a crew definition between disk and the node.

`crew_registry.py` has done the work since Phase 1: validate, publish to `crews.registry.<agent>`,
fetch it back, install it somewhere else — even from another owner by GAII. What it never had was a
way in that was not an agent calling a tool. These three commands are that way in.

    crewaimeat publish crew_defs/joker.json --as joker
    crewaimeat publish mydef.json --as joker --public      shareable by GAII
    crewaimeat defs --as joker                             what the registry holds
    crewaimeat install research-bot --as joker             fetch + materialise locally
    crewaimeat install research-bot --as joker --node-backed   the loader only; def stays on the node

WHICH IDENTITY. Every one of these calls the node, and the node answers to an agent's token, so
`--as` names the agent whose credentials to spend. It defaults to the definition's own
`agent_name` — right once that agent is registered, useless before, which is exactly when you are
publishing its first definition. The error says so rather than making you guess.

TWO WAYS TO INSTALL, and the difference matters. The default materialises `crew_defs/<name>.json`
plus a loader that reads that FILE: a snapshot, yours to edit, and it will not change under you.
`--node-backed` writes only the loader, and the definition stays at `crews.registry.<name>` where
the Crew tab edits it — the agent then follows the node and needs no restart to change. Take the
snapshot when you want to own the crew; take the node-backed one when somebody else will be editing
it from AIMEAT.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _load(path: str) -> dict:
    from crewaimeat.crew_def import load_crew_doc

    return load_crew_doc(Path(path))


def _identity(explicit: str | None, doc: dict | None, name: str | None) -> str:
    who = (explicit or "").strip() or str((doc or {}).get("agent_name") or name or "").strip()
    if not who:
        raise ValueError("no agent to call the node as — pass --as <registered agent>")
    return who


def cmd_publish(path: str, as_agent: str | None, public: bool, offline: bool = False) -> int:
    from crewaimeat.crew_registry import publish_crew_def, publish_crew_def_live

    try:
        doc = _load(path)
    except (OSError, ValueError) as exc:
        print(f"FAILED to read {path}: {exc}", file=sys.stderr)
        return 2
    who = _identity(as_agent, doc, None)

    # THE AGENT'S OWN LIVE DEFINITION goes through the node's publish route: it validates against
    # that agent's runtime, numbers the revision, keeps the last ten restorable, and wakes the
    # runtime so the change is in force in seconds. `--public` is a different act — a template for
    # somebody else to install by GAII, which no runtime of ours validates — so it keeps the direct
    # write, and `--offline` is the deliberate way to say the agent is not up and you meant it.
    if public or offline:
        ok, key, detail = publish_crew_def(doc, agent=who, visibility="public" if public else "owner")
    else:
        ok, key, detail = publish_crew_def_live(doc, agent=who)
        if not ok and "AGENT_OFFLINE" not in detail and "runtime is not up" in detail:
            detail += "\n  Publish it anyway with --offline (writes the key directly, outside the numbered history)."
    if not ok:
        print(detail, file=sys.stderr)
        # The commonest cause of a failed publish is not a bad document but an identity with no token.
        if "INVALID" not in detail:
            print(
                f"\nPublished as {who!r}. If that agent is not registered on this machine, name one that is with --as.",
                file=sys.stderr,
            )
        return 1
    print(f"published {doc['agent_name']} -> {key}" + (" (public)" if public else ""))
    print(detail)
    print(
        f"\nThe agent follows this key: the NEXT TASK uses the new definition, no restart.\n"
        f"If {doc['agent_name']!r} has no loader here yet: crewaimeat new-json-agent {doc['agent_name']}"
    )
    return 0


def cmd_install(name: str, as_agent: str | None, gaii: str | None, node_backed: bool, register: bool) -> int:
    from crewaimeat.crew_def import CrewDocError
    from crewaimeat.crew_registry import fetch_crew_def, install_crew_def

    who = _identity(as_agent, None, name)
    if node_backed:
        # Fetch only to PROVE the definition is there and valid — then write the loader and leave the
        # definition where it is. Installing a loader for a key that holds nothing would produce an
        # agent that cannot start, and the reason would surface at fleet start instead of here.
        from crewaimeat.json_agent import write_loader

        try:
            doc = fetch_crew_def(name, agent=who, gaii=gaii)
        except CrewDocError as exc:
            print(f"FAILED: {name} has no valid definition on the node:", file=sys.stderr)
            for e in getattr(exc, "errors", None) or [str(exc)]:
                print(f"  - {e}", file=sys.stderr)
            return 1
        try:
            path = write_loader(doc["agent_name"])
        except FileExistsError as exc:
            print(f"FAILED: {exc}", file=sys.stderr)
            return 1
        print(f"wrote {path}")
        print(
            f"\n{doc['agent_name']} is node-backed: its crew stays at crews.registry.{doc['agent_name']}\n"
            "and is edited in AIMEAT. Nothing about it lives in this repo but the name."
        )
        return 0

    try:
        print(install_crew_def(name, agent=who, gaii=gaii, register=register))
    except CrewDocError as exc:
        print(f"FAILED: {name} has no valid definition on the node:", file=sys.stderr)
        for e in getattr(exc, "errors", None) or [str(exc)]:
            print(f"  - {e}", file=sys.stderr)
        return 1
    return 0


def cmd_defs(as_agent: str | None) -> int:
    from crewaimeat.crew_registry import list_crew_defs

    who = _identity(as_agent, None, None)
    rows = list_crew_defs(agent=who)
    if not rows:
        print("the registry holds no crew definitions for this owner.")
        return 0
    for r in rows:
        name = r.get("agent_name", "?")
        extra = " ".join(f"{k}={r[k]}" for k in ("revision", "visibility", "publishedAt") if r.get(k))
        print(f"  {name:28} {extra}")
    print(f"\n{len(rows)} definition(s). Install one: crewaimeat install <name> --as {who}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="crewaimeat publish|install|defs")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("publish", help="validate a def and publish it to the node's crew registry")
    p.add_argument("path")
    p.add_argument("--as", dest="as_agent", default=None, help="agent whose token calls the node")
    p.add_argument("--public", action="store_true", help="any owner may install it by your GAII")
    p.add_argument(
        "--offline",
        action="store_true",
        help="write the registry key directly instead of through the node (use when the agent's runtime is down)",
    )

    i = sub.add_parser("install", help="materialise a def from the registry")
    i.add_argument("name")
    i.add_argument("--as", dest="as_agent", default=None)
    i.add_argument("--from", dest="gaii", default=None, help="another owner's GAII for a public def")
    i.add_argument("--node-backed", action="store_true", help="write only the loader; the def stays on the node")
    i.add_argument("--no-register", action="store_true", help="materialise only — do not register/launch")

    d = sub.add_parser("defs", help="list the crew definitions in the registry")
    d.add_argument("--as", dest="as_agent", default=None)

    a = ap.parse_args(argv)
    try:
        if a.cmd == "publish":
            return cmd_publish(a.path, a.as_agent, a.public, getattr(a, "offline", False))
        if a.cmd == "install":
            return cmd_install(a.name, a.as_agent, a.gaii, a.node_backed, not a.no_register)
        return cmd_defs(a.as_agent)
    except ValueError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

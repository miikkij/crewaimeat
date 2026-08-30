"""`crewaimeat orphans` — the agents the node still holds that this repo knows nothing about.

WHY THIS NEEDS AN OWNER TOKEN, AND WHY A SCOPE WILL NOT DO. The node's door is

    DELETE /v1/agents/:name   requireRoleOrScope('owner', 'agent:delete')

and inside it (aimeat/src/routes/agents/management.ts) there are two callers:

  · an OWNER (or operator) session may delete any of that owner's agents;
  · an AGENT may delete one only when it is ALSO the principal that registered it (`registeredBy`),
    on top of carrying `agent:delete`.

So granting a fleet agent `agent:delete` does NOT unlock the leftovers here: 20 of them were
registered by the node's hatchery, and `registeredBy` is written once at creation. The node's own
comment explains the choice — "same-owner on THIS door would mean every agent an owner has may kill
every sibling it has never seen"; "the scope alone would hand a single approved agent the whole
fleet". Both conditions, or an owner session. That is a deliberate boundary, not a gap.

Every token under `.aimeat/` is an AGENT token, which is why the first attempt at this failed. The
owner's own session is the key, and it is the owner's to hand over:

    $env:AIMEAT_OWNER_TOKEN = "<token>"      # from the dashboard, or POST /v1/ghii/login
    uv run crewaimeat orphans                # list, change nothing
    uv run crewaimeat orphans --apply        # delete the ones with no crew file here

Without the token this still LISTS — the read needs no owner role — and says exactly what is missing.
It never accepts a password: obtaining the token is the owner's step, not this tool's.
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Interactive tool identities (a Claude Desktop / goose / VS Code session) are the OWNER at a
# keyboard, not fleet leftovers. They are never offered for deletion.
TOOL_SESSION = re.compile(r"claude|goose|openhands|cursor|vscode|desktop|^chat$|^analyst$|hermes|dify|secretary|probe")


@dataclass
class Orphan:
    name: str
    mode: str
    last_seen: str
    age_days: int | None
    created: str
    registered_by: str


def _age(stamp: str | None) -> int | None:
    if not stamp:
        return None
    try:
        dt = datetime.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.datetime.now(datetime.timezone.utc) - dt).days


def owner_token() -> str | None:
    return (os.getenv("AIMEAT_OWNER_TOKEN") or "").strip() or None


def find(root: Path, probe: str) -> tuple[list[Orphan], list[str]]:
    """(orphans, tool_sessions). An orphan is an agent the node holds with no crew file here."""
    from crewaimeat.agent_manifest import by_agent
    from crewaimeat.aimeat_crew import _aimeat_call
    from crewaimeat.doctor.inventory import gather

    inv = gather(root)
    mine = set(by_agent(root)) | set(inv.served)
    data = _aimeat_call(probe, "aimeat_agents_list", {}) or {}
    roster = data.get("agents") if isinstance(data, dict) else None
    if not roster:
        return [], []

    orphans, tools = [], []
    for a in roster:
        name = a.get("name") or ""
        if not name or name in mine:
            continue
        if TOOL_SESSION.search(name.lower()) or a.get("mode") == "interactive":
            tools.append(name)
            continue
        orphans.append(
            Orphan(
                name=name,
                mode=str(a.get("mode") or "?"),
                last_seen=str(a.get("last_seen") or "")[:10],
                age_days=_age(a.get("last_seen")),
                created=str(a.get("created_at") or "")[:10],
                registered_by=str(a.get("registered_by") or a.get("registeredBy") or "").split("#")[0],
            )
        )
    orphans.sort(key=lambda o: (o.created, o.name))
    return orphans, sorted(tools)


def delete(name: str, node_url: str, token: str) -> tuple[bool, str]:
    """DELETE /v1/agents/<name> as the OWNER. Returns (ok, detail).

    The owner comes from the TOKEN (`req.auth.owner`), not from a header — an invented header would
    be ignored at best and misleading at worst. The bearer is the whole authorisation.
    """
    import requests

    try:
        r = requests.delete(
            f"{node_url.rstrip('/')}/v1/agents/{name}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001 — a network failure is a result, not a crash
        return False, f"request failed: {exc!r}"
    if r.status_code in (200, 204):
        return True, "deleted"
    return False, f"HTTP {r.status_code} {r.text[:160]}"


def _node_and_owner(root: Path) -> tuple[str, str]:
    from crewaimeat.doctor.inventory import gather

    inv = gather(root)
    first = next(iter(sorted(inv.served)), None)
    entry = inv.served.get(first or "", {})
    return str(entry.get("node_url") or "https://aimeat.io"), str(entry.get("owner") or "")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="crewaimeat orphans", description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true", help="actually delete them (needs AIMEAT_OWNER_TOKEN)")
    ap.add_argument("--older-than", type=int, default=0, help="only those last seen more than N days ago")
    ap.add_argument("--root", default=".", help="repo root (default: cwd)")
    ap.add_argument("--only", default="", help="comma-separated agent names: sweep ONLY these")
    ap.add_argument(
        "--except",
        dest="excluded",
        default="",
        help="comma-separated names to spare — for an orphan HERE that another repo still backs",
    )
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    from crewaimeat.doctor.inventory import gather

    probe = next(iter(sorted(gather(root).served)), None)
    if not probe:
        print("orphans: nothing registered in serve.json — no identity to read the node with", file=sys.stderr)
        return 2

    orphans, tools = find(root, probe)
    if not orphans and not tools:
        print("orphans: the node returned no agent roster (run with the fleet attached)", file=sys.stderr)
        return 2

    # WHY A NAME LIST AND NOT JUST AN AGE. "No crew file HERE" is not "nobody runs it": this
    # machine holds sibling checkouts and other products against the SAME owner, and their agents
    # look like orphans from in here. Two of them belong to a live Company Brain and are 59 days
    # cold — the same age as a real leftover, so `--older-than` cannot tell them apart. Sweeping by
    # age alone would have taken them.
    only = {n.strip() for n in (args.only or "").split(",") if n.strip()}
    spared = {n.strip() for n in (args.excluded or "").split(",") if n.strip()}
    unknown = (only | spared) - {o.name for o in orphans}
    if unknown:
        print(f"error: not an orphan on this node: {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2

    def _wanted(o) -> bool:  # noqa: ANN001
        if o.name in spared:
            return False
        if only:
            return o.name in only
        return not args.older_than or (o.age_days or 0) > args.older_than

    targets = [o for o in orphans if _wanted(o)]
    keep = [o for o in orphans if not _wanted(o)]

    print(f"agents on the node with no crew file here: {len(orphans)}\n")
    width = max((len(o.name) for o in orphans), default=10)
    for o in targets:
        age = f"{o.age_days}d" if o.age_days is not None else "?"
        by = f"  registered by {o.registered_by}" if o.registered_by else ""
        print(f"  {o.name:<{width}}  created {o.created}  last seen {age:>5}{by}")
    if keep:
        why = (
            "--only" if only else ("--except" if spared and not args.older_than else f"--older-than {args.older_than}")
        )
        print(f"\n  {len(keep)} left alone by {why}: {', '.join(o.name for o in keep)}")
    if tools:
        print(f"\n  {len(tools)} interactive tool session(s) left alone — those are you, not leftovers:")
        print(f"    {', '.join(tools)}")

    if not args.apply:
        print("\nNothing changed. Re-run with --apply.")
        if not owner_token():
            print("\nAIMEAT_OWNER_TOKEN is not set, and deleting needs an OWNER session. The node's door is")
            print("  requireRoleOrScope('owner', 'agent:delete')")
            print("and for an AGENT caller there is a SECOND condition: the agent's `registeredBy` must be")
            print("the calling principal. Every token under .aimeat/ is an agent token, and the node's")
            print("hatchery is the `registeredBy` of most of the agents above — so granting a fleet agent")
            print("`agent:delete` will NOT unlock them. That is deliberate: the scope alone would hand one")
            print("approved agent the whole fleet. An owner session skips the second condition entirely.")
            print("\nTwo ways to get one:")
            print("  · the dashboard — Profile -> Agents has a Delete per agent (that IS an owner session)")
            print("  · POST /v1/ghii/login returns an owner-role token; then, for the whole sweep at once:")
            print('      $env:AIMEAT_OWNER_TOKEN = "<token>"; uv run crewaimeat orphans --apply')
        return 0

    token = owner_token()
    if not token:
        print("\norphans: --apply needs AIMEAT_OWNER_TOKEN (see the note above)", file=sys.stderr)
        return 2

    node_url, owner = _node_and_owner(root)
    # The hatchery is not a leftover: it is the node feature that MAKES agents, and most of the
    # leftovers here name it as their `registeredBy`. Deleting it would remove the thing that can
    # still account for them, so it is never included in a sweep — only ever on its own, deliberately.
    infra = [o for o in targets if o.name.startswith("hatchery-")]
    targets = [o for o in targets if not o.name.startswith("hatchery-")]
    if infra:
        print(f"\n  holding back {len(infra)} node component(s) — pass the name explicitly to remove one:")
        for o in infra:
            print(f"    · {o.name} (the node's agent hatchery; it registered most of the rest)")

    print(f"\ndeleting {len(targets)} agent(s) as owner '{owner}' on {node_url}:\n")
    failed = 0
    for o in targets:
        ok, detail = delete(o.name, node_url, token)
        failed += 0 if ok else 1
        print(f"  {'ok  ' if ok else 'FAIL'} {o.name}: {detail}")
    print(f"\n{len(targets) - failed} deleted, {failed} failed.")
    if failed:
        print("A 403 here means the token is an AGENT token, not an owner session — see the note above.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Onboard (or end) one Aamukatsaus subscriber.

    uv run python scripts/subscribe_aamukatsaus.py --groups          # find the group id
    uv run python scripts/subscribe_aamukatsaus.py acme-oy grp_xxx   # share the space with it
    uv run python scripts/subscribe_aamukatsaus.py --revoke acme-oy
    uv run python scripts/subscribe_aamukatsaus.py --list

THE WORK IS SPLIT BETWEEN TWO PRINCIPALS, and only the second half runs here.

    OWNER (you, in the browser)   create the group, admit the subscriber's GHII
    AGENT (this script)           share `aamukatsaus.<subscriber>.**` with that group

Measured against the live API 2026-08-14: `POST /v1/groups` and the `aimeat_group_create` MCP tool
both answer an agent token with `ACCESS_DENIED: Role "owner" required`, and MCP does NOT bypass it
— the gate is on the role, not the transport. That boundary is deliberate: an agent may hand out
access to a key space, but it must never assemble its own audience. So the group id is an argument
here rather than something the script creates.

ONE MORE THING NO WILDCARD COVERS: `postman` needs the `share:manage` scope. `scopes:["*"]` is NOT
enough — the node keeps it out of every wildcard so that nobody ticking "full access" is thereby
deciding an agent may publish their memory to strangers. Without it the share call answers
`SCOPE_DENIED`, and this script prints that rather than a generic failure.

After provisioning, the daily write is an ordinary private record and nothing here runs again: `**`
covers keys written later, which is what makes it a subscription rather than a daily re-share.
"""

from __future__ import annotations

import argparse
import json
import sys

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, "src")

from crewaimeat.subscriber_space import (  # noqa: E402
    AGENT,
    OwnerActionRequired,
    _tool,
    pattern_of,
    provision,
    revoke,
    shares_out,
    space_of,
)


def main() -> int:
    ap = argparse.ArgumentParser(description="Onboard or end an Aamukatsaus subscriber.")
    ap.add_argument("subscriber", nargs="?", help="short id, e.g. acme-oy (becomes part of the key AND the share)")
    ap.add_argument("group_id", nargs="?", help="a group the OWNER already created and admitted them to")
    ap.add_argument("--revoke", action="store_true", help="end the subscription (delete the share)")
    ap.add_argument("--list", action="store_true", help="list what we currently share")
    ap.add_argument("--groups", action="store_true", help="list the owner's groups, to find a group id")
    args = ap.parse_args()

    try:
        if args.groups:
            d = _tool("aimeat_group_list", {}) or {}
            groups = d.get("groups") or d.get("items") or []
            print(f"{len(groups)} group(s):")
            for g in groups:
                print(f"  {g.get('id') or g.get('group_id'):24s} {g.get('name') or ''}")
            if not groups:
                print(
                    "  (none) — the OWNER creates a group in the browser and admits the subscriber's\n"
                    '  GHII. An agent token cannot: the node answers ACCESS_DENIED, Role "owner" required.'
                )
            return 0

        if args.list:
            out = shares_out()
            print(f"{len(out)} share(s):")
            for s in out:
                print(f"  {s.get('key_pattern'):40s} group={s.get('group_id') or s.get('group')}")
            return 0

        if not args.subscriber:
            ap.error("subscriber id is required (or use --list / --groups)")
        if args.revoke:
            return 0 if revoke(args.subscriber) else 1

        if not args.group_id:
            ap.error("a group id is required when onboarding — see --groups, and note the OWNER creates it")
        res = provision(args.subscriber, args.group_id)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        print(
            f"\nDaily write from now on:  crewaimeat.subscriber_space.publish({args.subscriber!r}, value)"
            f"\n  -> writes {space_of(args.subscriber)}.<date> as an ORDINARY PRIVATE record"
            f"\n  -> covered by the share {pattern_of(args.subscriber)} without touching it again"
        )
        return 0
    except OwnerActionRequired as exc:
        print(f"{exc}\n\nGrant the scope from the owner's settings for agent {AGENT!r}.", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

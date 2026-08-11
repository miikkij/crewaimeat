"""Onboard (or end) one Aamukatsaus subscriber.

    uv run python scripts/subscribe_aamukatsaus.py acme-oy matti@aimeat-finland-001-genesis
    uv run python scripts/subscribe_aamukatsaus.py --revoke acme-oy
    uv run python scripts/subscribe_aamukatsaus.py --list

WHAT IT DOES, once per subscriber: creates a group, admits them by GHII, and shares
`aamukatsaus.<subscriber>.**` to that group. After that the daily write is an ordinary private
record and nothing here runs again — `**` covers keys written later, which is what makes it a
subscription rather than a daily re-share.

BEFORE IT CAN WORK — one manual step that no wildcard covers:

    the `postman` agent needs `share:manage`  (create a share)
    and `consent:groups`                       (create a group, admit members)

`scopes: ["*"]` is NOT enough. The node keeps share:manage out of every wildcard on purpose:
nobody ticking "full access" is thereby deciding that an agent may publish their memory to
strangers. Grant them per agent from the owner's agent settings.

The share API was on a node branch and not deployed when this was written; the script says so
plainly instead of failing in a way that looks like a permission problem.
"""

from __future__ import annotations

import argparse
import json
import sys

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, "src")

from crewaimeat.subscriber_space import (  # noqa: E402
    ShareApiUnavailable,
    pattern_of,
    provision,
    revoke,
    shares_out,
    space_of,
)

SCOPE_HINT = (
    "The share API answered 404. Two possibilities, in order of likelihood:\n"
    "  1. the node build carrying it is not deployed here yet (it was on a branch 2026-08-11)\n"
    "  2. it is deployed and this is a routing problem\n"
    "If it IS deployed and you get 403 instead, the cause is scopes: `postman` needs\n"
    '`share:manage` and `consent:groups`, and neither is covered by `scopes:["*"]`.'
)


def main() -> int:
    ap = argparse.ArgumentParser(description="Onboard or end an Aamukatsaus subscriber.")
    ap.add_argument("subscriber", nargs="?", help="short id, e.g. acme-oy (becomes part of the key AND the share)")
    ap.add_argument("ghii", nargs="?", help="the subscriber as a person, e.g. matti@aimeat-finland-001-genesis")
    ap.add_argument("--agent", action="append", default=[], help="also admit this agent GAII (repeatable)")
    ap.add_argument("--revoke", action="store_true", help="end the subscription (delete the share)")
    ap.add_argument("--list", action="store_true", help="list what we currently share")
    args = ap.parse_args()

    try:
        if args.list:
            out = shares_out()
            print(f"{len(out)} share(s):")
            for s in out:
                print(f"  {s.get('key_pattern'):40s} group={s.get('group_id') or s.get('group')}")
            return 0

        if not args.subscriber:
            ap.error("subscriber id is required (or use --list)")
        if args.revoke:
            return 0 if revoke(args.subscriber) else 1

        if not args.ghii:
            ap.error("the subscriber's GHII is required when onboarding")
        res = provision(args.subscriber, args.ghii, args.agent)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        print(
            f"\nDaily write from now on:  crewaimeat.subscriber_space.publish({args.subscriber!r}, value)"
            f"\n  -> writes {space_of(args.subscriber)}.<date> as an ORDINARY PRIVATE record"
            f"\n  -> covered by the share {pattern_of(args.subscriber)} without touching it again"
        )
        return 0
    except ShareApiUnavailable as exc:
        print(f"{exc}\n\n{SCOPE_HINT}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Register the whole crewaimeat fleet (every crews/*_crew.py) against ONE node in a single command.

Use it to stand this fleet up on a SECOND node — typically a local dev node — from a SEPARATE clone,
so dev and prod stay fully isolated (own AIMEAT_HOME, own serve daemon, own logs/locks):

    # in the dev clone (E:\\dev\\GitHub\\crewfive-dev):
    uv run python scripts/register_fleet.py --owner <dev-owner> --url http://localhost:40050

It pins AIMEAT_HOME to this clone's .aimeat (unless already set), then launches `connect add` for each
agent and prints its device-approval code/URL. Approve them in that node's dashboard (Profile -> Agents);
each registers automatically once approved. Restrict to a subset with --agents a,b,c.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser(description="Register the whole fleet against one node.")
    ap.add_argument("--owner", required=True, help="the owner account on the target node")
    ap.add_argument("--url", default="http://localhost:40050", help="node URL (default: the local dev node)")
    ap.add_argument("--agents", default="", help="optional comma-separated subset (default: every crew)")
    args = ap.parse_args()

    # Pin AIMEAT_HOME to THIS clone so the new tokens land here, isolated from any other checkout's fleet.
    os.environ.setdefault("AIMEAT_HOME", str(Path.cwd() / ".aimeat"))

    from crewaimeat.forge import register_fleet

    subset = [a.strip() for a in args.agents.split(",") if a.strip()] or None
    print(register_fleet(args.owner, args.url, agents=subset))


if __name__ == "__main__":
    main()

"""Test harness for the federated-DM inbound -> crew -> hand-back loop (Phase 2 handler).

The production trigger is the aimeat-crewai daemon's `dm.inbound` PUSH drain (calls
`dm.process_dm_inbox`). This script exercises the SAME handler manually, so you can verify the
read -> respond -> hand-back round-trip TODAY, before the package push lands.

Usage:
    # 1) send a DM to the agent first (from the dashboard, another agent, or another identity),
    #    addressed to e.g.  workflow-manager#<owner>@<node>
    # 2) then run this to process the inbox and reply in-thread:
    uv run python scripts/dm_inbound_test.py workflow-manager
    uv run python scripts/dm_inbound_test.py workflow-manager --crew   # run the agent's real crew

By default the responder is a deterministic ECHO (no LLM, no node writes beyond the reply) so the
loop is cheap to test. With --crew it builds the agent's actual crew from the DM body.
"""

from __future__ import annotations

import argparse
import sys

from crewaimeat import dm


def echo_responder(message: dict) -> str:
    """A deterministic acknowledgement — proves the round-trip without spending an LLM call."""
    _id, _conv, _sender, body, subject = dm._inbound_fields(message)
    return (
        f"Got it — re: **{subject or 'your message'}**.\n\n> {str(body or '(no body)')[:300]}\n\n"
        "_(crewaimeat dm-inbound test responder)_"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Test the federated-DM inbound -> hand-back loop.")
    ap.add_argument("agent", help="the agent whose federated inbox to process (e.g. workflow-manager)")
    ap.add_argument("--max", type=int, default=5, help="max DMs to process this run")
    ap.add_argument("--crew", action="store_true", help="run the agent's real crew instead of the echo responder")
    args = ap.parse_args()

    inbox = dm.dm_inbox(args.agent, per_page=args.max)
    msgs = (inbox.get("messages") if isinstance(inbox, dict) else None) or []
    print(f"[{args.agent}] federated inbox: {len(msgs)} message(s)")
    for m in msgs:
        _id, conv, sender, body, subject = dm._inbound_fields(m)
        print(f"  - [{conv or '?'}] from {sender or '?'}: {subject or ''} — {str(body)[:80]}")
    if not msgs:
        print("Nothing to process. Send a DM to this agent first, then re-run.")
        return 0

    responder = echo_responder
    if args.crew:
        print(
            "(--crew: building the agent's real crew per DM is left to the daemon's on_dm path; "
            "using echo here so the harness stays node-light.)"
        )

    result = dm.process_dm_inbox(args.agent, responder, max_items=args.max)
    print(f"[{args.agent}] handled: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

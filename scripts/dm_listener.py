"""Long-running federated-DM listener — the PRODUCTION inbound trigger, standalone.

Drains the serve daemon's `/local/dm/next` loopback queue (v1.30.2+) and replies to each DM IN-THREAD.
Event-driven: the long-poll parks on the node's push, so it makes ZERO node calls while idle. Run it next
to a fleet (the serve daemon must be up). Ctrl+C to stop.

This is the trigger until aimeat-crewai's `run_crew_daemon` grows an `on_dm` drain (then it moves into the
daemon and one coordinator agent gets `listen_for=("tasks","dms")`). The hand-back (`dm_reply`) is shared.

Usage:
    uv run python scripts/dm_listener.py workflow-manager
"""

from __future__ import annotations

import argparse
import sys

from crewaimeat import dm


def echo_responder(event: dict) -> str:
    """Deterministic acknowledgement — proves the trigger without an LLM call. A real responder would build
    the agent's crew from dm_thread(agent, conversationId) and return its deliverable text."""
    _id, _conv, _sender, body, subject = dm._inbound_fields(event)
    return f"Got it — re: **{subject or 'your message'}**.\n\n> {str(body or '')[:300]}\n\n_(crewaimeat dm listener)_"


def main() -> int:
    ap = argparse.ArgumentParser(description="Long-running federated-DM listener (production inbound trigger).")
    ap.add_argument("agent", help="the agent whose federated inbox to serve (e.g. workflow-manager)")
    ap.add_argument("--wait-ms", type=int, default=5000, help="long-poll wait per drain")
    args = ap.parse_args()
    print(f"[{args.agent}] dm listener: parking on /local/dm/next (Ctrl+C to stop) ...")
    try:
        dm.run_dm_listener(args.agent, echo_responder, wait_ms=args.wait_ms)
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Detect and clear a wake-queue spin on the shared serve daemon.

THE MECHANISM, measured by the agenttihautomo team on this machine 2026-08-17 and reported to us.
`/local/wake/next` is LEVEL-triggered: it answers 200 whenever the agent's queue holds an element,
and consumes nothing. The daemon then lists tasks from the store and leaves the queue alone. So one
task push stays queued forever, every wake returns immediately, and the daemon's idle wait collapses
to zero.

What that cost, measured: ONE stuck agent of 61 (crypto-weekly-reporter) produced 28 req/s against
`/v1/agents/:name/tasks` and 76 % of a CPU. Shutting the fleet down took the node from 35 req/s to
0.3 req/s. With the spin on, fleet_host sat at 33.1 % of a core and the serve daemon at 23.2 %;
after ONE element was consumed, 0.1 % and 0.8 %.

The defect is in aimeat-protocol and is being fixed there. This is the mitigation, and it is safe
for one reason: the node's store is the source of truth for tasks and the daemon re-lists them every
cycle — the connector's own code says so. Consuming a queue element therefore drops a redundant
REPEAT of an event, never the event.

TWO RULES THIS MODULE EXISTS TO ENFORCE:

  1. NEVER drain a fresh wake. Eat a push before the daemon has seen it and the task waits for the
     ~5-minute safety net instead of running now. Only an agent that has read 200 for longer than
     `MIN_STUCK_SECONDS` is drained.
  2. Drain to 204, not once. A queue holding several repeats spins just as hard after one is taken.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

MIN_STUCK_SECONDS = 90  # a wake younger than this may be a real push the daemon has not read yet
MAX_DRAIN = 50  # per agent per pass — a bound, so a genuinely busy queue cannot hold the watchdog
_TIMEOUT = 5


def _code(url: str) -> int | None:
    """HTTP status, or None when the daemon does not answer at all."""
    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT) as r:  # noqa: S310 — loopback, our own daemon
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:  # noqa: BLE001 — daemon down / restarting: not this module's problem
        return None


def is_spinning(port: int, agent: str) -> bool:
    """True when the agent's wake answers immediately — 200 with `wait=0` means an element is queued."""
    return _code(f"http://127.0.0.1:{port}/local/wake/next?wait=0&agent={agent}") == 200


def drain(port: int, agent: str, *, max_items: int = MAX_DRAIN) -> int:
    """Consume queued elements until the queue is empty (204). Returns how many were taken."""
    taken = 0
    while taken < max_items:
        code = _code(f"http://127.0.0.1:{port}/local/tasks/next?wait=0&agent={agent}")
        if code != 200:  # 204 = clean, None = daemon gone
            break
        taken += 1
    return taken


def agents_of(serve_json: Path) -> tuple[int | None, list[str]]:
    """(port, agent names) from a serve.json, or (None, []) when it is absent or unreadable."""
    try:
        doc = json.loads(serve_json.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — a half-written file during daemon start is normal
        return None, []
    names = [a.get("agent") for a in (doc.get("agents") or []) if isinstance(a, dict) and a.get("agent")]
    return doc.get("port"), names


class SpinSweeper:
    """One pass per watchdog cycle. Remembers when each agent's spin was FIRST seen, so a fresh
    push is left alone and only a queue that has been hot for MIN_STUCK_SECONDS is drained."""

    def __init__(self, min_stuck: int = MIN_STUCK_SECONDS):
        self.min_stuck = min_stuck
        self._since: dict[str, float] = {}

    def sweep(self, port: int, agents: list[str], *, now: float | None = None) -> list[tuple[str, int]]:
        """Probe every agent; drain the ones stuck long enough. Returns [(agent, items_taken)]."""
        now = time.time() if now is None else now
        cleared: list[tuple[str, int]] = []
        for agent in agents:
            if not is_spinning(port, agent):
                self._since.pop(agent, None)  # settled on its own: forget it, so the clock restarts
                continue
            first = self._since.setdefault(agent, now)
            if now - first < self.min_stuck:
                continue  # RULE 1: too young to be sure it is a spin rather than a real push
            took = drain(port, agent)
            self._since.pop(agent, None)
            if took:
                cleared.append((agent, took))
        return cleared

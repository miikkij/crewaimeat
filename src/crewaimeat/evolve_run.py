"""Background runner for one self-evolution (doc 20, Phase 3).

Launched DETACHED by an agent when the owner clicks "Evolve to the next level", so the slow work
(design the candidate -> stage + validate -> A/B against the agent on its own tasks -> message the
result) never blocks the agent's daemon. The agent always gets a message back with the outcome.

    python -m crewaimeat.evolve_run <agent> <ctx>
"""
from __future__ import annotations

import sys

try:
    from dotenv import load_dotenv

    load_dotenv()  # OPENROUTER_API_KEY / AIMEAT_OWNER etc. for the design LLM + registration
except Exception:  # noqa: BLE001
    pass

from crewaimeat.evolve import run_evolution


def main() -> None:
    agent = sys.argv[1] if len(sys.argv) > 1 else None
    ctx = sys.argv[2] if len(sys.argv) > 2 else "creative"
    if not agent:
        print("usage: python -m crewaimeat.evolve_run <agent> <ctx>", file=sys.stderr)
        return
    run_evolution(agent, ctx)


if __name__ == "__main__":
    main()

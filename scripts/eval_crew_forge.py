"""Behavioral eval for crew-forge — does it really build the agent it was asked for?

Runs crew-forge's Architect+Builder over a corpus of plain-language orders (crewaimeat.forge_eval.ORDERS)
in DRY-RUN mode: each generated crew is written into a throwaway temp dir and validated, but registration
and launch are neutralized, so nothing ever touches aimeat.io or the running fleet. Each generated crew
is graded on: did it build (validate)? did it wire the RIGHT tools (expected, none forbidden)? does it
have a sensible shape and consume the request? A scorecard is printed; exit code is non-zero if any order
failed.

This needs an LLM key (OPENROUTER_API_KEY) + network — it runs the real Architect. It is opt-in and NOT
part of the deterministic test floor (the pure grader is unit-tested there instead).

Usage:
    uv run python scripts/eval_crew_forge.py                 # run the whole corpus
    uv run python scripts/eval_crew_forge.py --order web-research   # one order
    uv run python scripts/eval_crew_forge.py --model openai/gpt-oss-120b   # override the model
    uv run python scripts/eval_crew_forge.py --keep         # keep the generated crews for inspection
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT),):
    if _p not in sys.path:
        sys.path.insert(0, _p)

load_dotenv()


def main() -> int:
    from crewaimeat import forge_eval

    ap = argparse.ArgumentParser(description="Behavioral eval for crew-forge (dry-run, no register/launch).")
    ap.add_argument("--order", help="run only this order id (default: the whole corpus)")
    ap.add_argument("--model", help="override the model id the Architect/Builder run on")
    ap.add_argument("--keep", action="store_true", help="keep the generated crews (print the temp dir)")
    args = ap.parse_args()

    orders = forge_eval.ORDERS
    if args.order:
        orders = [o for o in orders if o.id == args.order]
        if not orders:
            print(f"No order with id '{args.order}'. Known: {', '.join(o.id for o in forge_eval.ORDERS)}")
            return 2

    # crew_forge_crew.build_domain is the real thing under test; import it from the crews/ tree.
    from crews.crew_forge_crew import build_domain

    root = Path(tempfile.mkdtemp(prefix="crewforge-eval-"))
    print(f"Running {len(orders)} order(s) into {root} (dry-run — nothing is registered or launched)\n")
    grades = forge_eval.run_eval(build_domain, orders, root=root, model=args.model)
    print(forge_eval.format_scorecard(grades))
    if args.keep:
        print(f"\nGenerated crews kept in: {root}")
    return 0 if all(g.passed for g in grades) else 1


if __name__ == "__main__":
    raise SystemExit(main())

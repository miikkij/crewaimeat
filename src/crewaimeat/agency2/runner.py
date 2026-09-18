"""`python -m crewaimeat.agency2.runner <name>` — one local agent's runtime.

The definition is the node's (`crews.registry.<name>`); on the very first start a staged
`crew_defs/<name>.json` in the working directory is published by the agent itself
(`json_agent.seed_from_staged`). The BARE name is passed on purpose: a GAII changes the staged file name
and fails its name check (spec §7 V0 finding 1).
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or "#" in args[0] or "@" in args[0]:
        raise SystemExit("usage: python -m crewaimeat.agency2.runner <bare-agent-name>")
    from crewaimeat.agency2.engine import openrouter_only

    openrouter_only()
    from crewaimeat.json_agent import run_json_agent

    run_json_agent(args[0])


if __name__ == "__main__":
    main()

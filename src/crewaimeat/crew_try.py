"""`crewaimeat try` — run a JSON crew definition ONCE, locally, before it is anything.

The bench. Today the only way to find out whether a crew def works is to register the agent, wait
for the device-flow approval and restart the whole fleet — ten minutes and a fleet outage per typo.
That loop is why `crew_defs/` has three files and `crews/` has sixty: the declarative path is not
worse, it is just more expensive to TRY, and cost per attempt is what decides which path people take.

This makes an attempt cost seconds:

    crewaimeat try crew_defs/joker.json --prompt "kissoista"
    crewaimeat try mydef.json --prompt "…" --as web-researcher   # borrow a live identity for tools
    crewaimeat try mydef.json --check                            # validate only, no model call

It runs the REAL interpreter (`crew_def.build_domain_from_json`) and a REAL crewai kickoff, so what
you see is what the fleet would do. What it deliberately does NOT do: register an agent, publish an
offer, write to node memory, or touch the fleet. Nothing to clean up afterwards — a trial whose
traces you have to sweep is not a trial.

TOOLS AND IDENTITY. A def's tools (`memory`, `web`, `schedule`, …) reach the node through the shared
serve daemon under an agent's token, so an unregistered `agent_name` has no identity to call with.
`--as <agent>` borrows a registered one for the run: the crew is the doc's, the credentials are
somebody else's. A def with no node tools needs neither.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _print_errors(errors: list[str], path: Path) -> None:
    print(f"INVALID: {path} has {len(errors)} problem(s)\n", file=sys.stderr)
    for e in errors:
        print(f"  - {e}", file=sys.stderr)
    print(
        "\nNothing was built. Fix the definition and run again — this is the same check that runs "
        "before the fleet ever sees it.",
        file=sys.stderr,
    )


def _summary(doc: dict) -> str:
    agents = doc.get("agents") or []
    tasks = doc.get("tasks") or []
    tools = sorted({t for a in agents if isinstance(a, dict) for t in (a.get("tools") or [])})
    return (
        f"{doc.get('agent_name')}: {len(agents)} agent(s), {len(tasks)} task(s)"
        + (f", tools: {', '.join(tools)}" if tools else ", no tools")
        + (f", profile: {doc['llm_profile']}" if doc.get("llm_profile") else "")
    )


def try_crew(
    path: str | Path,
    prompt: str,
    *,
    as_agent: str | None = None,
    check_only: bool = False,
    verbose: bool = False,
) -> int:
    """Validate a crew def and, unless `check_only`, run it once. Returns a process exit code."""
    from crewaimeat.crew_def import build_domain_from_json, load_crew_doc, validate_crew_doc

    p = Path(path)
    try:
        doc = load_crew_doc(p)
    except (OSError, ValueError) as exc:
        print(f"FAILED to read {p}: {exc}", file=sys.stderr)
        return 2

    errors = validate_crew_doc(doc)
    if errors:
        _print_errors(errors, p)
        return 1

    print(f"VALID  {_summary(doc)}", file=sys.stderr)
    if check_only:
        return 0
    if not prompt.strip():
        print("FAILED: --prompt is required to run (use --check to validate only).", file=sys.stderr)
        return 2

    # The identity the TOOLS call the node with. The crew is the doc's either way; only the
    # credentials are borrowed, and only when asked for.
    identity = as_agent or str(doc.get("agent_name") or "")
    from crewaimeat.aimeat_crew import BuildContext, _now_context
    from crewaimeat.llm import get_llm, resolved_model

    llm = get_llm(
        for_tool_use=bool(any((a or {}).get("tools") for a in doc.get("agents") or [])),
        temperature=doc.get("temperature"),
        agent_name=identity,
    )
    ctx = BuildContext(
        task={"id": "try", "title": "local try", "description": prompt},
        prompt=prompt,
        llm=llm,
        today=_now_context(),
        directives="",
    )

    doc_for_tools = dict(doc, agent_name=identity) if as_agent else doc
    try:
        agents, tasks = build_domain_from_json(doc_for_tools, ctx)
    except Exception as exc:  # noqa: BLE001 — a construction failure IS the answer here
        print(f"FAILED to build: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    from crewai import Crew, Process

    crew = Crew(
        agents=agents,
        tasks=tasks,
        process=Process.hierarchical if doc.get("process") == "hierarchical" else Process.sequential,
        verbose=verbose,
    )
    print(
        f"RUN    as {identity!r} on {resolved_model(llm) or 'the routed model'} — nothing is "
        "registered, published or written.\n",
        file=sys.stderr,
    )
    try:
        result = crew.kickoff()
    except Exception as exc:  # noqa: BLE001 — surface the real cause, do not dress it up
        print(f"\nFAILED at kickoff: {type(exc).__name__}: {exc}", file=sys.stderr)
        if identity and not as_agent:
            print(
                f"If this is a node/tool error, {identity!r} may not be a registered agent — "
                "run again with --as <a registered agent> to borrow an identity for the tools.",
                file=sys.stderr,
            )
        return 1

    print(str(result))
    print(f"\nOK     {len(str(result))} chars from {resolved_model(llm) or '?'}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="crewaimeat try",
        description="Validate a JSON crew definition and run it once, locally. Registers nothing.",
    )
    ap.add_argument("path", help="crew_defs/<name>.json")
    ap.add_argument("--prompt", default="", help="the request the crew receives as {{ctx.prompt}}")
    ap.add_argument("--as", dest="as_agent", default=None, help="registered agent whose identity the TOOLS use")
    ap.add_argument("--check", action="store_true", help="validate only — no model call")
    ap.add_argument("--verbose", action="store_true", help="crewai's own step-by-step output")
    a = ap.parse_args(argv)
    return try_crew(a.path, a.prompt, as_agent=a.as_agent, check_only=a.check, verbose=a.verbose)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

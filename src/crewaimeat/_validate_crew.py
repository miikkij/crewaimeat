"""Validate a generated crew file in a fresh subprocess.

Usage:  python -m crewaimeat._validate_crew crews/<name>_crew.py

Imports the candidate file by path (surfacing any syntax / import error), calls its
build_domain() with a dummy BuildContext, and checks it returns a non-empty
(agents, tasks) of crewai Agent / Task objects. Exits 0 and prints "VALID: ..." on
success; exits non-zero and prints "FAIL: ..." otherwise. Run in a subprocess so a
broken candidate cannot pollute the daemon's process or its import cache.
"""

from __future__ import annotations

import importlib.util
import sys
import traceback


def _is_toollike(t: object) -> bool:
    """True if `t` is a real crewai tool object, not a raw container. Catches the classic mistake of
    attaching a factory's (tools, state) tuple without unpacking (a list/dict/tuple/str slips in)."""
    return not isinstance(t, (list, tuple, dict, str)) and hasattr(t, "name")


def _validate(path: str) -> tuple[bool, str]:
    spec = importlib.util.spec_from_file_location("_candidate_crew", path)
    if spec is None or spec.loader is None:
        return False, f"could not load {path}"
    module = importlib.util.module_from_spec(spec)
    # exec_module runs the file's top level; `run()` does NOT fire because __name__
    # is "_candidate_crew", not "__main__".
    spec.loader.exec_module(module)

    if not hasattr(module, "build_domain"):
        return False, "the file defines no build_domain(ctx) function"
    if not hasattr(module, "AGENT_NAME") or not str(module.AGENT_NAME).strip():
        return False, "AGENT_NAME is missing or empty"

    from crewai import Agent, Task

    from crewaimeat.aimeat_crew import BuildContext

    ctx = BuildContext(task={}, prompt="(sample task text)", llm=None, today="(today)")
    result = module.build_domain(ctx)

    if not (isinstance(result, (list, tuple)) and len(result) == 2):
        return False, "build_domain must return a 2-tuple (agents, tasks)"
    agents, tasks = result
    if not (isinstance(agents, (list, tuple)) and agents):
        return False, "build_domain must return a non-empty list of agents first"
    if not (isinstance(tasks, (list, tuple)) and tasks):
        return False, "build_domain must return a non-empty list of tasks second"
    bad_agent = next((a for a in agents if not isinstance(a, Agent)), None)
    if bad_agent is not None:
        return False, f"every agent must be a crewai Agent (got {type(bad_agent).__name__})"
    bad_task = next((t for t in tasks if not isinstance(t, Task)), None)
    if bad_task is not None:
        return False, f"every task must be a crewai Task (got {type(bad_task).__name__})"

    # Tool sanity: every entry an agent carries in `tools` must be a real tool object, not a raw
    # container. The classic mistake is attaching a factory's (tools, state) tuple without unpacking
    # (tools=make_x(...) instead of tools=[*make_x(...)]), or splatting a tuple so a state dict lands
    # in the list. Catch it here with a clear message instead of a confusing failure at kickoff.
    for a in agents:
        for t in getattr(a, "tools", None) or []:
            if not _is_toollike(t):
                role = getattr(a, "role", "?")
                return False, (
                    f"agent '{role}' has a non-tool in its tools ({type(t).__name__}) — a tool factory "
                    "likely returned (tools, state) and was not unpacked; use tools=[*make_x(AGENT_NAME)]"
                )

    return True, f"{len(agents)} agents, {len(tasks)} tasks"


def main() -> int:
    if len(sys.argv) != 2:
        print("FAIL: usage: python -m crewaimeat._validate_crew <crew_file>")
        return 2
    try:
        ok, detail = _validate(sys.argv[1])
    except Exception as exc:  # noqa: BLE001 — any failure means the candidate is invalid
        tb = traceback.format_exc(limit=4).strip().splitlines()
        # Keep the last few lines: the actual error is what the author must fix.
        print(f"FAIL: {type(exc).__name__}: {exc}\n" + "\n".join(tb[-6:]))
        return 1
    print(("VALID: " if ok else "FAIL: ") + detail)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Answer the node's `crew.validate` and `crew.try` — the two buttons in the Crew tab.

The Crew tab has to tell a person whether their definition is valid, and let them run it once before
publishing. Both answers can only come from HERE: `validate_crew_doc` is the one validator (a second
one in JS would drift and hand out green lights for definitions that fail at run time), and a trial
has to build the real crew with the real tools.

THE TRANSPORT IS NOT OURS. The node asks over the connector tunnel's server-initiated `invoke` frame,
and `aimeat-crewai>=0.22.0` serves that queue: `run_crew_daemon(on_invoke=...)` polls from the moment
the agent starts, runs handlers in a small pool, answers HANDLER_ERROR when one raises, and reports
an older serve daemon once instead of spinning on 404s. This module wrote all of that by hand for a
day and it is deleted: the package's version polls from startup for the same reason (the daemon
answers the node NO_HANDLER for an agent nobody has polled in 90 s) and is BETTER, because a
minutes-long `crew.try` runs in the pool instead of blocking the `crew.validate` behind it.

What stays here is the part the package cannot know: what a crewaimeat definition means. `handle()`
is that, and `CrewSpec.on_invoke` is where it plugs in.

A TRIAL LEAVES NOTHING BEHIND. No task, no memory write, no offer — the same promise `crewaimeat try`
makes, because it is the same code path. The node holds the result in memory for a quarter of an hour
and never stores it.

FAILURE IS AN ANSWER TOO. Every capability replies — a build that raises, a model that errors, an
unknown capability — because a button that spins forever is worse than one that says why.
"""

from __future__ import annotations

import time
from typing import Any

# The node's own ceilings (spec: validate 30 s, try AIMEAT_CREW_TRY_TIMEOUT_MS, default 5 min). Ours
# are the same numbers seen from this side: a reply the node has stopped waiting for is wasted work.
_POLL_WAIT_MS = 25_000  # long-poll; the daemon answers 204 when nothing arrives
_RETRY_S = 5.0  # after a transport error, before parking again
_CAPABILITIES = ("crew.validate", "crew.try")


def handle(capability: str, payload: dict, *, agent_name: str) -> tuple[bool, dict]:
    """Answer one invoke. Returns `(ok, result)` exactly as the daemon wants to post it back.

    Pure apart from the model call inside `crew.try`, so the whole contract is unit-testable without
    a node: the transport is somebody else's problem, this is the meaning.
    """
    doc = payload.get("doc") if isinstance(payload, dict) else None
    if not isinstance(doc, dict):
        return False, {"code": "BAD_INPUT", "message": "input.doc must be the crew definition object"}

    if capability == "crew.validate":
        from crewaimeat.crew_def import validate_crew_doc

        return True, {"errors": validate_crew_doc(doc)}

    if capability == "crew.try":
        from crewaimeat.crew_def import validate_crew_doc

        errors = validate_crew_doc(doc)
        if errors:
            # Running an invalid definition would fail somewhere deep in crewai with a worse message
            # than the one the validator already has.
            return False, {"code": "INVALID", "message": "; ".join(errors), "errors": errors}
        prompt = str((payload or {}).get("prompt") or "").strip()
        if not prompt:
            return False, {"code": "BAD_INPUT", "message": "input.prompt is required to run a trial"}
        return _run_trial(doc, prompt, agent_name=agent_name)

    return False, {"code": "UNKNOWN_CAPABILITY", "message": f"{capability!r} is not one of {list(_CAPABILITIES)}"}


def _run_trial(doc: dict, prompt: str, *, agent_name: str) -> tuple[bool, dict]:
    """Build the crew and run it ONCE. The identity is this agent's own — the invoke arrived on its
    tunnel — so the definition under trial may use node tools without borrowing anybody."""
    from crewai import Crew, Process

    from crewaimeat.aimeat_crew import BuildContext, _now_context
    from crewaimeat.crew_def import build_domain_from_json
    from crewaimeat.llm import get_llm, resolved_model

    started = time.monotonic()
    try:
        llm = get_llm(
            for_tool_use=bool(any((a or {}).get("tools") for a in doc.get("agents") or [])),
            temperature=doc.get("temperature"),
            agent_name=agent_name,
        )
        # The trial runs the DOCUMENT's crew under THIS agent's credentials: the tools must call the
        # node as somebody, and the only identity we may use is the one the invoke came in on.
        agents, tasks = build_domain_from_json(
            dict(doc, agent_name=agent_name),
            BuildContext(
                task={"id": "crew.try", "title": "trial", "description": prompt},
                prompt=prompt,
                llm=llm,
                today=_now_context(),
                directives="",
            ),
        )
        crew = Crew(
            agents=agents,
            tasks=tasks,
            process=Process.hierarchical if doc.get("process") == "hierarchical" else Process.sequential,
            verbose=False,
        )
        output = str(crew.kickoff())
    except Exception as exc:  # noqa: BLE001 — the failure IS the answer; a spinning button is worse
        return False, {
            "code": "TRIAL_FAILED",
            "message": f"{type(exc).__name__}: {exc}",
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
    return True, {
        "output": output,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "model": resolved_model(llm),
    }


def on_invoke(capability: str, payload: Any, invoke: dict | None = None) -> tuple[bool, dict]:
    """`CrewSpec.on_invoke` adapter — the package's handler signature, our meaning.

    `(capability, input, invoke) -> (ok, result)`. The agent name comes from the frame rather than a
    closure: the daemon serves one agent per listener, and reading it from the frame keeps this a
    plain function that the fleet can pass straight through.
    """
    agent_name = str((invoke or {}).get("agent") or "")
    return handle(capability, payload if isinstance(payload, dict) else {}, agent_name=agent_name)

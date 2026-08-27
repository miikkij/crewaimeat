"""Answer the node's `crew.validate` and `crew.try` — the two buttons in the Crew tab.

The Crew tab has to tell a person whether their definition is valid, and let them run it once before
publishing. Both answers can only come from HERE: `validate_crew_doc` is the one validator (a second
one in JS would drift and hand out green lights for definitions that fail at run time), and a trial
has to build the real crew with the real tools.

So the node asks. The transport is the connector tunnel's server-initiated `invoke` frame — already
built, id-correlated and time-bounded — surfaced to us by the serve daemon as a long-poll queue, the
same shape tasks/records/dms already use (living spec doc-mtc3ztsbxn9n, answer A):

    GET  /local/invoke/next?wait=<ms>&agent=<name>  -> {id, capability, input, caller, timeout_ms} | 204
    POST /local/invoke/<id>/result                  <- {ok, result}

A TRIAL LEAVES NOTHING BEHIND. No task, no memory write, no offer — the same promise `crewaimeat try`
makes, because it is the same code path. The node holds the result in memory for a quarter of an hour
and never stores it.

WHY THIS RUNS AS A THREAD BESIDE THE AGENT, not as part of its task loop: a person clicking Validate
is not work the agent queues, it is a question about a document they have not published yet. It must
answer in seconds while the agent may be busy for minutes on a real task, and it must answer for a
document that is NOT the agent's current definition. Sharing the task loop would make the button wait
for whatever the agent happens to be writing.

FAILURE IS AN ANSWER TOO. Every capability replies — a build that raises, a model that errors, an
unknown capability — because a button that spins forever is worse than one that says why. The only
thing that stays silent is a queue that is empty (204) or a daemon too old to have the endpoint,
which is reported once and then left alone.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Any

# The node's own ceilings (spec: validate 30 s, try AIMEAT_CREW_TRY_TIMEOUT_MS, default 5 min). Ours
# are the same numbers seen from this side: a reply the node has stopped waiting for is wasted work.
_POLL_WAIT_MS = 25_000  # long-poll; the daemon answers 204 when nothing arrives
_RETRY_S = 5.0  # after a transport error, before parking again
_CAPABILITIES = ("crew.validate", "crew.try")


def _serve_base() -> str:
    from aimeat_crewai.mcp_client import _read_discovery, serve_discovery_path

    doc = _read_discovery(serve_discovery_path())
    if not doc or not doc.get("port"):
        raise RuntimeError("no serve daemon discovery file — the fleet's loopback daemon is not running")
    return f"http://127.0.0.1:{doc['port']}"


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


def serve_invokes(agent_name: str, *, stop: threading.Event | None = None, poll_wait_ms: int = _POLL_WAIT_MS) -> None:
    """Park on the invoke queue for `agent_name` and answer until `stop` is set.

    Runs forever by design — it is the agent's side of a button somebody may press at any time.
    """
    import requests

    stop = stop or threading.Event()
    try:
        base = _serve_base()
    except Exception as exc:  # noqa: BLE001 — no daemon means no buttons; the agent's own work is fine
        print(f"[{agent_name}] invoke: {exc}; the Crew tab's buttons will not reach this agent.", file=sys.stderr)
        return

    session = requests.Session()
    session.headers.update({"X-Aimeat-Agent": agent_name})
    said_missing = False
    print(f"[{agent_name}] invoke: answering crew.validate + crew.try on {base}", file=sys.stderr)

    while not stop.is_set():
        try:
            r = session.get(
                f"{base}/local/invoke/next",
                params={"wait": poll_wait_ms, "agent": agent_name},
                timeout=(poll_wait_ms / 1000) + 10,
            )
        except Exception as exc:  # noqa: BLE001 — a blip: wait and park again
            print(f"[{agent_name}] invoke poll failed ({type(exc).__name__}: {exc}); retrying", file=sys.stderr)
            stop.wait(_RETRY_S)
            continue

        if r.status_code == 404:
            # An older connector without the invoke surface. Said ONCE — repeating it every 25 s
            # would bury the log of an agent that is otherwise working perfectly well.
            if not said_missing:
                print(
                    f"[{agent_name}] invoke: this serve daemon has no /local/invoke/next "
                    "(older connector) — Validate and Try in the Crew tab cannot reach this agent "
                    "until it is updated. Everything else works.",
                    file=sys.stderr,
                )
                said_missing = True
            stop.wait(60.0)
            continue
        if r.status_code == 204 or not r.content:
            continue
        if r.status_code >= 400:
            print(f"[{agent_name}] invoke poll HTTP {r.status_code}; retrying", file=sys.stderr)
            stop.wait(_RETRY_S)
            continue

        try:
            frame = r.json()
        except ValueError:
            print(f"[{agent_name}] invoke: reply was not JSON; skipping", file=sys.stderr)
            continue
        _answer(session, base, frame, agent_name)


def _answer(session: Any, base: str, frame: dict, agent_name: str) -> None:
    """Run one frame and post the result. An id we cannot answer is still an answer we must post."""
    invoke_id = str((frame or {}).get("id") or "")
    capability = str((frame or {}).get("capability") or "")
    if not invoke_id:
        print(f"[{agent_name}] invoke: frame without an id; dropped", file=sys.stderr)
        return

    print(f"[{agent_name}] invoke {invoke_id[:8]} {capability}", file=sys.stderr)
    try:
        ok, result = handle(capability, frame.get("input") or {}, agent_name=agent_name)
    except Exception as exc:  # noqa: BLE001 — never leave the caller waiting on our own bug
        ok, result = False, {"code": "HANDLER_CRASHED", "message": f"{type(exc).__name__}: {exc}"}

    try:
        session.post(f"{base}/local/invoke/{invoke_id}/result", json={"ok": ok, "result": result}, timeout=30)
    except Exception as exc:  # noqa: BLE001 — the node times the call out; say why it went unanswered
        print(f"[{agent_name}] invoke {invoke_id[:8]}: could not post result ({exc})", file=sys.stderr)
        return
    summary = "ok" if ok else f"failed: {result.get('code')}"
    print(f"[{agent_name}] invoke {invoke_id[:8]} {capability} -> {summary}", file=sys.stderr)


def start_invoke_thread(agent_name: str) -> threading.Event:
    """Answer invokes beside the agent's own work. Returns the stop event; the thread is a daemon so
    it never keeps the fleet alive on its own."""
    stop = threading.Event()
    threading.Thread(
        target=serve_invokes,
        args=(agent_name,),
        kwargs={"stop": stop},
        name=f"invoke:{agent_name}",
        daemon=True,
    ).start()
    return stop

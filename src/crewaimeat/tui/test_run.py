"""Live agent test — fire a real task at a RUNNING agent and wait for its deliverable.

This is the "does it actually work?" path: it does NOT run the crew in-process. It creates a real
AIMEAT task targeted at the agent (so the agent's own daemon picks it up, on its real model, over
the real serve tunnel) and polls the agent's memory until its `…latest_output` lands — the same
fan-out + fan-in plumbing collab_smoke_test.py exercises. All deterministic; no LLM here.

Used by the TUI Test tab off the UI thread. `on_update(str)` streams progress lines so the pane can
show "waiting … 15s" while the agent works.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


def _find_id(obj) -> str | None:
    """Pull the created task's UUID out of whatever shape task_create returns."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in ("id", "task_id", "taskid") and isinstance(v, str) and _UUID.search(v):
                return _UUID.search(v).group(0)
        for v in obj.values():
            r = _find_id(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_id(v)
            if r:
                return r
    return None


def _keys_of(mem_list_resp) -> list[str]:
    if not isinstance(mem_list_resp, dict):
        return []
    for container in (mem_list_resp, mem_list_resp.get("data") or {}):
        if not isinstance(container, dict):
            continue
        for field in ("items", "keys", "memories", "entries"):
            arr = container.get(field)
            if isinstance(arr, list):
                out = []
                for it in arr:
                    if isinstance(it, str):
                        out.append(it)
                    elif isinstance(it, dict) and it.get("key"):
                        out.append(it["key"])
                if out:
                    return out
    return []


def _read_deliverable(call, agent: str, short: str):
    """The agent's deliverable for this subtask (memory key carries the task short id), or None."""
    for k in _keys_of(call(agent, "aimeat_memory_list", {})):
        if k.startswith(f"crews.{agent}.") and short in k and k.endswith("latest_output"):
            r = call(agent, "aimeat_memory_read", {"key": k})
            val = r.get("value") if isinstance(r, dict) else r
            if val:
                return k, val
    return None, None


def run_agent_test(
    target: str,
    prompt: str,
    *,
    creator: str | None = None,
    on_update: Callable[[str], None] | None = None,
    timeout_s: int = 180,
    poll_s: int = 5,
    call=None,
) -> dict:
    """Create a live task for `target` with `prompt` and poll until its deliverable lands.

    Returns {ok, task_id, key, result, error, elapsed_s}. `creator` defaults to the target itself
    (a self-task — one token, the very agent under test); pass another agent to delegate as it would
    in a real fan-out. `call` is the dispatcher (defaults to aimeat_crew._aimeat_call) — injectable
    for tests.
    """
    if call is None:
        from crewaimeat.aimeat_crew import _aimeat_call as call
    creator = creator or target

    def emit(msg: str) -> None:
        if on_update:
            on_update(msg)

    emit(f"creating task for {target}…")
    resp = call(
        creator,
        "aimeat_task_create",
        {
            "target_agent": target,
            "title": f"TUI test: {prompt[:48]}",
            "description": prompt,
            "status": "queued",
        },
    )
    tid = _find_id(resp)
    if not tid:
        return {
            "ok": False,
            "task_id": None,
            "key": None,
            "result": None,
            "error": "task_create returned no task id (is the creator agent authorized?)",
            "elapsed_s": 0,
        }
    short = tid.split("-", 1)[0]
    emit(f"task {tid} queued; waiting for {target} (timeout {timeout_s}s)…")

    waited = 0
    while waited < timeout_s:
        key, val = _read_deliverable(call, target, short)
        if val:
            return {"ok": True, "task_id": tid, "key": key, "result": str(val), "error": None, "elapsed_s": waited}
        time.sleep(poll_s)
        waited += poll_s
        emit(f"waiting on {target}… {waited}s")
    return {
        "ok": False,
        "task_id": tid,
        "key": None,
        "result": None,
        "error": f"timeout after {timeout_s}s — no deliverable from {target}",
        "elapsed_s": waited,
    }

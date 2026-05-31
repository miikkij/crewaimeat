"""Tier-0 collaboration smoke test (deterministic, no LLM coordinator).

Proves the AIMEAT collaboration plumbing with the crews you already have:
  1. fan-out  : create a subtask for two OTHER same-owner crews via aimeat_task_create
  2. parallel : both crews run concurrently (their own daemons pick the tasks up)
  3. fan-in   : poll each crew's memory until its deliverable appears, then read it
  4. synthesis: assemble both into one "Tapiola Weekly" page and publish to memory

It acts as `crew-forge` (its stored token) to create + publish. Workers are untouched.
Run:  uv run python collab_smoke_test.py
"""

from __future__ import annotations

import json
import re
import sys
import time

from crewaimeat.aimeat_crew import _aimeat_call

COORDINATOR = "crew-forge"  # whose token we use to create tasks + publish
POLL_SECONDS = 15
TIMEOUT_SECONDS = 600

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)

# Two jobs for two existing crews -> material for a mini newspaper page.
JOBS = [
    {
        "agent": "probability-creator",
        "title": "Tapiola week outlook",
        "description": (
            "List 5 plausible things people in Tapiola, Espoo might do or experience next week, "
            "from a 0% longshot to the 100% obvious. Keep each to one short line."
        ),
        "section": "Week outlook",
    },
    {
        "agent": "joker",
        "title": "Tapiola weather desk",
        "description": "Tell ONE short one-line joke about the weather in Tapiola, Espoo.",
        "section": "Weather desk",
    },
]


def _find_id(obj) -> str | None:
    """Pull the created task's UUID out of whatever shape the response uses."""
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


def _keys_of(memory_list_resp) -> list[str]:
    if not isinstance(memory_list_resp, dict):
        return []
    for container in (memory_list_resp, memory_list_resp.get("data") or {}):
        for field in ("items", "keys", "memories", "entries"):
            arr = container.get(field) if isinstance(container, dict) else None
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


def read_deliverable(agent: str, short: str):
    """Find this agent's deliverable for the subtask (key contains the task short id)."""
    keys = _keys_of(_aimeat_call(agent, "aimeat_memory_list", {}))
    for k in keys:
        if k.startswith(f"crews.{agent}.") and short in k and k.endswith("latest_output"):
            r = _aimeat_call(agent, "aimeat_memory_read", {"key": k})
            val = r.get("value") if isinstance(r, dict) else r
            return k, val
    return None, None


def main() -> int:
    print(f"[smoke] coordinator = {COORDINATOR}\n", flush=True)

    # 1) FAN-OUT: create both subtasks up front so the crews run in parallel.
    for job in JOBS:
        resp = _aimeat_call(
            COORDINATOR,
            "aimeat_task_create",
            {
                "target_agent": job["agent"],
                "title": job["title"],
                "description": job["description"],
                "status": "queued",
            },
        )
        tid = _find_id(resp)
        job["task_id"] = tid
        job["short"] = tid.split("-", 1)[0] if tid else None
        print(f"[smoke] delegated to {job['agent']}: task {tid}", flush=True)
        if not tid:
            print(f"[smoke] could not read task id; raw response:\n{json.dumps(resp, indent=2)[:600]}", flush=True)

    if any(not j.get("task_id") for j in JOBS):
        print("[smoke] FAILED: a subtask was not created.", flush=True)
        return 1

    # 2+3) PARALLEL + FAN-IN: poll each crew's memory until its deliverable lands.
    print(f"\n[smoke] waiting for deliverables (poll {POLL_SECONDS}s, timeout {TIMEOUT_SECONDS}s) ...", flush=True)
    deadline = TIMEOUT_SECONDS
    waited = 0
    while waited < deadline:
        pending = []
        for job in JOBS:
            if "result" in job:
                continue
            key, val = read_deliverable(job["agent"], job["short"])
            if val:
                job["result"] = val
                print(f"[smoke]   ✓ {job['agent']} done -> {key}", flush=True)
            else:
                pending.append(job["agent"])
        if not pending:
            break
        print(f"[smoke]   still waiting on: {', '.join(pending)}  ({waited}s)", flush=True)
        time.sleep(POLL_SECONDS)
        waited += POLL_SECONDS

    missing = [j["agent"] for j in JOBS if "result" not in j]
    if missing:
        print(f"\n[smoke] TIMEOUT: no deliverable from: {', '.join(missing)}", flush=True)
        return 1

    # 4) SYNTHESIS: assemble one page from both crews' output.
    page = "# Tapiola Weekly (collaboration smoke test)\n\n"
    for job in JOBS:
        page += f"## {job['section']}  — by {job['agent']}\n\n{str(job['result']).strip()}\n\n"
    page += f"_Assembled by {COORDINATOR} from {len(JOBS)} crews._\n"

    pub = _aimeat_call(
        COORDINATOR,
        "aimeat_memory_write",
        {"key": "crews.crew-forge.tapiola-weekly-smoke.latest_output", "value": page, "visibility": "owner"},
    )
    print("\n[smoke] published page -> crews.crew-forge.tapiola-weekly-smoke.latest_output:", bool(pub), flush=True)
    print("\n" + "=" * 70 + "\n" + page + "=" * 70, flush=True)
    print("\n[smoke] SUCCESS: two crews collaborated via AIMEAT (fan-out + fan-in + synthesis).", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

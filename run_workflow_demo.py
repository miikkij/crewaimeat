"""Live re-test after the fixes: queue ONE goal to workflow-manager and wait for its deliverable.

Proves Fix B (deterministic publish + complete) and Fix A (collect gates on live-status done,
reads by exact key). Lighter goal -> fast crews -> clean full result.
"""
from __future__ import annotations

import time

from crewaimeat.aimeat_crew import _aimeat_call
from crewaimeat.workflow import _find_id, _read_deliverable, _deliverable_by_key

GOAL = (
    "Write a short, upbeat bulletin about opening a board-game cafe in Tapiola, Espoo: "
    "a few realistic scenarios for how it could go, plus one playful joke to open with."
)

resp = _aimeat_call(
    "crew-forge",
    "aimeat_task_create",
    {"target_agent": "workflow-manager", "title": "Board-game cafe bulletin", "description": GOAL, "status": "queued"},
)
tid = _find_id(resp)
short = tid.split("-", 1)[0] if tid else None
print(f"[demo] queued goal to workflow-manager: task {tid}", flush=True)
if not short:
    print(f"[demo] FAILED to create task: {resp}", flush=True)
    raise SystemExit(1)

deadline, waited = 900, 0
while waited < deadline:
    val = _deliverable_by_key("workflow-manager", GOAL, short) or _read_deliverable("workflow-manager", short)
    if val:
        print("\n" + "=" * 70, flush=True)
        print("FINAL DELIVERABLE (workflow-manager, after the fixes):\n", flush=True)
        print(val, flush=True)
        print("=" * 70, flush=True)
        print("\n[demo] SUCCESS: deliverable published + collected end-to-end.", flush=True)
        break
    print(f"[demo] waiting ... {waited}s", flush=True)
    time.sleep(20)
    waited += 20
else:
    print("\n[demo] TIMEOUT: no deliverable within 15 min.", flush=True)

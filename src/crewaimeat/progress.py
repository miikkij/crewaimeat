"""Deterministic progress bridge: CrewAI event bus -> AIMEAT (no LLM).

Two channels:
- **Milestones** (kickoff / task / tool transitions) -> ``aimeat_task_event``;
  they show up on the Tasks-tab timeline as discrete events.
- **Live status every 5s, OVERWRITING** -> memory key
  ``agents.<agent>.tasks.<aimeat_task_id>.live``. Because the value is overwritten
  rather than appended, an incomplete/crashed run also stays visible (last status
  + timestamp).

Signals come from CrewAI's framework events (crewai.events), not from LLM
decisions -> fully deterministic. Writes go through ``aimeat connect call`` (its
own short-lived REST client, independent of the serve process).

**Concurrency (aimeat-crewai >= 0.3.8 pool).** Several EXECUTE tasks may run at
once, each in its OWN worker thread (``executor.submit(_execute_worker, task)``
calls ``build_crew`` AND ``crew.kickoff()`` inside that thread, and CrewAI emits
its events synchronously in the running thread). So the reporter keeps per-task
state keyed by the worker thread's ident: ``bind`` (called in the worker thread
before kickoff) records thread->task, and every event handler routes to the task
owned by its calling thread. The serial path (max_concurrent_tasks=1) is just the
single-entry case. Each task's 5s heartbeat runs in its own beat thread and reads
the shared per-task state by task_id (lock-guarded). A thread with no bound task
emits nothing (safe: never misattributes to another task).

Prototype lives in crewaimeat. Portable to aimeat-crewai: there the daemon has
``_read_token`` -> token + node_url ready, so these writes can be done directly
with ``requests.post`` without a subprocess.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

from crewai.events import (  # noqa: E402
    BaseEventListener,
    CrewKickoffCompletedEvent,
    CrewKickoffFailedEvent,
    CrewKickoffStartedEvent,
    LLMCallStartedEvent,
    TaskStartedEvent,
    ToolUsageFinishedEvent,
    ToolUsageStartedEvent,
)

HEARTBEAT_SECONDS = 5


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _aimeat_fire(tool: str, payload: dict, agent: str) -> None:
    """Fire-and-forget AIMEAT call via the connector. Best-effort: progress must
    never crash the crew, so all errors are swallowed (logged to stderr)."""
    base = ["aimeat", "connect", "call", tool, "--agent", agent, "--stdin"]
    cmd = ["cmd", "/c", *base] if os.name == "nt" else base
    try:
        subprocess.run(
            cmd, input=json.dumps(payload), capture_output=True, text=True, timeout=20
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[progress] {tool} failed: {exc}", file=sys.stderr)


class ProgressReporter:
    """Multi-task progress reporter, keyed by the worker thread that owns each task.

    Event handlers call ``set``/``milestone`` (cheap, locked) and route to the
    task of the calling thread. Each task gets its own 5s heartbeat thread that
    writes its live status for as long as the crew is running.
    """

    def __init__(self, agent_name: str) -> None:
        self.agent_name = agent_name
        self._lock = threading.Lock()
        self._by_thread: dict[int, str] = {}   # worker thread ident -> task_id
        self._tasks: dict[str, dict] = {}      # task_id -> {title, status, t0, beat_stop}

    # --- small internal helpers ----------------------------------------- #
    def _live_key(self, task_id: str) -> str:
        return f"agents.{self.agent_name}.tasks.{task_id}.live"

    def _tid(self) -> str | None:
        """The task_id owned by the CALLING thread (where CrewAI fired this event)."""
        with self._lock:
            return self._by_thread.get(threading.get_ident())

    def _snapshot(self, task_id: str) -> dict | None:
        with self._lock:
            st = self._tasks.get(task_id)
            if not st:
                return None
            snap = dict(st["status"])
            snap["title"] = st["title"]
            snap["elapsed_s"] = int(time.monotonic() - st["t0"]) if st["t0"] else 0
            snap["updated_at"] = _now_iso()
            return snap

    def _write_live(self, task_id: str) -> None:
        snap = self._snapshot(task_id)
        if snap is None:
            return  # task already finished/cleaned up -> stop writing
        _aimeat_fire(
            "aimeat_memory_write",
            {"key": self._live_key(task_id), "value": snap, "visibility": "owner",
             "tags": ["live-status"]},
            self.agent_name,
        )

    def _milestone(self, ev_type: str, message: str) -> None:
        task_id = self._tid()
        if not task_id:
            return
        _aimeat_fire(
            "aimeat_task_event",
            {"task_id": task_id, "type": ev_type, "message": message},
            self.agent_name,
        )

    def set(self, **kw) -> None:
        task_id = self._tid()
        if not task_id:
            return
        with self._lock:
            st = self._tasks.get(task_id)
            if st:
                st["status"].update(kw)

    # --- lifecycle (called from the listener / _build) ------------------ #
    def bind(self, task_id: str, title: str) -> None:
        """Bind THIS worker thread to its AIMEAT task, before crew.kickoff."""
        with self._lock:
            self._by_thread[threading.get_ident()] = task_id
            self._tasks[task_id] = {
                "title": title or "",
                "status": {"state": "starting", "activity": "crew starting"},
                "t0": 0.0,
                "beat_stop": None,
            }

    def on_kickoff_start(self) -> None:
        task_id = self._tid()
        if not task_id:
            return
        with self._lock:
            st = self._tasks.get(task_id)
            if st:
                st["t0"] = time.monotonic()
                st["status"] = {"state": "running", "phase": "crew", "activity": "started"}
        self._milestone("started", "CrewAI crew started")
        self._write_live(task_id)
        self._start_beat(task_id)

    def on_kickoff_end(self, ok: bool, error: str = "") -> None:
        task_id = self._tid()
        if not task_id:
            return
        self._stop_beat(task_id)
        if ok:
            self.set(state="done", activity="done", tool=None)
            self._milestone("progress", "CrewAI crew finished")
        else:
            self.set(state="failed", activity=f"aborted: {error[:200]}", tool=None)
            self._milestone("progress", f"CrewAI crew aborted: {error[:200]}")
        self._write_live(task_id)  # last state stays in memory (also if incomplete)
        with self._lock:           # prevent stray writes after the task ends
            self._tasks.pop(task_id, None)
            self._by_thread.pop(threading.get_ident(), None)

    # --- heartbeat (one beat thread per task) --------------------------- #
    def _start_beat(self, task_id: str) -> None:
        stop = threading.Event()
        with self._lock:
            st = self._tasks.get(task_id)
            if not st:
                return
            st["beat_stop"] = stop

        def _loop() -> None:
            while not stop.wait(HEARTBEAT_SECONDS):
                self._write_live(task_id)

        threading.Thread(target=_loop, name=f"aimeat-progress-beat-{task_id[:8]}", daemon=True).start()

    def _stop_beat(self, task_id: str) -> None:
        with self._lock:
            st = self._tasks.get(task_id)
            stop = st.get("beat_stop") if st else None
        if stop:
            stop.set()


class _ProgressListener(BaseEventListener):
    """Registers handlers on CrewAI's global event bus (once). Each handler routes
    to the task owned by its calling thread via reporter._tid()."""

    def __init__(self, reporter: ProgressReporter) -> None:
        self._r = reporter
        super().__init__()

    def setup_listeners(self, bus) -> None:  # noqa: ANN001
        r = self._r

        @bus.on(CrewKickoffStartedEvent)
        def _ks(_src, _ev):  # noqa: ANN001
            if r._tid():
                r.on_kickoff_start()

        @bus.on(CrewKickoffCompletedEvent)
        def _kc(_src, _ev):  # noqa: ANN001
            if r._tid():
                r.on_kickoff_end(ok=True)

        @bus.on(CrewKickoffFailedEvent)
        def _kf(_src, ev):  # noqa: ANN001
            if r._tid():
                r.on_kickoff_end(ok=False, error=str(getattr(ev, "error", "")))

        @bus.on(TaskStartedEvent)
        def _ts(_src, ev):  # noqa: ANN001
            if not r._tid():
                return
            agent = getattr(getattr(ev, "task", None), "agent", None)
            role = getattr(agent, "role", None) or "agent"
            r.set(phase=role, tool=None, activity=f"{role} started")
            r._milestone("progress", f"{role} started its task")

        @bus.on(ToolUsageStartedEvent)
        def _tus(_src, ev):  # noqa: ANN001
            if not r._tid():
                return
            tool = getattr(ev, "tool_name", None) or "tool"
            role = getattr(ev, "agent_role", None) or "agent"
            r.set(phase=role, tool=tool, activity=f"{role}: {tool}")
            r._milestone("progress", f"{role} is using tool: {tool}")

        @bus.on(ToolUsageFinishedEvent)
        def _tuf(_src, ev):  # noqa: ANN001
            if not r._tid():
                return
            tool = getattr(ev, "tool_name", None) or "tool"
            r.set(tool=None, activity=f"{tool} done")

        @bus.on(LLMCallStartedEvent)
        def _lls(_src, ev):  # noqa: ANN001
            if not r._tid():
                return
            role = getattr(ev, "agent_role", None) or "agent"
            # No milestone (LLM calls are frequent) — live status only (5s beat).
            r.set(phase=role, activity=f"{role} thinking")


_INSTALLED: ProgressReporter | None = None


def install_progress(agent_name: str) -> ProgressReporter:
    """Create the reporter + register the listener once. Returns the singleton."""
    global _INSTALLED
    if _INSTALLED is None:
        _INSTALLED = ProgressReporter(agent_name)
        _ProgressListener(_INSTALLED)  # registers handlers on the bus
    return _INSTALLED

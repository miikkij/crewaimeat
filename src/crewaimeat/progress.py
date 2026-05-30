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
    """Shared state + a 5s heartbeat thread for a single AIMEAT task.

    Event handlers call ``set``/``milestone`` (cheap, locked). The heartbeat
    thread writes the live status to memory every ``HEARTBEAT_SECONDS`` for as
    long as the crew is running.
    """

    def __init__(self, agent_name: str) -> None:
        self.agent_name = agent_name
        self._lock = threading.Lock()
        self._task_id: str | None = None
        self._title: str = ""
        self._status: dict = {}
        self._t0: float = 0.0
        self._beat_stop: threading.Event | None = None

    # --- small internal helpers ----------------------------------------- #
    def _live_key(self) -> str:
        return f"agents.{self.agent_name}.tasks.{self._task_id}.live"

    def _snapshot(self) -> tuple[str | None, dict]:
        with self._lock:
            if not self._task_id:
                return None, {}
            snap = dict(self._status)
            snap["title"] = self._title
            snap["elapsed_s"] = int(time.monotonic() - self._t0) if self._t0 else 0
            snap["updated_at"] = _now_iso()
            return self._task_id, snap

    def _write_live(self) -> None:
        task_id, snap = self._snapshot()
        if not task_id:
            return
        _aimeat_fire(
            "aimeat_memory_write",
            {"key": self._live_key(), "value": snap, "visibility": "owner",
             "tags": ["live-status"]},
            self.agent_name,
        )

    def _milestone(self, ev_type: str, message: str) -> None:
        with self._lock:
            task_id = self._task_id
        if not task_id:
            return
        _aimeat_fire(
            "aimeat_task_event",
            {"task_id": task_id, "type": ev_type, "message": message},
            self.agent_name,
        )

    def set(self, **kw) -> None:
        with self._lock:
            self._status.update(kw)

    # --- lifecycle (called from the listener) --------------------------- #
    def bind(self, task_id: str, title: str) -> None:
        """Bind the reporter to this AIMEAT task before crew.kickoff."""
        with self._lock:
            self._task_id = task_id
            self._title = title or ""
            self._status = {"state": "starting", "activity": "crew starting"}
            self._t0 = 0.0

    def on_kickoff_start(self) -> None:
        with self._lock:
            self._t0 = time.monotonic()
            self._status = {"state": "running", "phase": "crew", "activity": "started"}
        self._milestone("started", "CrewAI crew started")
        self._write_live()
        self._start_beat()

    def on_kickoff_end(self, ok: bool, error: str = "") -> None:
        self._stop_beat()
        if ok:
            self.set(state="done", activity="done", tool=None)
            self._milestone("progress", "CrewAI crew finished")
        else:
            self.set(state="failed", activity=f"aborted: {error[:200]}", tool=None)
            self._milestone("progress", f"CrewAI crew aborted: {error[:200]}")
        self._write_live()  # last state stays in memory (also if incomplete)
        with self._lock:
            self._task_id = None  # prevent stray writes between tasks

    # --- heartbeat ------------------------------------------------------ #
    def _start_beat(self) -> None:
        stop = threading.Event()
        self._beat_stop = stop

        def _loop() -> None:
            while not stop.wait(HEARTBEAT_SECONDS):
                self._write_live()

        threading.Thread(target=_loop, name="aimeat-progress-beat", daemon=True).start()

    def _stop_beat(self) -> None:
        if self._beat_stop:
            self._beat_stop.set()
            self._beat_stop = None


class _ProgressListener(BaseEventListener):
    """Registers handlers on CrewAI's global event bus (once)."""

    def __init__(self, reporter: ProgressReporter) -> None:
        self._r = reporter
        super().__init__()

    def setup_listeners(self, bus) -> None:  # noqa: ANN001
        r = self._r

        @bus.on(CrewKickoffStartedEvent)
        def _ks(_src, _ev):  # noqa: ANN001
            if r._task_id:
                r.on_kickoff_start()

        @bus.on(CrewKickoffCompletedEvent)
        def _kc(_src, _ev):  # noqa: ANN001
            if r._task_id:
                r.on_kickoff_end(ok=True)

        @bus.on(CrewKickoffFailedEvent)
        def _kf(_src, ev):  # noqa: ANN001
            if r._task_id:
                r.on_kickoff_end(ok=False, error=str(getattr(ev, "error", "")))

        @bus.on(TaskStartedEvent)
        def _ts(_src, ev):  # noqa: ANN001
            if not r._task_id:
                return
            agent = getattr(getattr(ev, "task", None), "agent", None)
            role = getattr(agent, "role", None) or "agent"
            r.set(phase=role, tool=None, activity=f"{role} started")
            r._milestone("progress", f"{role} started its task")

        @bus.on(ToolUsageStartedEvent)
        def _tus(_src, ev):  # noqa: ANN001
            if not r._task_id:
                return
            tool = getattr(ev, "tool_name", None) or "tool"
            role = getattr(ev, "agent_role", None) or "agent"
            r.set(phase=role, tool=tool, activity=f"{role}: {tool}")
            r._milestone("progress", f"{role} is using tool: {tool}")

        @bus.on(ToolUsageFinishedEvent)
        def _tuf(_src, ev):  # noqa: ANN001
            if not r._task_id:
                return
            tool = getattr(ev, "tool_name", None) or "tool"
            r.set(tool=None, activity=f"{tool} done")

        @bus.on(LLMCallStartedEvent)
        def _lls(_src, ev):  # noqa: ANN001
            if not r._task_id:
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

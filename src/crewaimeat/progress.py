"""Deterministic progress bridge: CrewAI event bus -> AIMEAT (no LLM).

Two channels:
- **Milestones** (kickoff / task / tool transitions) -> ``aimeat_task_event``;
  they show up on the Tasks-tab timeline as discrete events.
- **Live status every 5s, OVERWRITING** -> memory key
  ``agents.<agent>.tasks.<aimeat_task_id>.live``. Because the value is overwritten
  rather than appended, an incomplete/crashed run also stays visible (last status
  + timestamp).

Signals come from CrewAI's framework events (crewai.events), not from LLM
decisions -> fully deterministic.

**ONE WRITER, THROUGH THE SERVE DAEMON, NEVER A PROCESS PER CALL.** Writes used to go through
``aimeat connect call`` -- one Node process per milestone and per heartbeat, 110-130 MB each, fired from
CrewAI's event handlers in parallel. Measured 2026-10-03 on a Solo place during one run: six at once
(five task events and a heartbeat), 1783 MB peak with ONE agent; the run before, the cgroup OOM killer
took the daemon and the worker at the 2048 MB limit. The place already runs ``aimeat connect serve
--http`` for exactly these calls, so every write now goes to its ``/local/call`` door (with the
serve.json secret) through ``_aimeat_call``, from ONE writer thread per process, in order:
milestones are queued as they come, and the live status keeps only its LATEST snapshot per task, so a
slow node never piles heartbeats up. With no daemon there is no write and one line says so -- the
progress view is not worth a Node process per event.

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

import atexit
import contextvars
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

# NB: per-LLM-call usage metering (AIMEAT LEDGER / TARGET-016) is NOT here anymore — it moved into
# the aimeat-crewai package (>=0.16.0): run_crew_daemon auto-subscribes to LLMCallCompletedEvent and
# POSTs the `llm_call` telemetry itself (run_id = the AIMEAT task id via a ContextVar). Keeping a copy
# here would DOUBLE-count every call. This module keeps only the milestone + 5s live-status channels.

HEARTBEAT_SECONDS = 5

# The active AIMEAT task for the current execution context. Set in bind() (in the worker thread,
# before kickoff); CrewAI copies the context into the threads it spawns during kickoff (its
# "contextvars thread propagation"), so event handlers — even when fired from a CrewAI sub-thread —
# resolve to the right task. Backed up by a thread map + a single-active fallback (see _tid).
_CURRENT_TASK: contextvars.ContextVar = contextvars.ContextVar("aimeat_progress_task", default=None)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class _Writer:
    """One background thread per process that sends progress writes, one at a time, through the serve
    daemon. Milestones keep their order; a live status replaces the one still waiting for its key."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._events: list[tuple[str, dict, str]] = []  # (tool, payload, agent), in arrival order
        self._live: dict[tuple[str, str], dict] = {}  # (agent, key) -> the latest payload not yet sent
        self._thread: threading.Thread | None = None
        self._said_no_daemon = False
        self._sending = False  # a write popped from the queue and not finished yet
        self.sent = 0  # for tests and for a person reading a stall: how many writes left this process

    def event(self, agent: str, payload: dict) -> None:
        with self._cond:
            self._events.append(("aimeat_task_event", payload, agent))
            self._wake()

    def live(self, agent: str, payload: dict) -> None:
        with self._cond:
            self._live[(agent, payload["key"])] = payload
            self._wake()

    def _wake(self) -> None:  # caller holds the lock
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="aimeat-progress-writer", daemon=True)
            self._thread.start()
        self._cond.notify()

    def _next(self) -> tuple[str, dict, str] | None:  # caller holds the lock
        if self._events:
            return self._events.pop(0)
        if self._live:
            (agent, _key), payload = self._live.popitem()
            return "aimeat_memory_write", payload, agent
        return None

    def _run(self) -> None:
        while True:
            with self._cond:
                item = self._next()
                while item is None:
                    if not self._cond.wait(timeout=30):
                        self._thread = None  # idle for 30 s: the thread ends, the next write starts one
                        return
                    item = self._next()
                self._sending = True
            try:
                self._send(*item)
            finally:
                with self._cond:
                    self._sending = False

    def _send(self, tool: str, payload: dict, agent: str) -> None:
        from crewaimeat import aimeat_crew

        if aimeat_crew._serve_api() is None:
            if not self._said_no_daemon:
                self._said_no_daemon = True
                print(
                    "[progress] no serve daemon: task events and live status are not written (a CLI "
                    "process per event is not worth it). Start the daemon to see progress.",
                    file=sys.stderr,
                )
            return
        try:
            aimeat_crew._aimeat_call(agent, tool, payload, retries=1, quiet=True)
            self.sent += 1
        except Exception as exc:  # noqa: BLE001 -- progress must never break the crew
            print(f"[progress] {tool} failed: {exc!r}", file=sys.stderr)

    def flush(self, timeout: float = 10.0) -> bool:
        """Wait until nothing is waiting (tests, and a worker about to exit). True when drained.

        When the writer thread is gone (it ended idle, or the interpreter is shutting down and cannot
        start one), what is still queued is sent from the calling thread, in order."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._cond:
                if not self._events and not self._live and not self._sending:
                    return True
                alive = self._thread is not None and self._thread.is_alive()
                item = None if (alive or self._sending) else self._next()
            if item is not None:
                self._send(*item)
                continue
            time.sleep(0.05)
        return False


_WRITER = _Writer()

# A spawn worker exits right after its run, and the writer is a daemon thread: without this the last
# milestone ("crew finished") and the last live status could die with the process. The old bridge
# wrote synchronously, so those always landed.
atexit.register(_WRITER.flush, 15.0)


def _aimeat_fire(tool: str, payload: dict, agent: str) -> None:
    """Queue a progress write; the writer thread sends it through the serve daemon. Never blocks the
    crew and never starts a process."""
    if tool == "aimeat_memory_write":
        _WRITER.live(agent, payload)
    else:
        _WRITER.event(agent, payload)


class ProgressReporter:
    """Multi-task progress reporter, keyed by the worker thread that owns each task.

    Event handlers call ``set``/``milestone`` (cheap, locked) and route to the
    task of the calling thread. Each task gets its own 5s heartbeat thread that
    writes its live status for as long as the crew is running.
    """

    def __init__(self, agent_name: str) -> None:
        self.agent_name = agent_name
        self._lock = threading.Lock()
        self._by_thread: dict[int, str] = {}  # worker thread ident -> task_id
        self._tasks: dict[str, dict] = {}  # task_id -> {title, status, t0, beat_stop}

    # --- small internal helpers ----------------------------------------- #
    def _live_key(self, task_id: str) -> str:
        return f"agents.{self.agent_name}.tasks.{task_id}.live"

    def _tid(self) -> str | None:
        """Resolve the AIMEAT task this event belongs to, most-reliable first:
        1. the contextvar (propagated into CrewAI's kickoff threads),
        2. the worker thread that called bind() (events fired inline in that thread),
        3. single-active fallback — if exactly one task is running it's unambiguous (covers serial
           mode and the case where CrewAI emits from a thread/context we did not tag).
        Returns None only when 2+ tasks run AND neither contextvar nor thread resolves -> we drop
        rather than misattribute to the wrong task."""
        t = _CURRENT_TASK.get()
        if t:
            return t
        with self._lock:
            t = self._by_thread.get(threading.get_ident())
            if t:
                return t
            if len(self._tasks) == 1:
                return next(iter(self._tasks))
            return None

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
            {
                "key": self._live_key(task_id),
                "value": snap,
                "visibility": "owner",
                "tags": ["live-status", f"task:{task_id}"],
            },  # per-task tag so AIMEAT lists it under the task
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
        """Bind the current execution context + worker thread to its AIMEAT task, before crew.kickoff."""
        _CURRENT_TASK.set(task_id)  # propagated into CrewAI's kickoff threads
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
        with self._lock:  # prevent stray writes after the task ends
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

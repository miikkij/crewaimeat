"""The SPAWNER — one manager per connector home that runs agents only while they have work.

    uv run python -m crewaimeat.spawner            # every crew declaring RUN_MODE = "spawn"
    uv run python -m crewaimeat.spawner --agents joker

WHAT IT IS. fleet_host keeps every agent alive as a thread forever: 49 agents cost ~3 GB of commit and
12.6% of a core on this machine even when nothing is happening, because idle is not free — it is just
invisible. The spawner inverts that. An idle spawn-mode agent is DATA on the node: no thread, no
liaison, no crew objects, nothing here but a row in a table and one parked HTTP request. When the node
pushes work, the spawner starts `crewaimeat.run_once` as a subprocess; when the cycle ends the process
exits and the OS takes all ~225 MB back. Memory then scales with what is RUNNING, not with how many
agents exist.

IT MUST NOT IMPORT CREWAI. That is the whole point — the manager has to be cheap enough that idling is
genuinely free. It uses `agent_manifest` (which reads crew files with `ast`, never by importing them),
`requests`, and the standard library.

HOW IT KNOWS TO WAKE. The node pushes over the WS tunnel to the shared `aimeat connect serve` daemon,
which fans it into a per-agent channel and fires `signalWake()`. The spawner is parked on that agent's
`GET /local/wake/next`, so the request simply returns. There is no polling of the node anywhere here.

WHAT IT DOES *NOT* DO — the load-bearing invariant: THE SPAWNER NEVER CONSUMES A QUEUE. The wake is a
pure signal that consumes nothing, so the worker can drain records/DMs and re-list tasks itself, exactly
as a continuous daemon does. If the spawner drained instead, it would own the only copy of that work and
would lose it the moment it crashed.

SINGLE-FLIGHT, AND WHY IT IS NOT NEGOTIABLE. One worker per agent at a time, always. Parallelism belongs
INSIDE a worker (`max_concurrent_tasks` — a bounded pool with its own liaison per task, code that
already exists and is proven). Two worker PROCESSES for one agent would race for the same task, because
the daemon's `in_flight`/`done_ids` guards are process-local and the node offers no task lease at all.
A wake that arrives mid-run therefore sets a `dirty` flag and the agent is re-spawned the instant the
current worker exits — the work is never dropped, because the work is not held here: tasks live on the
node and record/DM pushes live in the serve daemon's queue until somebody drains them.

DUPLICATE SAFETY DOES NOT RELY ON THIS FILE BEING CORRECT. The worker takes an OS advisory lock on the
agent (`aimeat_crew._acquire_single_instance`) and exits 0 if a continuous daemon already holds it. So a
spawn worker can never double-dispatch against fleet_host even if the policy here has a bug.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from crewaimeat import agent_manifest, spawn_state

# How long one park lasts before we re-park. Short enough that a dropped connection self-heals within
# half a minute, long enough that a quiet fleet makes ~2 requests a minute per agent over loopback.
WAKE_WAIT_MS = 25_000
# Coalesce a burst into one run. A workspace write storm is one wake per record; without this the
# spawner would start a 225 MB process for each of them and they would all find the same batch of work.
DEBOUNCE_S = 2.0
# The ceiling on CONCURRENT worker processes. Measured: a real run peaks at ~225 MB, so 8 is ~1.8 GB.
MAX_WORKERS = int(os.environ.get("SPAWN_MAX_WORKERS", "8"))
# Wall-clock per run. Mirrors the connector's own runner default rather than inventing a second number.
RUN_TIMEOUT_S = int(os.environ.get("SPAWN_RUN_TIMEOUT_S", "3600"))
KILL_GRACE_S = 10.0
STATUS_INTERVAL_S = 2.0
# How often the roster is re-read. The agent set CHANGES UNDER US: the node's basic-agents button
# enrols new agents into a running daemon without a restart, so a spawner that read its roster once
# would never serve them. 30 s is well inside "press the button and it works".
ROSTER_INTERVAL_S = 30.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class AgentState:
    """Everything the policy needs about one agent. Mutated ONLY under `Spawner._lock`."""

    agent: str
    proc: object | None = None
    run_id: str | None = None
    started_at: float = 0.0
    dirty: bool = False  # a wake arrived while a worker was running -> re-spawn on exit
    queued_since: float = 0.0  # >0 while waiting for a free worker slot
    runs: int = 0
    last_exit: int | None = None
    last_seconds: float | None = None
    killed_last: bool = False  # the previous run hit the timeout -> a second one does not get a retry
    trigger: str = "wake"
    wakes: int = 0
    last_wake_at: float = 0.0
    invokes: int = 0
    retired: bool = False  # dropped from the roster; its loops exit after the park in flight

    @property
    def busy(self) -> bool:
        return self.proc is not None


@dataclass
class Spawner:
    """The manager. `spawn_fn` and `wake_fn` are injection seams so tests never touch a real process
    or a real socket — the same shape `FleetApp(node_index_fn=…, snapshot_fn=…)` uses in the TUI."""

    agents: list[str]
    root: Path = field(default_factory=Path.cwd)
    spawn_fn: object | None = None  # (agent, run_id) -> Popen-like
    wake_fn: object | None = None  # (agent, timeout_s) -> bool  (True == woke)
    invoke_fn: object | None = None  # (agent, frame) -> {"ok", "result"}
    invoke_poll_fn: object | None = None  # (agent, timeout_s) -> frame dict | None
    roster_fn: object | None = None  # () -> list[str]; default discovers from crews/ + the node
    max_workers: int = MAX_WORKERS
    debounce_s: float = DEBOUNCE_S
    run_timeout_s: int = RUN_TIMEOUT_S
    roster_interval_s: float = ROSTER_INTERVAL_S

    def __post_init__(self) -> None:
        self.state: dict[str, AgentState] = {a: AgentState(a) for a in self.agents}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._queue: list[str] = []  # FIFO of agents waiting for a worker slot
        self._port: int | None = None
        self._threads: dict[str, list[threading.Thread]] = {}
        self._roster_note: str | None = None  # last roster complaint, printed once per change
        self._last_invoke_complaint: dict[str, float] = {}

    # ---------------------------------------------------------------- wake --- #
    def _serve_port(self) -> int | None:
        """The loopback port of the shared serve daemon, re-read each time so a serve restart heals."""
        doc = spawn_state.read_json(spawn_state.aimeat_home() / "serve.json")
        port = (doc or {}).get("port")
        return int(port) if isinstance(port, int) else None

    def _park(self, agent: str, timeout_s: float) -> bool:
        """Park on this agent's unified wake. True = something arrived; False = timeout/unreachable.

        `/local/wake/next` is PER AGENT — there is no fan-in endpoint — so one park per agent is the
        only shape available today. It is cheap: a socket and a blocked thread in a process that has
        not imported crewai.
        """
        if self.wake_fn is not None:
            return bool(self.wake_fn(agent, timeout_s))
        if "pytest" in sys.modules:  # same rule as _next_invoke: no live daemon from a test
            time.sleep(min(0.2, timeout_s))
            return False
        port = self._port or self._serve_port()
        if port is None:
            time.sleep(min(5.0, timeout_s))
            return False
        self._port = port
        import requests

        try:
            resp = requests.get(
                f"http://127.0.0.1:{port}/local/wake/next",
                params={"agent": agent, "wait": int(timeout_s * 1000)},
                headers={"X-Aimeat-Agent": agent},
                timeout=timeout_s + 10,
            )
        except Exception as exc:  # noqa: BLE001 — a dropped loopback connection is weather, not news
            print(f"[spawner] {agent}: wake park failed ({type(exc).__name__}) — re-parking", file=sys.stderr)
            self._port = None
            time.sleep(2.0)
            return False
        if resp.status_code == 204:
            return False  # nothing happened in the window; park again
        if resp.status_code == 200:
            return True
        # 400 UNKNOWN_AGENT means the serve daemon does not carry this agent: loud, and back off so a
        # misconfigured roster cannot turn into a request storm.
        print(f"[spawner] {agent}: wake refused HTTP {resp.status_code} {resp.text[:160]}", file=sys.stderr)
        time.sleep(10.0)
        return False

    def _wake_loop(self, agent: str) -> None:
        while not self._stop.is_set():
            if self._park(agent, WAKE_WAIT_MS / 1000.0):
                self.on_wake(agent)

    def _invoke_loop(self, agent: str) -> None:
        """Hold the server-initiated invoke poll for one agent — the Crew tab's Validate and Try.

        THIS PARK IS NOT OPTIONAL. The node drops a handler nobody has polled for 90 s
        (INVOKE_HANDLER_STALE_MS), and an idle spawn agent polls nothing, so without this the Crew
        tab answers NO_HANDLER for every spawn-mode agent. The park itself is a blocked socket in a
        process that has not imported crewai; the work is done by a worker that exits.
        """
        while not self._stop.is_set():
            st = self.state.get(agent)
            if st is None or st.retired:
                return
            frame = self._next_invoke(agent, WAKE_WAIT_MS / 1000.0)
            if frame:
                self._answer_invoke(agent, frame)

    def _next_invoke(self, agent: str, timeout_s: float) -> dict | None:
        if self.invoke_poll_fn is not None:
            return self.invoke_poll_fn(agent, timeout_s)
        if "pytest" in sys.modules:
            # A test must never reach the live daemon on the machine running the suite — the same
            # rule test_spawn_guards.py enforces for processes. Inject invoke_poll_fn to exercise it.
            time.sleep(min(0.2, timeout_s))
            return None
        port = self._port or self._serve_port()
        if port is None:
            time.sleep(min(5.0, timeout_s))
            return None
        import requests

        try:
            resp = requests.get(
                f"http://127.0.0.1:{port}/local/invoke/next",
                params={"agent": agent, "wait": int(timeout_s * 1000)},
                headers={"X-Aimeat-Agent": agent},
                timeout=timeout_s + 10,
            )
        except Exception:  # noqa: BLE001 — a dropped loopback connection is weather, not news
            time.sleep(2.0)
            return None
        if resp.status_code != 200:
            if resp.status_code != 204:
                # ANY refusal backs off. 404 is a connector predating the invoke surface; 400 is an
                # agent this daemon does not carry. Neither fixes itself by asking again at once, and
                # a long-poll that returns instantly is a tight loop: MEASURED 2026-09-02, one agent
                # the daemon did not carry produced 14 627 HTTP 400s against shared infrastructure
                # before the run was stopped. Say it once per minute, not once per millisecond.
                now = time.monotonic()
                if now - self._last_invoke_complaint.get(agent, 0.0) > 60.0:
                    self._last_invoke_complaint[agent] = now
                    print(f"[spawner] {agent}: invoke poll HTTP {resp.status_code} — backing off", file=sys.stderr)
                time.sleep(30.0)
            return None
        return (resp.json() or {}).get("data") or None

    def _answer_invoke(self, agent: str, frame: dict) -> None:
        """Run one invoke in a worker and post the answer. EVERY path posts: a button that spins
        forever is worse than one that says why."""
        inv_id = str(frame.get("id") or "")
        capability = str(frame.get("capability") or "")
        with self._lock:
            st = self.state.get(agent)
            if st is not None:
                st.invokes += 1
        payload = {"ok": False, "result": {"code": "HANDLER_ERROR", "message": "worker produced no answer"}}
        try:
            payload = self._run_invoke_worker(agent, frame) or payload
        except Exception as exc:  # noqa: BLE001
            payload = {"ok": False, "result": {"code": "HANDLER_ERROR", "message": f"{type(exc).__name__}: {exc}"}}
        port = self._port or self._serve_port()
        if port is None:
            return
        import requests

        try:
            requests.post(
                f"http://127.0.0.1:{port}/local/invoke/{inv_id}/result",
                headers={"X-Aimeat-Agent": agent},
                json=payload,
                timeout=30,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[spawner] {agent}: could not post invoke result ({exc!r})", file=sys.stderr)
        print(f"[spawner] {agent}: invoke {capability} answered ok={payload.get('ok')}", file=sys.stderr)

    def _run_invoke_worker(self, agent: str, frame: dict) -> dict | None:
        """Hand the frame to a short-lived worker and read its answer back.

        The frame travels through a file rather than argv: a crew definition is arbitrary JSON and
        has no business on a command line. The timeout follows the node's own `timeout_ms` for this
        call, so we stop when it has stopped waiting.
        """
        if self.invoke_fn is not None:
            return self.invoke_fn(agent, frame)
        if "pytest" in sys.modules:
            raise RuntimeError("refusing to spawn a real process under pytest — inject invoke_fn instead")
        job_dir = spawn_state.spawn_dir() / "invoke"
        job_dir.mkdir(parents=True, exist_ok=True)
        job = job_dir / f"{spawn_state._safe(agent)}-{spawn_state._safe(str(frame.get('id')))}.json"
        out = job.with_suffix(".out.json")
        job.write_text(
            json.dumps(
                {
                    "agent": agent,
                    "id": frame.get("id"),
                    "capability": frame.get("capability"),
                    "input": frame.get("input"),
                }
            ),
            encoding="utf-8",
        )
        env = dict(os.environ)
        env["AIMEAT_HOME"] = str(spawn_state.aimeat_home())
        deadline = frame.get("timeout_ms")
        timeout_s = (int(deadline) / 1000.0) if isinstance(deadline, (int, float)) and deadline > 0 else 300.0
        try:
            subprocess.run(  # noqa: S603 — argv list, no shell; the command is ours, not config
                [sys.executable, "-m", "crewaimeat.run_once", agent, "--answer-invoke", str(job)],
                cwd=str(self.root),
                env=env,
                capture_output=True,
                timeout=max(10.0, timeout_s),
                check=False,
            )
            return json.loads(out.read_text(encoding="utf-8")) if out.is_file() else None
        except subprocess.TimeoutExpired:
            return {"ok": False, "result": {"code": "TIMEOUT", "message": f"worker exceeded {timeout_s:.0f}s"}}
        finally:
            for f in (job, out):
                try:
                    f.unlink(missing_ok=True)
                except OSError:
                    pass

    # -------------------------------------------------------------- policy --- #
    def on_wake(self, agent: str, trigger: str = "wake") -> None:
        """A push arrived for `agent`. Decide: start it, mark it dirty, or queue it."""
        with self._lock:
            st = self.state.get(agent)
            if st is None:
                return
            now = time.monotonic()
            st.wakes += 1
            if st.busy:
                # Single-flight: never a second process for the same agent. Not dropped — re-run on exit.
                st.dirty = True
                st.last_wake_at = now
                print(f"[spawner] {agent}: wake while running -> queued behind the current run", file=sys.stderr)
                return
            if st.queued_since:
                st.last_wake_at = now
                return  # already waiting for a slot
            if st.last_wake_at and (now - st.last_wake_at) < self.debounce_s:
                # A burst (e.g. one wake per workspace record) is ONE unit of work: the worker re-lists
                # everything anyway, so a second process would only race the first to the same batch.
                st.last_wake_at = now
                print(f"[spawner] {agent}: wake coalesced (within {self.debounce_s}s)", file=sys.stderr)
                return
            st.last_wake_at = now
            if self._live_workers() >= self.max_workers:
                st.queued_since = now
                self._queue.append(agent)
                print(
                    f"[spawner] {agent}: all {self.max_workers} worker slots busy -> QUEUED "
                    f"(position {len(self._queue)}). Nothing is dropped.",
                    file=sys.stderr,
                )
                return
            self._start(st, trigger)

    def _live_workers(self) -> int:
        return sum(1 for s in self.state.values() if s.busy)

    def _start(self, st: AgentState, trigger: str) -> None:
        """Start one worker. Caller holds the lock."""
        run_id = f"{st.agent}-{int(time.time() * 1000):x}"
        try:
            proc = self._spawn(st.agent, run_id)
        except Exception as exc:  # noqa: BLE001 — surface it; do not silently stop serving the agent
            print(f"[spawner] {st.agent}: SPAWN FAILED {type(exc).__name__}: {exc}", file=sys.stderr)
            return
        st.proc, st.run_id, st.started_at, st.trigger = proc, run_id, time.monotonic(), trigger
        st.dirty = False
        st.runs += 1
        spawn_state.write_json(
            spawn_state.pid_file(st.agent),
            {"pid": proc.pid, "run_id": run_id, "started": _now(), "manager_pid": os.getpid()},
        )
        spawn_state.merge_audit(
            st.agent,
            run_id,
            {
                "trigger": trigger,
                "started": _now(),
                "pid": proc.pid,
                "log": str(spawn_state.log_file(st.agent, run_id)),
            },
        )
        print(f"[spawner] {st.agent}: run {run_id} started (pid {proc.pid}, trigger={trigger})", file=sys.stderr)

    def _spawn(self, agent: str, run_id: str):
        if self.spawn_fn is not None:
            return self.spawn_fn(agent, run_id)
        if "pytest" in sys.modules:  # the same refusal forge.launch_crew and tui.actions make
            raise RuntimeError("refusing to spawn a real process under pytest — inject spawn_fn instead")
        env = dict(os.environ)
        env["AIMEAT_SPAWN_RUN_ID"] = run_id
        env["AIMEAT_HOME"] = str(spawn_state.aimeat_home())
        # Keep the worker's console. A spawned run is over in seconds and its process is gone; if its
        # output went to DEVNULL the only record of WHAT HAPPENED would be an exit code, and "the task
        # did nothing" would be unanswerable after the fact.
        log = spawn_state.log_file(agent, run_id)
        log.parent.mkdir(parents=True, exist_ok=True)
        handle = open(log, "w", encoding="utf-8", errors="replace")
        proc = subprocess.Popen(  # noqa: S603 — argv list, no shell; the command is ours, not config
            [sys.executable, "-m", "crewaimeat.run_once", agent, "--quiet"],
            cwd=str(self.root),
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
        )
        proc._crewaimeat_log = handle  # closed in _settle; a leaked handle would pin the file open
        return proc

    # --------------------------------------------------------------- reaper --- #
    def reap(self) -> None:
        """One pass: finish exited workers, kill overdue ones, fill free slots. Safe to call often.

        NOTHING THAT BLOCKS RUNS UNDER THE LOCK. Killing an overdue worker waits up to 20 s
        (terminate, grace, kill, wait); doing that while holding `_lock` would stall every wake thread
        for the same 20 s, and a manager that stops answering wakes is exactly the knot this run mode
        cannot afford. So the pass is three phases: decide under the lock, do the slow part outside it,
        settle under the lock again.
        """
        with self._lock:
            finished = []
            overdue = []
            for st in self.state.values():
                if not st.busy:
                    continue
                code = st.proc.poll()
                if code is None:
                    if time.monotonic() - st.started_at > self.run_timeout_s:
                        overdue.append(st)
                    continue
                finished.append((st, int(code)))

        for st in overdue:  # slow, outside the lock
            self._kill(st)

        with self._lock:
            for st, code in finished:
                self._settle(st, code, killed=False)
            for st in overdue:
                if st.busy:
                    code = st.proc.poll()
                    self._settle(st, -1 if code is None else int(code), killed=True)
            self._drain_queue()

    def _kill(self, st: AgentState) -> None:
        """Terminate an overdue worker. Runs OUTSIDE the lock — it blocks for up to 20 s."""
        print(
            f"[spawner] {st.agent}: run {st.run_id} exceeded {self.run_timeout_s}s — terminating",
            file=sys.stderr,
        )
        try:
            st.proc.terminate()
            try:
                st.proc.wait(timeout=KILL_GRACE_S)
            except subprocess.TimeoutExpired:
                st.proc.kill()
                st.proc.wait(timeout=KILL_GRACE_S)
        except Exception as exc:  # noqa: BLE001
            print(f"[spawner] {st.agent}: kill failed ({exc!r})", file=sys.stderr)
        # The caller settles it under the lock — this function must not touch shared state itself.

    def _settle(self, st: AgentState, code: int, *, killed: bool) -> None:
        """Record the ending and release the slot. Caller holds the lock."""
        seconds = round(time.monotonic() - st.started_at, 1)
        run_id, agent = st.run_id, st.agent
        handle = getattr(st.proc, "_crewaimeat_log", None)
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        st.proc, st.run_id, st.last_exit, st.last_seconds = None, None, code, seconds
        try:
            spawn_state.pid_file(agent).unlink()
        except OSError:
            pass
        if run_id:
            spawn_state.merge_audit(
                agent, run_id, {"ended": _now(), "exit_code": code, "seconds": seconds, "killed": killed}
            )
        note = " KILLED" if killed else ""
        print(f"[spawner] {agent}: run {run_id} ended exit={code} in {seconds}s{note}", file=sys.stderr)
        if code == 2:
            # The daemon's auth-failure exit. Re-running would hot-loop against a dead token.
            print(
                f"[spawner] {agent}: token rejected (exit 2) — NOT re-spawning. Re-approve with "
                f"`npx aimeat@latest connect --agent {agent}`.",
                file=sys.stderr,
            )
            st.dirty = False
            return
        if killed and not st.killed_last:
            # A reaped worker leaves its task ACTIVE on the node, and the only thing that starts a
            # worker is a PUSH — which already happened, for the task that is still sitting there. So
            # without this the work waits for an unrelated event, indefinitely: on 2026-09-02 a worker
            # was killed at the hour mark and its task stayed active with nothing left to trigger it.
            # ONE retry, then stop: a run that times out twice is not transient, and re-spawning an
            # hour at a time forever is how a stuck agent becomes a bill.
            print(f"[spawner] {agent}: run was killed — ONE re-run, then it waits for a person.", file=sys.stderr)
            st.dirty = True
        st.killed_last = killed
        if st.dirty:
            st.dirty = False
            if self._live_workers() < self.max_workers:
                self._start(st, "dirty")
            else:
                st.queued_since = time.monotonic()
                self._queue.append(agent)

    def _drain_queue(self) -> None:
        """Give free slots to whoever has waited longest. Caller holds the lock."""
        while self._queue and self._live_workers() < self.max_workers:
            agent = self._queue.pop(0)
            st = self.state.get(agent)
            if st is None or st.busy:
                continue
            waited = round(time.monotonic() - st.queued_since, 1)
            st.queued_since = 0.0
            print(f"[spawner] {agent}: slot free after {waited}s in queue -> starting", file=sys.stderr)
            self._start(st, "queued")

    # --------------------------------------------------------------- status --- #
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "manager_pid": os.getpid(),
                "updated": _now(),
                "max_workers": self.max_workers,
                "live_workers": self._live_workers(),
                "queued": list(self._queue),
                "agents": {
                    a: {
                        "run_mode": "spawn",
                        "busy": s.busy,
                        "run_id": s.run_id,
                        "runs": s.runs,
                        "wakes": s.wakes,
                        "invokes": s.invokes,
                        "retired": s.retired,
                        "dirty": s.dirty,
                        "queued": bool(s.queued_since),
                        "last_exit": s.last_exit,
                        "last_seconds": s.last_seconds,
                        "seconds_running": round(time.monotonic() - s.started_at, 1) if s.busy else None,
                    }
                    for a, s in self.state.items()
                    if not s.retired
                },
            }

    def sweep_orphans(self) -> list[str]:
        """Kill workers left behind by a dead manager, and clear their pidfiles.

        A worker whose manager is gone answers to nobody: nothing will reap it, time it out, or notice
        it finished. Run at startup, before parking, so a crashed spawner cannot leak processes.
        """
        killed: list[str] = []
        rdir = spawn_state.running_dir()
        if not rdir.is_dir():
            return killed
        for pf in sorted(rdir.glob("*.pid")):
            doc = spawn_state.read_json(pf) or {}
            pid, mgr = doc.get("pid"), doc.get("manager_pid")
            if not isinstance(pid, int):
                pf.unlink(missing_ok=True)
                continue
            if not spawn_state.pid_alive(pid):
                pf.unlink(missing_ok=True)
                continue
            if isinstance(mgr, int) and spawn_state.pid_alive(mgr) and mgr != os.getpid():
                print(f"[spawner] {pf.stem}: worker pid {pid} has live manager {mgr} — leaving it", file=sys.stderr)
                continue
            print(f"[spawner] {pf.stem}: ORPHAN worker pid {pid} (manager {mgr} gone) — terminating", file=sys.stderr)
            _terminate_pid(pid)
            killed.append(f"{pf.stem}:{pid}")
            pf.unlink(missing_ok=True)
        return killed

    # --------------------------------------------------------------- roster --- #
    def _ensure_agent(self, agent: str) -> bool:
        """Start serving `agent` if we are not already. Returns True when it is newly started."""
        with self._lock:
            st = self.state.get(agent)
            if st is not None and not st.retired:
                return False
            self.state[agent] = AgentState(agent)
        threads = [
            threading.Thread(target=self._wake_loop, args=(agent,), name=f"wake:{agent}", daemon=True),
            threading.Thread(target=self._invoke_loop, args=(agent,), name=f"invoke:{agent}", daemon=True),
        ]
        self._threads[agent] = threads
        for t in threads:
            t.start()
        return True

    def _retire_agent(self, agent: str) -> None:
        """Stop serving `agent`. Its loops exit after the park in flight (<=25 s); a RUNNING worker is
        left alone — the agent left the roster, its work did not stop being work."""
        with self._lock:
            st = self.state.get(agent)
            if st is None or st.retired:
                return
            st.retired = True
            if agent in self._queue:
                self._queue.remove(agent)
        print(f"[spawner] {agent}: left the roster — no longer parking for it", file=sys.stderr)

    def refresh_roster(self) -> None:
        """Re-read the roster and start/stop parks to match it.

        THE AGENT SET CHANGES UNDER US. The node's basic-agents button enrols new agents into a
        running daemon without a restart, so a roster read once at startup would never serve them.
        Adding one starts its two parks; the others are not touched.
        """
        try:
            wanted = list(self.roster_fn()) if self.roster_fn else discover_agents(self.root)
        except Exception as exc:  # noqa: BLE001 — a bad roster read must not stop the ones we serve
            print(f"[spawner] roster refresh failed ({exc!r}); keeping the current set", file=sys.stderr)
            return
        current = {a for a, st in self.state.items() if not st.retired}
        for agent in sorted(set(wanted) - current):
            if self._ensure_agent(agent):
                print(f"[spawner] {agent}: joined the roster — parking for it now", file=sys.stderr)
        for agent in sorted(current - set(wanted)):
            self._retire_agent(agent)

    # ----------------------------------------------------------------- run --- #
    def serve_forever(self) -> int:
        self.sweep_orphans()
        for agent in list(self.state):
            self.state.pop(agent)
        self.refresh_roster()
        for agent in self.agents:  # an explicit --agents list is served even if discovery missed it
            self._ensure_agent(agent)
        live = sorted(a for a, st in self.state.items() if not st.retired)
        print(
            f"[spawner] parked on {len(live)} agent(s): {', '.join(live) or '(none yet)'} "
            f"(max {self.max_workers} concurrent workers, run timeout {self.run_timeout_s}s, "
            f"roster re-read every {self.roster_interval_s:.0f}s)",
            file=sys.stderr,
        )
        next_roster = time.monotonic() + self.roster_interval_s
        try:
            while not self._stop.is_set():
                self.reap()
                if time.monotonic() >= next_roster:
                    self.refresh_roster()
                    next_roster = time.monotonic() + self.roster_interval_s
                spawn_state.write_json(spawn_state.status_file(), self.snapshot())
                time.sleep(STATUS_INTERVAL_S)
        except KeyboardInterrupt:
            print("[spawner] stopping (Ctrl+C) — waiting for running workers", file=sys.stderr)
        finally:
            self._stop.set()
            self._shutdown()
        return 0

    def _shutdown(self) -> None:
        deadline = time.monotonic() + KILL_GRACE_S
        while time.monotonic() < deadline:
            self.reap()
            with self._lock:
                if self._live_workers() == 0:
                    break
            time.sleep(0.5)
        stuck = [st for st in self.state.values() if st.busy]
        for st in stuck:  # blocking, so outside the lock (see reap)
            self._kill(st)
        with self._lock:
            for st in stuck:
                if st.busy:
                    code = st.proc.poll()
                    self._settle(st, -1 if code is None else int(code), killed=True)
        try:
            spawn_state.status_file().unlink()
        except OSError:
            pass


def _terminate_pid(pid: int) -> None:
    """Kill a process TREE we did not spawn (a uv -> venv-shim -> python chain, on Windows)."""
    try:
        if os.name == "nt":
            subprocess.run(  # noqa: S603,S607 — fixed argv, no shell
                ["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=20, check=False
            )
        else:
            os.kill(pid, 15)
    except Exception as exc:  # noqa: BLE001
        print(f"[spawner] could not terminate pid {pid} ({exc!r})", file=sys.stderr)


def _serve_doc() -> dict:
    return spawn_state.read_json(spawn_state.aimeat_home() / "serve.json") or {}


def local_spawn_agents(root: Path) -> list[str]:
    """Live crews in THIS repo declaring RUN_MODE = "spawn"."""
    return sorted(
        m.agent
        for m in agent_manifest.all_manifests(root, refresh=True)
        if m.live and m.agent and m.effective_run_mode == agent_manifest.RUN_SPAWN
    )


def node_spawn_agents() -> tuple[list[str], str | None]:
    """Agents the NODE says are spawn-mode, as GAIIs. Returns (agents, note) — a reason, not a crash.

    ONE CALL PER OWNER, and the identity is the GAII, not the name. Both follow from one connector
    home now serving more than one owner:

    * `GET /v1/agents` is owner-scoped — asked as one of alice's agents it returns alice's and only
      alice's — so a single call would quietly serve half the daemon.
    * the same NAME exists under both owners, and the daemon REFUSES a shared bare name (measured:
      400 UNKNOWN_AGENT naming both GAIIs). A roster of names could not be parked on at all.

    The node filters server-side (`?run_mode=spawn`), so the 30-second refresh asks for the handful
    it wants instead of fetching everything and sorting it out here.
    """
    doc = _serve_doc()
    port = doc.get("port")
    if not isinstance(port, int):
        return [], "no serve daemon in serve.json — node roster skipped"
    # One caller per owner: whichever of that owner's agents the daemon carries.
    callers: dict[str, str] = {}
    for a in doc.get("agents") or []:
        owner, ident = a.get("owner"), (a.get("gaii") or a.get("agent"))
        if owner and ident and owner not in callers:
            callers[owner] = ident
    if not callers:
        return [], "serve.json names no agents — node roster skipped"
    import requests

    out: list[str] = []
    notes: list[str] = []
    for owner, caller in sorted(callers.items()):
        try:
            resp = requests.get(
                f"http://127.0.0.1:{port}/v1/agents",
                params={"run_mode": agent_manifest.RUN_SPAWN},
                headers={"X-Aimeat-Agent": caller},
                timeout=30,
            )
            rows = ((resp.json() or {}).get("data") or {}).get("agents") or []
        except Exception as exc:  # noqa: BLE001 — an unreachable node must not empty the roster
            notes.append(f"{owner}: unreadable ({type(exc).__name__})")
            continue
        picked = [
            str(r.get("gaii") or r.get("name"))
            for r in rows
            if isinstance(r, dict)
            and (r.get("gaii") or r.get("name"))
            and agent_manifest.normalise_run_mode(r.get("run_mode") or r.get("runMode")) == agent_manifest.RUN_SPAWN
        ]
        if rows and not picked:
            # An unknown query parameter is IGNORED, not refused, so a node that does not know this
            # filter answers with every agent it has. Taking that on trust would put the whole fleet
            # in spawn mode, so every row is re-checked and an unhonoured filter serves nothing.
            notes.append(f"{owner}: {len(rows)} agent(s), none marked run_mode=spawn")
            continue
        out.extend(picked)
    note = "; ".join(notes) + " — local crews only" if notes and not out else ("; ".join(notes) or None)
    return sorted(set(out)), note


def _daemon_carries() -> set[str]:
    """Identities the running daemon actually holds — GAIIs and their bare names."""
    out: set[str] = set()
    for a in _serve_doc().get("agents") or []:
        for v in (a.get("gaii"), a.get("agent")):
            if v:
                out.add(str(v))
    return out


def discover_agents(root: Path) -> list[str]:
    """The agents this spawner serves: THE NODE'S ROSTER, and nothing else.

    A crew file's `RUN_MODE = "spawn"` is a REQUEST, not a fact. It used to be added to this roster
    directly, and that is the same mistake as listing `crews/*.py` as an owner's delegable peers: a
    checkout holds whatever the developer is working on, and a connector home is somebody's real
    fleet. On a real machine (2026-09-03, 58 crews / 68 node agents / one owner) the difference is
    not academic — the repo's own demo crew would have been served as if the owner had asked for it.

    `fleet_host` reads the SAME source to decide which crews it must not thread, so the two runtimes
    cannot both claim an agent. When the node cannot be asked, this is empty and every crew stays a
    fleet thread: work still happens, in the other half, which is the safe direction to fail.
    """
    node, note = node_spawn_agents()
    if note:
        _note_once(note)
    # Said out loud, because a crew declaring spawn and NOT getting it is otherwise invisible: it
    # simply runs as a thread, which looks like nothing happened.
    asked = set(local_spawn_agents(root))
    unmet = sorted(asked - {agent_manifest.agent_local_name(a) for a in node})
    if unmet:
        _note_once(
            f"crew(s) declaring RUN_MODE=spawn that the node does not list as spawn, left to the "
            f"fleet host: {', '.join(unmet)}"
        )
    return sorted(set(node))


_LAST_NOTE: dict[str, str] = {}


def _note_once(note: str) -> None:
    """Say a roster limitation once per change, not every 30 s."""
    if _LAST_NOTE.get("roster") != note:
        _LAST_NOTE["roster"] = note
        print(f"[spawner] roster: {note}", file=sys.stderr)


def select_agents(root: Path, wanted: list[str] | None = None) -> list[str]:
    """Live crews declaring RUN_MODE = "spawn". Undeclared stays CONTINUOUS — fleet_host keeps those."""
    spawn = discover_agents(root)
    if not wanted:
        return sorted(spawn)
    chosen, unknown = [], []
    for w in wanted:
        if w in spawn:
            chosen.append(w)
        else:
            unknown.append(w)
    if unknown:
        # Loud: silently serving fewer agents than asked for is how a fleet goes quiet unnoticed.
        print(
            f"[spawner] NOT spawn-mode (or not a live crew), refusing to park on: {', '.join(unknown)}. "
            'Declare RUN_MODE = "spawn" in the crew file.',
            file=sys.stderr,
        )
    return sorted(chosen)


def _acquire_singleton():
    """One spawner per home, enforced by the OS so a crash never leaves a lock behind.

    Same mechanism serve_watchdog uses — chosen there over a heartbeat file because the OS releases
    the lock the instant the holder dies, so a crashed manager never blocks its successor.
    """
    path = spawn_state.lock_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fh = open(path, "a+")
    except OSError:
        return True  # cannot create the lock -> do not block a legitimate start
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="crewaimeat spawner",
        description="Run spawn-mode agents on demand: park on their wake, start a worker process per run.",
    )
    ap.add_argument("--agents", nargs="*", default=None, help="subset of spawn-mode agents (default: all)")
    ap.add_argument("--root", default=None, help="repo root holding crews/ (default: cwd)")
    ap.add_argument("--list", action="store_true", help="list spawn-mode agents and exit")
    ap.add_argument("--max-workers", type=int, default=MAX_WORKERS)
    ap.add_argument("--run-timeout", type=int, default=RUN_TIMEOUT_S)
    a = ap.parse_args(argv)

    root = Path(a.root) if a.root else Path.cwd()
    os.environ.setdefault("AIMEAT_HOME", str(root / ".aimeat"))
    from crewaimeat import env_guard

    env_guard.load_env()

    agents = select_agents(root, a.agents)
    if a.list:
        print("\n".join(agents) if agents else '(no crew declares RUN_MODE = "spawn")')
        return 0
    if not agents:
        print(
            '[spawner] no spawn-mode agents. Add RUN_MODE = "spawn" to a crew file '
            "(undeclared means continuous, so nothing changes for the existing fleet).",
            file=sys.stderr,
        )
        return 1

    lock = _acquire_singleton()
    if lock is None:
        print("[spawner] another spawner already serves this AIMEAT_HOME — exiting.", file=sys.stderr)
        return 0
    return Spawner(agents=agents, root=root, max_workers=a.max_workers, run_timeout_s=a.run_timeout).serve_forever()


if __name__ == "__main__":
    raise SystemExit(main())

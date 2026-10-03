"""The progress bridge writes through the serve daemon, one write at a time, and never starts a process.

Solo place, 2026-10-03: during one run the bridge had six `aimeat connect call` Node processes alive at
once (five task events and a heartbeat), 110-130 MB each, and one agent peaked at 1783 MB; the run before,
the cgroup OOM killer took the daemon and the worker at the 2048 MB limit. These tests hold:

  every write goes through `_aimeat_call` (the daemon's /local/call door), never a subprocess;
  the writes leave one at a time, milestones in order, and the live status keeps only its latest;
  with no daemon nothing is written and one line says so.
"""

from __future__ import annotations

import threading
import time

import pytest

from crewaimeat import progress


@pytest.fixture
def writer(monkeypatch):
    """A fresh writer, a daemon that is there, and a recording `_aimeat_call` that tracks concurrency."""
    from crewaimeat import aimeat_crew

    w = progress._Writer()
    monkeypatch.setattr(progress, "_WRITER", w)
    monkeypatch.setattr(aimeat_crew, "_serve_api", lambda: ("http://127.0.0.1:1", object()))
    state = {"calls": [], "inflight": 0, "peak": 0, "delay": 0.0}
    lock = threading.Lock()

    def call(agent, tool, payload, **kw):
        with lock:
            state["inflight"] += 1
            state["peak"] = max(state["peak"], state["inflight"])
        time.sleep(state["delay"])
        with lock:
            state["calls"].append((tool, payload))
            state["inflight"] -= 1
        return {"ok": True}

    monkeypatch.setattr(aimeat_crew, "_aimeat_call", call)
    return w, state


def test_writes_go_through_the_daemon_and_never_start_a_process(writer):
    w, state = writer
    progress._aimeat_fire("aimeat_task_event", {"task_id": "t1", "type": "started", "message": "go"}, "a")
    progress._aimeat_fire("aimeat_memory_write", {"key": "agents.a.tasks.t1.live", "value": {"s": 1}}, "a")
    assert w.flush()
    assert [t for t, _ in state["calls"]] == ["aimeat_task_event", "aimeat_memory_write"]
    # The offline guard in conftest fails any subprocess; reaching here is the proof none was started.


def test_parallel_event_handlers_produce_one_write_at_a_time(writer):
    """CrewAI fires its handlers from several threads; the hosted run had six writes in flight."""
    w, state = writer
    state["delay"] = 0.02

    def fire(i):
        progress._aimeat_fire("aimeat_task_event", {"task_id": "t1", "type": "progress", "message": f"m{i}"}, "a")

    threads = [threading.Thread(target=fire, args=(i,)) for i in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert w.flush()
    assert len(state["calls"]) == 12
    assert state["peak"] == 1, f"{state['peak']} writes were in flight at once"


def test_milestones_keep_their_order_and_every_one_is_sent(writer):
    """The timeline is what it was: seven uses of one tool are seven events, in order."""
    w, state = writer
    for msg in ("a", "b", "b", "c"):
        progress._aimeat_fire("aimeat_task_event", {"task_id": "t1", "type": "progress", "message": msg}, "x")
    assert w.flush()
    assert [p["message"] for _, p in state["calls"]] == ["a", "b", "b", "c"]


def test_the_live_status_keeps_only_its_latest_while_the_node_is_slow(writer):
    w, state = writer
    state["delay"] = 0.2
    progress._aimeat_fire("aimeat_task_event", {"task_id": "t1", "type": "started", "message": "go"}, "a")
    for n in range(10):  # ten heartbeats queue up behind the slow first write
        progress._aimeat_fire("aimeat_memory_write", {"key": "agents.a.tasks.t1.live", "value": {"n": n}}, "a")
    assert w.flush(timeout=5)
    lives = [p["value"]["n"] for t, p in state["calls"] if t == "aimeat_memory_write"]
    assert lives == [9], f"stale heartbeats were sent: {lives}"


def test_with_no_daemon_nothing_is_written_and_it_is_said_once(monkeypatch, capsys):
    from crewaimeat import aimeat_crew

    w = progress._Writer()
    monkeypatch.setattr(progress, "_WRITER", w)
    monkeypatch.setattr(aimeat_crew, "_serve_api", lambda: None)
    called = []
    monkeypatch.setattr(aimeat_crew, "_aimeat_call", lambda *a, **k: called.append(a))
    for i in range(3):
        progress._aimeat_fire("aimeat_task_event", {"task_id": "t", "type": "progress", "message": str(i)}, "a")
    assert w.flush()
    assert called == []
    assert capsys.readouterr().err.count("no serve daemon") == 1


def test_a_failing_write_never_reaches_the_crew(monkeypatch, capsys):
    from crewaimeat import aimeat_crew

    w = progress._Writer()
    monkeypatch.setattr(progress, "_WRITER", w)
    monkeypatch.setattr(aimeat_crew, "_serve_api", lambda: ("http://127.0.0.1:1", object()))
    monkeypatch.setattr(aimeat_crew, "_aimeat_call", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("tunnel")))
    progress._aimeat_fire("aimeat_task_event", {"task_id": "t", "type": "progress", "message": "x"}, "a")
    assert w.flush()
    assert "aimeat_task_event failed" in capsys.readouterr().err


def test_the_cli_fallback_runs_one_process_at_a_time():
    from crewaimeat import aimeat_crew

    assert isinstance(aimeat_crew._SUBPROCESS_LOCK, type(threading.Lock()))
    import inspect

    src = inspect.getsource(aimeat_crew._aimeat_call_subprocess)
    assert "with _SUBPROCESS_LOCK:" in src
    assert "subprocess" not in inspect.getsource(progress).split('"""', 2)[2], "the bridge spawns nothing itself"


def test_what_is_queued_when_the_writer_is_gone_is_sent_from_the_caller(monkeypatch):
    """A spawn worker exits right after its run; the last milestone must not die with the writer."""
    from crewaimeat import aimeat_crew

    w = progress._Writer()
    monkeypatch.setattr(aimeat_crew, "_serve_api", lambda: ("http://127.0.0.1:1", object()))
    sent = []
    monkeypatch.setattr(aimeat_crew, "_aimeat_call", lambda a, t, p, **k: sent.append(p.get("message")) or {"ok": True})
    monkeypatch.setattr(w, "_wake", lambda: None)  # no thread: as at interpreter shutdown
    w.event("a", {"task_id": "t", "type": "progress", "message": "CrewAI crew finished"})
    assert w.flush(timeout=2)
    assert sent == ["CrewAI crew finished"]


def test_the_writer_is_flushed_at_exit():
    import inspect

    assert "atexit.register(_WRITER.flush" in inspect.getsource(progress)

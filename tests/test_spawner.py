"""The spawned run mode: declaration, policy, and the guards that make duplicates impossible.

Deterministic — no node, no LLM, no real process, no socket. The Spawner takes `spawn_fn` and
`wake_fn` injection seams for exactly this reason (same shape as FleetApp's `snapshot_fn`), so the
policy can be driven step by step instead of raced against wall-clock.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #
class FakeProc:
    """A Popen-like the spawner can poll. Mirrors the real signature it depends on."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self._code: int | None = None
        self.terminated = False
        self.killed = False

    def poll(self) -> int | None:
        return self._code

    def finish(self, code: int = 0) -> None:
        self._code = code

    def terminate(self) -> None:
        self.terminated = True
        self._code = -15

    def kill(self) -> None:
        self.killed = True
        self._code = -9

    def wait(self, timeout: float | None = None) -> int:
        return self._code if self._code is not None else 0


def make_spawner(agents, **kw):
    from crewaimeat.spawner import Spawner

    spawned: list[tuple[str, str, FakeProc]] = []

    def spawn_fn(agent: str, run_id: str) -> FakeProc:
        proc = FakeProc(9000 + len(spawned))
        spawned.append((agent, run_id, proc))
        return proc

    kw.setdefault("debounce_s", 0.0)
    sp = Spawner(agents=list(agents), root=Path.cwd(), spawn_fn=spawn_fn, wake_fn=lambda *_: False, **kw)
    return sp, spawned


# --------------------------------------------------------------------------- #
# Declaration (phase 0): nothing about the existing fleet may change
# --------------------------------------------------------------------------- #
def _repo(tmp_path: Path, body: str, name: str = "demo_crew.py") -> Path:
    crews = tmp_path / "crews"
    crews.mkdir(parents=True, exist_ok=True)
    (crews / name).write_text(body, encoding="utf-8")
    return tmp_path


def test_undeclared_run_mode_is_resident(tmp_path):
    """The backward-compatibility promise, asserted rather than hoped for."""
    from crewaimeat import agent_manifest as am

    root = _repo(tmp_path, 'AGENT_NAME = "plain"\n\ndef build_domain(ctx):\n    ...\n\ndef run():\n    ...\n')
    man = am.manifest_for("plain", root)
    assert man.run_mode is None
    assert man.effective_run_mode == am.RUN_RESIDENT


def test_declared_spawn_mode_is_read_statically(tmp_path):
    from crewaimeat import agent_manifest as am

    root = _repo(
        tmp_path,
        'AGENT_NAME = "sp"\nRUN_MODE = "spawn"\nMAX_CONCURRENT = 1\n'
        "\ndef build_domain(ctx):\n    ...\n\ndef run():\n    ...\n",
    )
    man = am.manifest_for("sp", root)
    assert man.effective_run_mode == am.RUN_SPAWN
    assert man.single_flight is True


def test_typo_in_run_mode_falls_back_to_resident_not_to_a_guess(tmp_path):
    """A typo must not silently relocate an agent to a different runtime; doctor makes it loud."""
    from crewaimeat import agent_manifest as am

    root = _repo(
        tmp_path,
        'AGENT_NAME = "t"\nRUN_MODE = "sapwn"\n\ndef build_domain(ctx):\n    ...\n\ndef run():\n    ...\n',
    )
    assert am.manifest_for("t", root).effective_run_mode == am.RUN_RESIDENT


def test_crew_def_validator_rejects_bad_run_mode_and_concurrency():
    from crewaimeat.crew_def import validate_crew_doc

    base = {
        "agent_name": "x",
        "agents": [{"role": "r", "goal": "g", "backstory": "b"}],
        "tasks": [{"id": "t", "description": "{{ctx.prompt}}", "expected_output": "o", "agent": "r"}],
    }
    assert validate_crew_doc({**base, "run_mode": "spawn", "max_concurrent": 2}) == []
    assert any("run_mode" in e for e in validate_crew_doc({**base, "run_mode": "sapwn"}))
    assert any("max_concurrent" in e for e in validate_crew_doc({**base, "max_concurrent": 0}))
    # bool is an int in Python — True must not read as 1 and hide a typo
    assert any("max_concurrent" in e for e in validate_crew_doc({**base, "max_concurrent": True}))


def test_select_agents_only_picks_spawn_mode(tmp_path, monkeypatch):
    # Own AIMEAT_HOME, or the roster reads the developer's REAL serve.json and answers about their
    # live daemon instead of this fixture — which is how this test started failing for a reason that
    # had nothing to do with what it checks.
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path / "home"))
    from crewaimeat.spawner import select_agents

    root = _repo(
        tmp_path, 'AGENT_NAME = "cont"\n\ndef build_domain(ctx):\n    ...\n\ndef run():\n    ...\n', "a_crew.py"
    )
    _repo(
        tmp_path,
        'AGENT_NAME = "spawny"\nRUN_MODE = "spawn"\n\ndef build_domain(ctx):\n    ...\n\ndef run():\n    ...\n',
        "b_crew.py",
    )
    # THE NODE'S ROSTER IS THE SET. A crew file's RUN_MODE is a request; without the node agreeing,
    # nobody is served and the fleet host keeps every crew — the safe direction, and it is said aloud.
    assert select_agents(root) == []

    monkeypatch.setattr("crewaimeat.spawner.node_spawn_agents", lambda: (["spawny"], None))
    assert select_agents(root) == ["spawny"]
    # asking for a continuous agent is refused, not silently served
    assert select_agents(root, ["cont"]) == []


# --------------------------------------------------------------------------- #
# Single-flight — the property the whole design rests on
# --------------------------------------------------------------------------- #
def test_wake_while_running_never_starts_a_second_process():
    sp, spawned = make_spawner(["a"])
    sp.on_wake("a")
    assert len(spawned) == 1
    for _ in range(5):
        sp.on_wake("a")
    assert len(spawned) == 1, "single-flight violated: a second worker for the same agent"
    assert sp.state["a"].dirty is True


def test_dirty_wake_is_re_run_after_the_worker_exits_not_dropped():
    sp, spawned = make_spawner(["a"])
    sp.on_wake("a")
    sp.on_wake("a")  # arrives mid-run
    spawned[0][2].finish(0)
    sp.reap()
    assert len(spawned) == 2, "the queued wake was dropped instead of re-run"
    assert sp.state["a"].dirty is False


def test_debounce_coalesces_a_burst_into_one_run():
    sp, spawned = make_spawner(["a"], debounce_s=60.0)
    sp.on_wake("a")
    spawned[0][2].finish(0)
    sp.reap()
    sp.on_wake("a")  # inside the debounce window -> coalesced
    assert len(spawned) == 1


# --------------------------------------------------------------------------- #
# The worker cap and the queue: nothing is silently dropped
# --------------------------------------------------------------------------- #
def test_cap_queues_instead_of_dropping():
    sp, spawned = make_spawner(["a", "b", "c"], max_workers=2)
    sp.on_wake("a")
    sp.on_wake("b")
    sp.on_wake("c")
    assert len(spawned) == 2 and sp._queue == ["c"]
    spawned[0][2].finish(0)
    sp.reap()
    assert [s[0] for s in spawned] == ["a", "b", "c"]
    assert sp._queue == []


def test_queue_is_fifo():
    sp, spawned = make_spawner(["a", "b", "c"], max_workers=1)
    sp.on_wake("a")
    sp.on_wake("b")
    sp.on_wake("c")
    assert sp._queue == ["b", "c"]
    spawned[0][2].finish(0)
    sp.reap()
    assert spawned[-1][0] == "b"


# --------------------------------------------------------------------------- #
# Endings: timeout, auth failure, orphans
# --------------------------------------------------------------------------- #
def test_overdue_worker_is_terminated():
    sp, spawned = make_spawner(["a"], run_timeout_s=30)
    sp.on_wake("a")
    proc = spawned[0][2]
    # Age the run past its deadline rather than racing a 0s timeout: time.monotonic() has ~15 ms
    # granularity on Windows, so "started and checked in the same tick" gives elapsed == 0.0 exactly.
    sp.state["a"].started_at -= 31
    sp.reap()
    assert proc.terminated is True
    assert sp.state["a"].last_exit is not None  # the overdue run was settled, its slot released
    assert len(spawned) == 2  # ...and handed straight to the one retry a reaped run gets


def test_a_reaped_run_is_retried_once_then_left_for_a_person():
    """A killed worker's task stays ACTIVE, and the push that would start one already happened.

    Live 2026-09-02: a worker was reaped at the hour mark and its task sat active with nothing left
    to trigger it. One retry covers the transient case; a second timeout is not transient, and
    re-spawning an hour at a time forever is how a stuck agent becomes a bill.
    """
    sp, spawned = make_spawner(["a"], run_timeout_s=30)
    sp.on_wake("a")
    sp.state["a"].started_at -= 31
    sp.reap()
    assert len(spawned) == 2  # reaped, then re-run once

    spawned[1][2].terminated = True  # the retry times out too
    sp.state["a"].started_at -= 31
    sp.reap()
    assert len(spawned) == 2  # ...and THAT one is left alone
    assert sp.state["a"].dirty is False


def test_auth_failure_exit_2_does_not_respawn():
    """Exit 2 is the daemon's 'token rejected'. Re-running would hot-loop against a dead token."""
    sp, spawned = make_spawner(["a"])
    sp.on_wake("a")
    sp.on_wake("a")  # would normally re-run via dirty
    spawned[0][2].finish(2)
    sp.reap()
    assert len(spawned) == 1
    assert sp.state["a"].dirty is False


def test_orphan_sweep_leaves_workers_of_a_live_manager_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    from crewaimeat import spawn_state
    from crewaimeat.spawner import Spawner

    killed: list[int] = []
    monkeypatch.setattr("crewaimeat.spawner._terminate_pid", lambda pid: killed.append(pid))
    # alive worker whose manager is also alive -> not ours to touch
    monkeypatch.setattr(spawn_state, "pid_alive", lambda pid: pid in (111, 222))
    spawn_state.write_json(spawn_state.pid_file("mine"), {"pid": 111, "manager_pid": 222})
    sp = Spawner(agents=["mine"], root=tmp_path, spawn_fn=lambda *_: None, wake_fn=lambda *_: False)
    assert sp.sweep_orphans() == []
    assert killed == []


def test_orphan_sweep_kills_a_worker_whose_manager_is_gone(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    from crewaimeat import spawn_state
    from crewaimeat.spawner import Spawner

    killed: list[int] = []
    monkeypatch.setattr("crewaimeat.spawner._terminate_pid", lambda pid: killed.append(pid))
    monkeypatch.setattr(spawn_state, "pid_alive", lambda pid: pid == 111)  # worker alive, manager dead
    spawn_state.write_json(spawn_state.pid_file("orph"), {"pid": 111, "manager_pid": 999})
    sp = Spawner(agents=["orph"], root=tmp_path, spawn_fn=lambda *_: None, wake_fn=lambda *_: False)
    assert sp.sweep_orphans() == ["orph:111"]
    assert killed == [111]
    assert not spawn_state.pid_file("orph").exists()


# --------------------------------------------------------------------------- #
# The no-real-processes rule (this file's own backstop)
# --------------------------------------------------------------------------- #
def test_spawner_refuses_to_start_a_real_process_under_pytest():
    from crewaimeat.spawner import Spawner

    sp = Spawner(agents=["a"], root=Path.cwd(), wake_fn=lambda *_: False)
    with pytest.raises(RuntimeError, match="pytest"):
        sp._spawn("a", "run-1")


# --------------------------------------------------------------------------- #
# Audit + measurement
# --------------------------------------------------------------------------- #
def test_audit_record_merges_both_writers(tmp_path, monkeypatch):
    """The worker writes what only it knows (peak memory); the spawner writes the rest."""
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    from crewaimeat import spawn_state

    spawn_state.merge_audit("a", "r1", {"trigger": "wake", "pid": 7})
    spawn_state.merge_audit("a", "r1", {"peak_rss_mb": 225.0})
    spawn_state.merge_audit("a", "r1", {"exit_code": 0, "killed": False})
    doc = spawn_state.read_json(spawn_state.audit_file("a", "r1"))
    assert doc == {
        "agent": "a",
        "run_id": "r1",
        "trigger": "wake",
        "pid": 7,
        "peak_rss_mb": 225.0,
        "exit_code": 0,
        "killed": False,
    }


def test_peak_rss_is_actually_measured():
    """A measurement that silently returns None would make every audit record a lie of omission."""
    from crewaimeat.spawn_state import peak_rss_mb

    val = peak_rss_mb()
    assert val is not None and val > 1.0


def test_spawn_paths_hang_off_aimeat_home_not_cwd(tmp_path, monkeypatch):
    """The state must follow the connector home — that is what keeps two users' agents apart."""
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path / "tenant-a"))
    from crewaimeat import spawn_state

    assert str(spawn_state.pid_file("x")).startswith(str(tmp_path / "tenant-a"))
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path / "tenant-b"))
    assert str(spawn_state.pid_file("x")).startswith(str(tmp_path / "tenant-b"))


# --------------------------------------------------------------------------- #
# doctor invariants — each one catches a failure that is SILENT in production
# --------------------------------------------------------------------------- #
def _doctor_rules(root: Path) -> set[str]:
    from crewaimeat.doctor.cli import run

    return {f.rule for f in run(root).findings}


def _spawn_repo(tmp_path: Path, extra: str = "", cfg: str | None = None) -> Path:
    crews = tmp_path / "crews"
    crews.mkdir(parents=True, exist_ok=True)
    (crews / "sp_crew.py").write_text(
        'AGENT_NAME = "sp"\nRUN_MODE = "spawn"\nMAX_CONCURRENT = 1\n'
        + extra
        + "\ndef build_domain(ctx):\n    ...\n\ndef run():\n    ...\n",
        encoding="utf-8",
    )
    aim = tmp_path / ".aimeat"
    (aim).mkdir(exist_ok=True)
    (aim / "serve.json").write_text('{"agents":[{"agent":"sp","owner":"o"}]}', encoding="utf-8")
    if cfg is not None:
        d = aim / "agents" / "sp"
        d.mkdir(parents=True, exist_ok=True)
        (d / "config.yaml").write_text(cfg, encoding="utf-8")
    return tmp_path


def test_doctor_flags_an_unknown_run_mode(tmp_path):
    crews = tmp_path / "crews"
    crews.mkdir(parents=True)
    (crews / "t_crew.py").write_text(
        'AGENT_NAME = "t"\nRUN_MODE = "sapwn"\n\ndef build_domain(ctx):\n    ...\n\ndef run():\n    ...\n',
        encoding="utf-8",
    )
    assert "runmode.unknown" in _doctor_rules(tmp_path)


def test_doctor_flags_spawn_plus_a_custom_invoke_handler(tmp_path):
    """The spawner holds the invoke poll and answers crew.validate/try in its own worker, so a crew's
    OWN on_invoke handler would never run — silently. That is the trap worth a hard error."""
    root = _spawn_repo(tmp_path, extra="SPEC_NOTE = 'CrewSpec(on_invoke=mine)'\n")
    assert "runmode.spawn.custom_invoke" in _doctor_rules(root)


def test_doctor_flags_a_connector_runner_block(tmp_path):
    root = _spawn_repo(tmp_path, cfg="agent: sp\nrunner:\n  command: python\n")
    assert "spawn.connector_runner_set" in _doctor_rules(root)


def test_doctor_finds_the_runner_block_at_the_per_owner_path(tmp_path):
    """The settings file moved to agents/<owner>/<agent>/config.yaml when one daemon started serving
    more than one owner. A rule that looks only where the file NO LONGER IS reports clean forever,
    which is worse than the error it exists to catch."""
    root = _spawn_repo(tmp_path)
    d = root / ".aimeat" / "agents" / "alice" / "sp"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.yaml").write_text("runner:\n  command: python\n", encoding="utf-8")
    assert "spawn.connector_runner_set" in _doctor_rules(root)


def test_doctor_flags_undeclared_concurrency_on_a_spawn_crew(tmp_path):
    crews = tmp_path / "crews"
    crews.mkdir(parents=True)
    (crews / "u_crew.py").write_text(
        'AGENT_NAME = "u"\nRUN_MODE = "spawn"\n\ndef build_domain(ctx):\n    ...\n\ndef run():\n    ...\n',
        encoding="utf-8",
    )
    assert "concurrency.undeclared" in _doctor_rules(tmp_path)


def test_doctor_is_quiet_on_a_well_formed_spawn_crew(tmp_path):
    """The rules must not fire on the healthy case, or they become noise people learn to ignore."""
    root = _spawn_repo(tmp_path)
    rules = _doctor_rules(root)
    assert "runmode.unknown" not in rules
    assert "concurrency.undeclared" not in rules
    assert "spawn.connector_runner_set" not in rules
    assert "runmode.spawn.custom_invoke" not in rules


def test_a_killed_worker_still_has_a_memory_number(tmp_path, monkeypatch):
    """The heartbeat is the ONLY thing that measures a run the spawner reaps.

    A worker killed at the run timeout never reaches `_finish`, so its own closing write never
    happens: on 2026-09-02 a real worker was terminated at 3601.8 s and its audit record read
    `peak_rss_mb: null` — the one run whose cost anybody would want to look up.
    """
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    from crewaimeat import spawn_state
    from crewaimeat.run_once import _record_rss

    spawn_state.merge_audit("a", "r1", {"trigger": "wake", "pid": 7})
    assert _record_rss("a", "r1", 0.0) is True
    # ...the worker is now killed; only the spawner's settle write follows.
    spawn_state.merge_audit("a", "r1", {"exit_code": -1, "killed": True})

    doc = spawn_state.read_json(spawn_state.audit_file("a", "r1"))
    assert doc["killed"] is True
    assert doc["peak_rss_mb"] is not None and doc["peak_rss_mb"] > 1.0
    assert doc["worker_seconds"] > 0


def test_the_heartbeat_stays_quiet_when_nobody_spawned_this_worker(monkeypatch):
    """`crewaimeat run-once <agent>` by hand has no audit record to write into."""
    monkeypatch.delenv("AIMEAT_SPAWN_RUN_ID", raising=False)
    from crewaimeat.run_once import _start_rss_heartbeat

    before = threading.active_count()
    _start_rss_heartbeat("a", 0.0)
    assert threading.active_count() == before  # no thread, no writes

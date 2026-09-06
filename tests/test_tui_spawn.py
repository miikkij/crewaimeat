"""TUI vs. the spawner: a parked agent is resting, not missing — and a lock file is not a lock.

Both defects were measured on 2026-09-06 against a healthy all-spawn fleet: fifty rows read
"attached (no runtime)" with a padlock on every one of them, while the spawner held a park for all
fifty and no process held any lock. A monitor that is wrong about a healthy fleet is worse than no
monitor, because it is the thing people check before believing something is broken.
"""

from __future__ import annotations

from crewaimeat.tui.fleet_state import AgentRow, collect_locks, derive_status
from crewaimeat.tui.render import _runtime_cell


def _st(**kw):
    base = dict(watchdog=0, daemon=0, lock=False, in_tunnel=True, age_s=None)
    return derive_status(**{**base, **kw})


def test_parked_is_a_resting_state_not_a_fault():
    assert _st(parked=True) == "parked"


def test_a_worker_running_reads_as_running():
    assert _st(parked=True, workers=1) == "running"
    assert _st(parked=True, workers=3) == "running 3"


def test_spawn_beside_a_per_process_daemon_is_the_loudest_problem():
    """Whichever loses the per-agent OS lock exits SystemExit 0, so the fleet looks up while the
    wrong half holds it. That must outrank every other verdict."""
    assert _st(parked=True, daemon=1, watchdog=1) == "DUPLICATE"
    assert _st(parked=True, daemon=1) == "DUPLICATE"


def test_the_old_verdicts_survive_for_a_fleet_that_has_no_spawner():
    assert _st(watchdog=2, daemon=1) == "DUPLICATE"
    assert _st(daemon=1) == "orphan"
    assert _st(watchdog=1, daemon=1) == "running"
    assert _st(in_tunnel=False, lock=True) == "down (stale lock)"
    assert _st(in_tunnel=False) == "down"
    assert _st() == "attached (no runtime)"


def test_a_lock_file_nobody_holds_is_not_reported_as_locked(tmp_path, monkeypatch):
    from crewaimeat.tui import fleet_state as fs

    locks = tmp_path / ".locks"
    locks.mkdir()
    (locks / "ghost-agent.lock").write_text("", encoding="utf-8")
    monkeypatch.setattr(fs, "_LOCKS_DIR", locks)
    assert collect_locks() == set(), "the OS frees the lock with the process; the file stays behind"


def test_a_lock_actually_held_is_reported(tmp_path, monkeypatch):
    from crewaimeat.tui import fleet_state as fs

    locks = tmp_path / ".locks"
    locks.mkdir()
    path = locks / "busy-agent.lock"
    path.write_text("", encoding="utf-8")
    monkeypatch.setattr(fs, "_LOCKS_DIR", locks)
    monkeypatch.setattr(fs, "_lock_is_held", lambda fh: True)
    assert collect_locks() == {"busy-agent"}


def _row(**kw) -> AgentRow:
    base = dict(
        agent="a",
        crew_file="a_crew.py",
        watchdog_procs=0,
        daemon_procs=0,
        lock=False,
        in_tunnel=True,
        last_seen=None,
        last_seen_age_s=None,
        mode=None,
        status="parked",
    )
    return AgentRow(**{**base, **kw})


def test_the_runtime_cell_names_who_runs_it():
    assert _runtime_cell(_row(hosted=True)) == "host"
    assert _runtime_cell(_row(parked=True)) == "spawn"
    assert _runtime_cell(_row(parked=True, workers=2)) == "spawn x2"
    assert _runtime_cell(_row(watchdog_procs=1, daemon_procs=1)) == "proc 1/1"
    assert _runtime_cell(_row()) == "-"

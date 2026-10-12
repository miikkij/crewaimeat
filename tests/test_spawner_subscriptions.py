"""The spawner starts an agent whose record spaces the connector does not hold.

A record subscription is made by a worker and kept in the serve daemon's memory, so a connector
restart leaves a records-driven agent deaf with nothing to announce it. Measured on a local node
2026-10-12 (connector 3.26.1): `/local/status` read `subscriptions: 0` after the restart, a record was
published, and no worker started.

Deterministic: no node, no daemon, no real process. `subscriptions_fn` is the daemon's status and
`spawn_fn` the worker.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from crewaimeat import spawn_state

A = "support-drafts#owner@node"
SPACE = {"organism_id": "org-1", "ws": "ws-1", "space": "shared.support-requests"}


class FakeProc:
    def __init__(self, pid: int) -> None:
        self.pid = pid

    def poll(self):
        return None


@pytest.fixture(autouse=True)
def own_home(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path / "home"))


def _spawner(tmp_path, held, *, daemon="daemon-1", agents=(A,)):
    from crewaimeat.spawner import Spawner

    started: list[str] = []

    def spawn(agent, run_id):
        started.append(agent)
        return FakeProc(1000 + len(started))

    state = {"daemon": daemon, "held": held}
    sp = Spawner(
        agents=list(agents),
        root=tmp_path,
        spawn_fn=spawn,
        subscriptions_fn=lambda: None if state["held"] is None else (state["daemon"], dict(state["held"])),
    )
    return sp, started, state


def _note(agent, spaces):
    spawn_state.write_json(spawn_state.subscribed_file(agent), {"spaces": spaces})


def test_an_agent_whose_spaces_the_connector_lost_is_started_once(tmp_path, capsys):
    _note(A, [SPACE])
    sp, started, _ = _spawner(tmp_path, {A: 0})

    assert sp.start_unsubscribed() == [A]
    assert started == [A]
    assert "holds none of its 1 record space(s)" in capsys.readouterr().err + capsys.readouterr().out


def test_it_is_not_started_again_for_the_same_connector_start(tmp_path):
    """A worker that cannot subscribe leaves the daemon at zero; without the limit that is a new
    worker every few seconds."""
    _note(A, [SPACE])
    sp, started, _ = _spawner(tmp_path, {A: 0})
    sp.start_unsubscribed()
    sp.state[A].proc = None  # the worker ended, and the daemon still holds nothing

    assert sp.start_unsubscribed() == [] and started == [A]


def test_the_next_connector_start_is_a_new_reason(tmp_path):
    _note(A, [SPACE])
    sp, started, state = _spawner(tmp_path, {A: 0})
    sp.start_unsubscribed()
    sp.state[A].proc = None
    sp.state[A].last_wake_at = 0.0
    state["daemon"] = "daemon-2"

    assert sp.start_unsubscribed() == [A] and started == [A, A]


def test_an_agent_the_connector_still_holds_is_left_parked(tmp_path):
    _note(A, [SPACE])
    sp, started, _ = _spawner(tmp_path, {A: 1})
    assert sp.start_unsubscribed() == [] and started == []


def test_an_agent_that_watches_nothing_is_left_parked(tmp_path):
    _note(A, [])
    sp, started, _ = _spawner(tmp_path, {A: 0})
    assert sp.start_unsubscribed() == [] and started == []


def test_an_agent_defined_on_the_node_that_has_not_run_here_is_started(tmp_path, capsys):
    """A new agent whose approval started no run, or one the owner moved to this computer: only its
    definition on the node says whether it watches a space, and only a run reads that."""
    sp, started, _ = _spawner(tmp_path, {A: 0})

    assert sp.start_unsubscribed() == [A] and started == [A]
    assert "has not run on this computer yet" in capsys.readouterr().err + capsys.readouterr().out


def test_a_crew_file_s_agent_with_no_note_is_left_parked(tmp_path):
    """It leaves its note on its next run; starting every crew of a fleet once to find out would be
    fifty workers on the first start after this change."""
    crews = tmp_path / "crews"
    crews.mkdir()
    (crews / "demo_crew.py").write_text(
        'AGENT_NAME = "demo"\nRUN_MODE = "spawn"\n\ndef build_domain(ctx):\n    ...\n\ndef run():\n    ...\n',
        encoding="utf-8",
    )
    sp, started, _ = _spawner(tmp_path, {"demo": 0}, agents=("demo",))
    assert sp.start_unsubscribed() == [] and started == []


def test_a_daemon_that_cannot_be_asked_starts_nobody(tmp_path):
    _note(A, [SPACE])
    sp, started, _ = _spawner(tmp_path, None)
    assert sp.start_unsubscribed() == [] and started == []


def test_an_agent_the_daemon_gives_no_count_for_is_not_taken_as_zero(tmp_path):
    """Not carried by this daemon, or a connector older than the field: absent is not known."""
    _note(A, [SPACE])
    sp, started, _ = _spawner(tmp_path, {})
    assert sp.start_unsubscribed() == [] and started == []


def test_a_running_worker_is_not_doubled(tmp_path):
    _note(A, [SPACE])
    sp, started, _ = _spawner(tmp_path, {A: 0})
    sp.on_wake(A)

    assert sp.start_unsubscribed() == [] and started == [A]


def test_a_stopped_agent_is_left_parked(tmp_path, monkeypatch):
    from crewaimeat import spawner

    _note(A, [SPACE])
    monkeypatch.setattr(spawner, "STOPPED_ON_NODE", {A})
    sp, started, _ = _spawner(tmp_path, {A: 0})
    assert sp.start_unsubscribed() == [] and started == []


# ── the worker's half ────────────────────────────────────────────────────────
def test_a_spawned_worker_leaves_the_spaces_it_asked_for(monkeypatch):
    monkeypatch.setenv(spawn_state.SPAWN_AGENT_ENV, A)
    spawn_state.note_subscribed([SPACE])
    assert spawn_state.read_json(spawn_state.subscribed_file(A)) == {"spaces": [SPACE]}


def test_a_worker_that_watches_nothing_says_so(monkeypatch):
    monkeypatch.setenv(spawn_state.SPAWN_AGENT_ENV, A)
    spawn_state.note_subscribed([])
    assert spawn_state.read_json(spawn_state.subscribed_file(A)) == {"spaces": []}


def test_outside_a_spawned_worker_nothing_is_written(monkeypatch):
    monkeypatch.delenv(spawn_state.SPAWN_AGENT_ENV, raising=False)
    spawn_state.note_subscribed([SPACE])
    assert not spawn_state.subscribed_file(A).parent.exists()


def test_the_worker_is_told_who_it_is_in_the_spawner_s_spelling(tmp_path):
    sp, _started, _ = _spawner(tmp_path, {})
    assert sp._worker_env(A, "run-1")[spawn_state.SPAWN_AGENT_ENV] == A


def test_a_deleted_agent_s_note_goes_with_its_other_files(tmp_path):
    from crewaimeat import agent_files

    _note(A, [SPACE])
    assert spawn_state.subscribed_file(A) in agent_files.runtime_files(Path(tmp_path), [A])


# ── the daemon's status as the spawner reads it ──────────────────────────────
def test_the_daemon_s_counts_are_read_by_identity_and_a_missing_count_is_left_out(monkeypatch):
    from crewaimeat import spawner

    rows = [
        {"agent": "support-drafts", "gaii": A, "transport": "tunnel", "subscriptions": 2},
        {"agent": "old", "gaii": "old#owner@node", "transport": "tunnel"},
        {"agent": "moved", "gaii": "moved#owner@node", "transport": spawner.REFUSED, "subscriptions": 0},
    ]

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True, "data": {"agents": rows}}

    monkeypatch.setattr(spawner, "_serve_doc", lambda: {"port": 4711, "pid": 99, "secret": "s"})
    monkeypatch.setattr("requests.get", lambda url, **kw: _Resp())

    assert spawner.daemon_subscriptions() == ((99, 4711), {A: 2, "support-drafts": 2})


def test_no_daemon_in_serve_json_is_not_an_empty_daemon(monkeypatch):
    from crewaimeat import spawner

    monkeypatch.setattr(spawner, "_serve_doc", lambda: {})
    assert spawner.daemon_subscriptions() is None

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


def _spawner(tmp_path, held, *, daemon="daemon-1", agents=(A,), definition_fn=None):
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
        definition_fn=definition_fn,
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


# ── an agent is subscribed for what it DECLARES, without waiting for a first run ─────────────
# Measured 2026-10-16: web-researcher (a crew file, record_spaces through contract_record_spaces) had
# not run since 2026-09-06 and had no note, so the spawner never started it, the connector held no
# subscription, and a republished market-scan request woke nobody.

RECORDS_CREW = (
    'AGENT_NAME = "watcher"\nRUN_MODE = "spawn"\n\n'
    "def build_domain(ctx):\n    ...\n\n"
    "def run():\n"
    "    run_crew(CrewSpec(agent_name=AGENT_NAME, build_domain=build_domain,\n"
    '        listen_for=("tasks", "records"), record_spaces=lambda: contract_record_spaces(AGENT_NAME, C)))\n'
)
TASKS_CREW = (
    'AGENT_NAME = "worker"\nRUN_MODE = "spawn"\n\n'
    "def build_domain(ctx):\n    ...\n\n"
    "def run():\n    run_crew(CrewSpec(agent_name=AGENT_NAME, build_domain=build_domain))\n"
)
GTM_VIESTITTAJA = "gtm-viestittaja#happydude500001@aimeat-finland-001-genesis"
GTM_MITTARI = "gtm-mittari#happydude500001@aimeat-finland-001-genesis"
CRM = {"organism_id": "b9c552d1-6a73-40f7-9d2c-91172c7f1332", "ws": "ws-mrkj9sbei68", "space": "crm.contacts"}
DEFS = {
    GTM_VIESTITTAJA: {"agent_name": "gtm-viestittaja", "listen_for": ["tasks", "records"], "record_spaces": [CRM]},
    GTM_MITTARI: {"agent_name": "gtm-mittari", "listen_for": ["tasks"]},
}


def _crew(tmp_path, name, text):
    crews = tmp_path / "crews"
    crews.mkdir(exist_ok=True)
    (crews / f"{name}_crew.py").write_text(text, encoding="utf-8")


def test_a_crew_file_that_declares_record_spaces_is_started_with_no_note(tmp_path, capsys):
    _crew(tmp_path, "watcher", RECORDS_CREW)
    sp, started, _ = _spawner(tmp_path, {"watcher": 0}, agents=("watcher",))
    assert sp.start_unsubscribed() == ["watcher"] and started == ["watcher"]
    assert "declares record spaces" in capsys.readouterr().err + capsys.readouterr().out


def test_a_crew_file_without_record_spaces_is_not_started(tmp_path):
    _crew(tmp_path, "worker", TASKS_CREW)
    sp, started, _ = _spawner(tmp_path, {"worker": 0}, agents=("worker",))
    assert sp.start_unsubscribed() == [] and started == []


def test_a_node_defined_agent_is_started_by_its_definition_and_a_tasks_only_one_is_not(tmp_path):
    asked: list[str] = []

    def definition(agent):
        asked.append(agent)
        return DEFS.get(agent)

    sp, started, _ = _spawner(
        tmp_path, {GTM_VIESTITTAJA: 0, GTM_MITTARI: 0}, agents=(GTM_VIESTITTAJA, GTM_MITTARI), definition_fn=definition
    )
    assert sp.start_unsubscribed() == [GTM_VIESTITTAJA] and started == [GTM_VIESTITTAJA]
    assert sp.start_unsubscribed() == [], "the next look, same connector start: nobody again"
    assert sorted(asked) == sorted([GTM_VIESTITTAJA, GTM_MITTARI]), "each definition read once per connector start"


def test_an_agent_already_subscribed_is_not_started(tmp_path):
    _crew(tmp_path, "watcher", RECORDS_CREW)
    sp, started, state = _spawner(
        tmp_path,
        {"watcher": 1, GTM_VIESTITTAJA: 1},
        agents=("watcher", GTM_VIESTITTAJA),
        definition_fn=DEFS.get,
    )
    assert sp.start_unsubscribed() == [] and started == []


def test_a_subscribed_agent_is_not_started_twice_for_one_connector_start(tmp_path):
    _crew(tmp_path, "watcher", RECORDS_CREW)
    sp, started, state = _spawner(tmp_path, {"watcher": 0}, agents=("watcher",))
    sp.start_unsubscribed()
    sp.state["watcher"].proc = None  # the worker ran, and subscribed
    state["held"] = {"watcher": 1}
    assert sp.start_unsubscribed() == [] and started == ["watcher"]


def test_the_crew_files_of_this_repository_say_what_they_watch():
    from crewaimeat import agent_manifest

    root = Path(__file__).resolve().parents[1]
    by = agent_manifest.by_agent(root, refresh=True)
    assert by["web-researcher"].record_spaces is True
    assert by["cadence-followup"].record_spaces is True
    assert by["some-listener"].record_spaces is False


def test_the_definition_key_is_the_registry_key():
    from crewaimeat.crew_registry import registry_key
    from crewaimeat.spawner import definition_key

    assert definition_key(GTM_VIESTITTAJA) == registry_key(GTM_VIESTITTAJA) == "crews.registry.gtm-viestittaja"


@pytest.mark.parametrize(
    ("value", "declares"),
    [
        ({"version": 1, "doc": DEFS[GTM_VIESTITTAJA]}, True),  # the envelope publish_crew_def writes
        (DEFS[GTM_MITTARI], False),  # a bare document
        ('{"doc": {"listen_for": ["tasks", "records"], "record_spaces": [{"x": 1}]}}', True),  # stored as text
    ],
)
def test_the_definition_is_read_through_the_daemon_as_the_agent(monkeypatch, value, declares):
    from crewaimeat import agent_manifest, spawner

    sent = {}

    class _Resp:
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True, "data": {"key": "crews.registry.gtm-viestittaja", "value": value}}

    def post(url, json=None, headers=None, **kw):
        sent.update(url=url, json=json, headers=headers)
        return _Resp()

    monkeypatch.setattr(spawner, "_serve_doc", lambda: {"port": 4711, "pid": 99, "secret": "s"})
    monkeypatch.setattr("requests.post", post)
    doc = spawner.read_definition(GTM_VIESTITTAJA)
    assert agent_manifest.doc_declares_records(doc) is declares
    assert sent["url"] == "http://127.0.0.1:4711/local/call/aimeat_memory_read"
    assert sent["json"]["key"] == "crews.registry.gtm-viestittaja"
    assert sent["headers"]["X-Aimeat-Agent"] == GTM_VIESTITTAJA

"""The fleet host's second kind of resident agent: one that has only a definition on the node.

An agent ordered on aimeat.io has no file in `crews/`; it is `crews.registry.<agent>` on the node and
a key on one connector. Marked run_mode=resident ("Always on") the spawner skips it, and until
2026-10-10 the host did not know it either, because the host picked what to run from `crews/` alone
— so nobody ran it (measured on a hosted place 2026-10-02: the task stayed `active` for 12 minutes).

Deterministic: no node, no model, no daemon, no crewai. `run_json_agent` and `_prepare_runtime` are
replaced, and the roster is whatever the test says the node and the connector answered.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

NODE = "n1"


def _gaii(name: str, owner: str = "alice") -> str:
    return f"{name}#{owner}@{NODE}"


@pytest.fixture
def host(monkeypatch, tmp_path):
    """The host module in a checkout of its own, with its module state reset and nothing heavy loaded."""
    from crewaimeat import agent_manifest, fleet_host, spawner

    (tmp_path / "crews").mkdir()
    monkeypatch.chdir(tmp_path)
    agent_manifest.all_manifests(tmp_path, refresh=True)
    monkeypatch.setattr(fleet_host, "_NOTED", {})
    monkeypatch.setattr(fleet_host, "_status", {})
    monkeypatch.setattr(fleet_host, "_prepare_runtime", lambda: None)
    monkeypatch.setattr("crewaimeat.log_timestamps.install", lambda: None)  # it wraps the real stderr
    monkeypatch.setattr(fleet_host, "_STAGGER_S", 0.0)
    monkeypatch.setattr(fleet_host, "_STATUS_FILE", tmp_path / "logs" / ".host_status.json")
    monkeypatch.setattr(spawner, "_LAST_NOTE", {})
    return fleet_host


class _Runs:
    """Stands in for `run_json_agent`: records who was started and blocks like a daemon loop would,
    until the test lets it go or tells it how to end."""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.release = threading.Event()
        self.ending: dict[str, BaseException] = {}

    def __call__(self, identity: str, **_kw) -> None:
        self.started.append(identity)
        if identity in self.ending:
            raise self.ending[identity]
        self.release.wait(5)


@pytest.fixture
def runs(monkeypatch):
    from crewaimeat import json_agent

    r = _Runs()
    monkeypatch.setattr(json_agent, "run_json_agent", r)
    yield r
    r.release.set()


def _settle(residents) -> None:
    for res in residents.values():
        if res.thread is not None:
            res.thread.join(timeout=2)


def _wait_started(runs: _Runs, n: int) -> None:
    deadline = time.monotonic() + 2
    while len(runs.started) < n and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(runs.started) >= n, f"expected {n} start(s), saw {runs.started}"


def _crew_file(root: Path, agent: str, fname: str) -> None:
    (root / "crews" / fname).write_text(
        f'AGENT_NAME = "{agent}"\n\ndef build_domain(ctx):\n    ...\n\ndef run():\n    ...\n', encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# Which agents: resident on the node, no crew file here, carried by this connector
# --------------------------------------------------------------------------- #
def test_the_host_runs_a_resident_agent_that_exists_only_on_the_node(host, monkeypatch, tmp_path):
    from crewaimeat import agent_manifest, spawner

    _crew_file(tmp_path, "filed", "filed_crew.py")
    _crew_file(tmp_path, "parked", "_parked_crew.py")
    agent_manifest.all_manifests(tmp_path, refresh=True)
    listed = [_gaii("ordered"), _gaii("filed"), _gaii("parked"), _gaii("elsewhere")]
    asked: list[str] = []

    def roster(run_mode="spawn"):
        asked.append(run_mode)
        return listed, [], set()

    monkeypatch.setattr(spawner, "read_node_roster", roster)
    monkeypatch.setattr(spawner, "daemon_identities", lambda: {g: "tunnel" for g in listed if "elsewhere" not in g})
    assert host._node_resident_agents() == [_gaii("ordered")]
    assert asked == ["resident"], "the host reads the resident half, never the spawner's"


def test_a_parked_crew_file_is_not_overruled_by_the_node(host, monkeypatch, tmp_path):
    """A leading underscore is the developer saying "not now" in the checkout the fleet runs from."""
    from crewaimeat import agent_manifest, spawner

    _crew_file(tmp_path, "parked", "_parked_crew.py")
    agent_manifest.all_manifests(tmp_path, refresh=True)
    monkeypatch.setattr(spawner, "read_node_roster", lambda run_mode="spawn": ([_gaii("parked")], [], set()))
    monkeypatch.setattr(spawner, "daemon_identities", lambda: {_gaii("parked"): "tunnel"})
    assert host._node_resident_agents() == []


def test_an_agent_on_another_computer_or_moved_away_is_not_started(host, monkeypatch, capsys):
    from crewaimeat import spawner

    listed = [_gaii("here"), _gaii("there"), _gaii("moved")]
    monkeypatch.setattr(spawner, "read_node_roster", lambda run_mode="spawn": (listed, [], set()))
    monkeypatch.setattr(
        spawner, "daemon_identities", lambda: {_gaii("here"): "tunnel", _gaii("moved"): spawner.REFUSED}
    )
    assert host._node_resident_agents() == [_gaii("here")]
    said = capsys.readouterr().err
    assert "[host]" in said and "there" in said and "moved" in said, "said in the host's own log, by name"
    assert "[spawner]" not in said


def test_a_roster_that_could_not_be_read_is_not_an_empty_one(host, monkeypatch):
    from crewaimeat import spawner

    monkeypatch.setattr(
        spawner, "read_node_roster", lambda run_mode="spawn": ([], ["alice: unreadable (HTTP 503)"], {"alice"})
    )
    assert host._node_resident_agents() is None

    monkeypatch.setattr(spawner, "read_node_roster", lambda run_mode="spawn": ([_gaii("a")], [], set()))
    monkeypatch.setattr(spawner, "daemon_identities", lambda: None)
    assert host._node_resident_agents() is None, "a daemon that did not answer carries an unknown set, not none"


# --------------------------------------------------------------------------- #
# Following the roster: join without a restart, never twice, never dropped on a blink
# --------------------------------------------------------------------------- #
def test_a_new_always_on_agent_joins_without_a_restart(host, runs):
    stop, residents = threading.Event(), {}
    host._follow_residents(residents, [_gaii("first")], stop)
    _wait_started(runs, 1)
    host._follow_residents(residents, [_gaii("first"), _gaii("second")], stop)  # approved on the page
    _wait_started(runs, 2)
    assert runs.started == [_gaii("first"), _gaii("second")], "run by its GAII, as the spawner's worker is"
    assert host._status == {"first": "running", "second": "running"}
    host._follow_residents(residents, [_gaii("first"), _gaii("second")], stop)
    assert len(runs.started) == 2, "an agent that is running is not started again"


def test_an_unreadable_roster_starts_and_stops_nothing(host, runs):
    stop, residents = threading.Event(), {}
    host._follow_residents(residents, [_gaii("a")], stop)
    _wait_started(runs, 1)
    host._follow_residents(residents, None, stop)
    assert set(residents) == {_gaii("a")} and residents[_gaii("a")].thread.is_alive()


def test_what_the_host_keeps_resident_is_written_where_doctor_reads_it(host, runs):
    import json

    from crewaimeat import spawn_state

    host._follow_residents({}, [_gaii("ordered")], threading.Event())
    doc = json.loads(spawn_state.resident_roster_file().read_text(encoding="utf-8"))
    assert doc["agents"] == [_gaii("ordered")] and doc["read_at"]


def test_doctor_does_not_call_an_always_on_node_agent_a_ghost(tmp_path, monkeypatch):
    """It is in serve.json and has no crew file — the shape of a crew whose file vanished. The
    spawner's roster does not hold it (it is not spawn), so the host's own file has to."""
    from crewaimeat import spawn_state
    from crewaimeat.doctor import inventory

    home = tmp_path / ".aimeat"
    monkeypatch.setenv("AIMEAT_HOME", str(home))
    (tmp_path / "crews").mkdir()
    spawn_state.write_json(home / "serve.json", {"port": 1, "agents": [{"agent": "ordered"}]})
    spawn_state.write_json(spawn_state.roster_file(), {"read_at": "t", "agents": [_gaii("burst")]})
    spawn_state.write_json(spawn_state.resident_roster_file(), {"read_at": "t", "agents": [_gaii("ordered")]})
    names, _at, _unread = inventory._read_node_roster(tmp_path)
    assert names == {"burst", "ordered"}


# --------------------------------------------------------------------------- #
# How a thread ends, and when the agent is started again
# --------------------------------------------------------------------------- #
def test_an_agent_with_no_definition_is_not_restarted_every_half_minute(host, runs, monkeypatch, capsys):
    """Every start reads the definition and writes the runtime status. An agent that cannot start is
    asked again after `_READMIT_S`, not at every roster read."""
    from crewaimeat.crew_def import CrewDocError

    stop, residents, a = threading.Event(), {}, _gaii("undefined")
    runs.ending[a] = CrewDocError(["crews.registry.undefined holds no crew definition"], missing=True)
    host._follow_residents(residents, [a], stop)
    _settle(residents)
    assert "CANNOT START" in capsys.readouterr().err and host._status == {"undefined": "stopped"}
    host._follow_residents(residents, [a], stop)
    _settle(residents)
    assert runs.started == [a], "not again inside the wait"

    monkeypatch.setattr(host, "_READMIT_S", 0.0)
    host._follow_residents(residents, [a], stop)
    _settle(residents)
    assert runs.started == [a, a], "and again once it has passed, for as long as the node lists it"


def test_an_agent_a_spawn_worker_still_holds_is_tried_at_the_next_roster_read(host, runs):
    """The owner switches an agent from spawn to always-on while a worker is mid-run: `run_crew`
    exits 0 on the lock. That clears when the run ends, so it does not wait ten minutes."""
    stop, residents, a = threading.Event(), {}, _gaii("switching")
    runs.ending[a] = SystemExit(0)
    host._follow_residents(residents, [a], stop)
    _settle(residents)
    assert residents[a].busy is True
    del runs.ending[a]  # the worker has finished and released the lock
    host._follow_residents(residents, [a], stop)
    _wait_started(runs, 2)
    assert residents[a].thread.is_alive()


def test_a_refused_credential_ends_the_thread_and_the_agent_leaves_with_the_roster(host, runs, capsys):
    """A MOVE, seen from the old computer: the daemon loop exits on `auth_failed` by itself, and the
    next roster read no longer lists the agent here."""
    stop, residents, a = threading.Event(), {}, _gaii("moved")
    runs.ending[a] = SystemExit(2)
    host._follow_residents(residents, [a], stop)
    _settle(residents)
    assert residents[a].busy is False and "refused its credential" in capsys.readouterr().err
    host._follow_residents(residents, [], stop)
    assert residents == {} and host._status == {}
    assert "left the resident roster" in capsys.readouterr().err


def test_a_running_agent_that_leaves_the_roster_is_named_not_silently_kept(host, runs, capsys):
    """A thread cannot be stopped from outside. When the owner changes the run mode the loop keeps
    running until the host restarts, and the log says so once instead of looking as if it obeyed."""
    stop, residents, a = threading.Event(), {}, _gaii("switched")
    host._follow_residents(residents, [a], stop)
    _wait_started(runs, 1)
    host._follow_residents(residents, [], stop)
    host._follow_residents(residents, [], stop)
    said = capsys.readouterr().err
    assert said.count("no longer a resident agent") == 1
    assert a in residents and residents[a].thread.is_alive()


def test_a_crashing_agent_is_restarted_a_bounded_number_of_times(host, runs, monkeypatch, capsys):
    monkeypatch.setattr(host, "_RESTART_DELAY_S", 0)
    stop, residents, a = threading.Event(), {}, _gaii("crashy")
    runs.ending[a] = RuntimeError("boom")
    host._follow_residents(residents, [a], stop)
    _settle(residents)
    assert len(runs.started) == host._MAX_RESTARTS + 1
    assert host._status == {"crashy": "crashed"} and "leaving it for" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# The promise: a home that tells the node it keeps agents resident keeps a host to do it
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("value", "expected"),
    [("spawn,resident", True), (" Resident ", True), ("spawn", False), ("", False), (None, False)],
)
def test_the_host_reads_the_promise_from_the_variable_the_connector_reads(host, monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("AIMEAT_RUN_MODES", raising=False)
    else:
        monkeypatch.setenv("AIMEAT_RUN_MODES", value)
    assert host.declares_resident() is expected


def test_an_empty_host_returns_at_once_in_a_home_that_made_no_promise(host, monkeypatch):
    """Today's behaviour for an all-spawn fleet, unchanged: the window comes back."""
    monkeypatch.delenv("AIMEAT_RUN_MODES", raising=False)
    monkeypatch.setattr(host, "_node_resident_agents", lambda: [])
    monkeypatch.setattr(host, "_spawn_mode_files", lambda: set())
    assert host.run_host() == 1


def test_an_empty_host_stays_and_follows_the_roster_in_a_home_that_promised(host, runs, monkeypatch):
    """With AIMEAT_RUN_MODES=spawn,resident the node accepts an always-on agent for this computer at
    any moment. A host that had returned would leave it run by nobody — the fault this fixes."""
    monkeypatch.setenv("AIMEAT_RUN_MODES", "spawn,resident")
    monkeypatch.setattr(host, "_spawn_mode_files", lambda: set())
    monkeypatch.setattr(host, "_ROSTER_INTERVAL_S", 0.0)
    answers = iter([[], [], [_gaii("ordered")]])
    monkeypatch.setattr(host, "_node_resident_agents", lambda: next(answers, [_gaii("ordered")]))
    ticks = {"n": 0}

    def tick(_s):
        ticks["n"] += 1
        if runs.started or ticks["n"] > 50:
            raise KeyboardInterrupt

    monkeypatch.setattr(host.time, "sleep", tick)
    assert host.run_host() == 0
    assert runs.started == [_gaii("ordered")], "the agent approved after start was picked up"


def test_a_named_subset_runs_exactly_what_was_named(host, runs, monkeypatch):
    """`--agents a,b` has always meant a and b. It does not also pull in the node's resident agents."""
    monkeypatch.setenv("AIMEAT_RUN_MODES", "spawn,resident")
    monkeypatch.setattr(host, "_spawn_mode_files", lambda: set())
    monkeypatch.setattr(host, "_node_resident_agents", lambda: pytest.fail("a subset host must not ask"))
    assert host.run_host(["nobody-by-this-name"]) == 1
    assert runs.started == []


@pytest.mark.local_process
def test_a_host_with_nothing_to_run_imports_no_crewai(tmp_path):
    """The host that waits for an always-on agent must cost what the spawner costs. Importing forge
    to list `crews/` loaded crewai (217 MB resident, measured 2026-10-10) before the host knew it
    had nothing to run."""
    import subprocess
    import sys

    (tmp_path / "crews").mkdir()
    code = "\n".join(
        [
            "import sys",
            "from crewaimeat import fleet_host, spawner",
            "spawner.read_node_roster = lambda run_mode='spawn': ([], [], set())",
            "assert fleet_host._select_crews(None) == []",
            "assert fleet_host._node_resident_agents() == []",
            "assert fleet_host.declares_resident() is False",
            "heavy = sorted(m for m in ('crewai', 'litellm', 'aimeat_crewai') if m in sys.modules)",
            "print('HEAVY', heavy)",
        ]
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=str(tmp_path), check=False)
    assert "HEAVY []" in proc.stdout, proc.stdout + proc.stderr

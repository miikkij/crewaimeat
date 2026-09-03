"""The two things a spawner must survive that a static fleet never had to: a roster that changes
under it, and a Crew-tab button aimed at an agent with no runtime up.

Both come from the node's basic-agents button (wish `wish-crewaimeat-perusagentit-spawn`): it enrols
agents into a LIVE daemon with no restart, and those agents have no crew file on this disk at all.

Deterministic — no node, no LLM, no real process, no socket. `roster_fn`, `invoke_fn`, `spawn_fn` and
`wake_fn` are the injection seams.
"""

from __future__ import annotations

from pathlib import Path

import pytest


class _Ok:
    """A requests-shaped response for the loopback POST the spawner makes."""

    status_code = 200

    @staticmethod
    def json():
        return {"ok": True}


def _repo(tmp_path: Path, body: str, name: str = "demo_crew.py") -> Path:
    crews = tmp_path / "crews"
    crews.mkdir(parents=True, exist_ok=True)
    (crews / name).write_text(body, encoding="utf-8")
    return tmp_path


def _crew_src(name: str) -> str:
    """A minimal spawn-mode crew file. Built here so the escapes live in ONE place."""
    return "\n".join(
        [
            f'AGENT_NAME = "{name}"',
            'RUN_MODE = "spawn"',
            "",
            "def build_domain(ctx):",
            "    ...",
            "",
            "def run():",
            "    ...",
            "",
        ]
    )


def _spawner(**kw):
    from crewaimeat.spawner import Spawner

    kw.setdefault("agents", [])
    kw.setdefault("root", Path.cwd())
    kw.setdefault("spawn_fn", lambda *_: None)
    kw.setdefault("wake_fn", lambda *_: False)
    return Spawner(**kw)


# --------------------------------------------------------------------------- #
# Vocabulary: the node's words are canonical, ours stay accepted
# --------------------------------------------------------------------------- #
def test_continuous_is_accepted_as_an_alias_for_resident(tmp_path):
    """`resident` is the node's word (agent record `run_mode`) and therefore the canonical one.
    `continuous` is what crewaimeat said first and must keep working."""
    from crewaimeat import agent_manifest as am

    root = _repo(
        tmp_path,
        'AGENT_NAME = "c"\nRUN_MODE = "continuous"\n\ndef build_domain(ctx):\n    ...\n\ndef run():\n    ...\n',
    )
    assert am.manifest_for("c", root).effective_run_mode == am.RUN_RESIDENT
    assert am.normalise_run_mode("CONTINUOUS ") == am.RUN_RESIDENT
    assert am.normalise_run_mode("sapwn") is None
    assert am.RUN_MODES == ("resident", "spawn")


def test_the_validator_accepts_the_alias_and_advertises_the_canonical_names():
    from crewaimeat.crew_def import validate_crew_doc

    base = {
        "agent_name": "x",
        "agents": [{"role": "r", "goal": "g", "backstory": "b"}],
        "tasks": [{"id": "t", "description": "{{ctx.prompt}}", "expected_output": "o", "agent": "r"}],
    }
    for mode in ("spawn", "resident", "continuous"):
        assert validate_crew_doc({**base, "run_mode": mode}) == []
    bad = validate_crew_doc({**base, "run_mode": "sapwn"})
    assert bad and "resident" in bad[0] and "spawn" in bad[0]


# --------------------------------------------------------------------------- #
# The roster changes under the daemon
# --------------------------------------------------------------------------- #
def test_a_new_agent_joins_the_roster_without_a_restart():
    roster = ["a"]
    sp = _spawner(invoke_fn=lambda *_: None, roster_fn=lambda: list(roster))
    sp.refresh_roster()
    assert set(sp.state) == {"a"}
    roster.append("b")  # the node's button enrols a second agent into the live daemon
    sp.refresh_roster()
    assert {k for k, v in sp.state.items() if not v.retired} == {"a", "b"}


def test_an_agent_that_leaves_the_roster_is_retired_and_disappears_from_the_status():
    roster = ["a", "b"]
    sp = _spawner(invoke_fn=lambda *_: None, roster_fn=lambda: list(roster))
    sp.refresh_roster()
    roster.remove("b")
    sp.refresh_roster()
    assert sp.state["b"].retired is True
    assert sp.state["a"].retired is False
    assert "b" not in sp.snapshot()["agents"]


def test_a_failing_roster_read_keeps_the_agents_we_already_serve():
    """An unreachable node must not empty the roster — that would silently stop the fleet."""
    calls = {"n": 0}

    def roster():
        calls["n"] += 1
        if calls["n"] == 1:
            return ["a"]
        raise RuntimeError("node unreachable")

    sp = _spawner(roster_fn=roster)
    sp.refresh_roster()
    sp.refresh_roster()
    assert sp.state["a"].retired is False


def test_refresh_is_idempotent_and_does_not_restart_a_live_agent():
    sp = _spawner(roster_fn=lambda: ["a"])
    sp.refresh_roster()
    first = sp.state["a"]
    sp.refresh_roster()
    assert sp.state["a"] is first, "an unchanged agent must not be torn down and rebuilt"


# --------------------------------------------------------------------------- #
# Where the roster comes from: repo crews UNION node agents, ONE CALL PER OWNER
# --------------------------------------------------------------------------- #
NODE = "n1"


def _serve(tmp_path, agents):
    from crewaimeat import spawn_state

    spawn_state.write_json(spawn_state.aimeat_home() / "serve.json", {"port": 1, "schema_version": 2, "agents": agents})


def _both_owners():
    return [
        {"agent": "bot", "gaii": f"bot#alice@{NODE}", "owner": "alice"},
        {"agent": "bot", "gaii": f"bot#bob@{NODE}", "owner": "bob"},
    ]


class _Resp:
    def __init__(self, rows):
        self.status_code = 200
        self._rows = rows

    def json(self):
        return {"data": {"agents": self._rows}}


def test_the_roster_asks_once_per_owner_because_the_listing_is_owner_scoped(monkeypatch, tmp_path):
    """MEASURED 2026-09-02: `GET /v1/agents` asked as one of alice's agents returns alice's and ONLY
    alice's. One call would therefore quietly serve half a two-owner daemon."""
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    import requests

    from crewaimeat import spawner

    _serve(tmp_path, _both_owners())
    seen: list[str] = []

    def _get(url, **kw):
        who = kw["headers"]["X-Aimeat-Agent"]
        seen.append(who)
        owner = who.split("#", 1)[1].split("@", 1)[0]
        return _Resp([{"name": "burst", "gaii": f"burst#{owner}@{NODE}", "owner": owner, "run_mode": "spawn"}])

    monkeypatch.setattr(requests, "get", _get)
    agents, note = spawner.node_spawn_agents()
    assert sorted(seen) == [f"bot#alice@{NODE}", f"bot#bob@{NODE}"], "one call per owner, not one in total"
    assert agents == [f"burst#alice@{NODE}", f"burst#bob@{NODE}"]
    assert note is None


def test_the_roster_is_gaiis_not_names(monkeypatch, tmp_path):
    """The daemon REFUSES a bare name two owners share (measured: 400, naming both). A roster of
    names could not be parked on at all."""
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    import requests

    from crewaimeat import spawner

    _serve(tmp_path, [{"agent": "bot", "gaii": f"bot#alice@{NODE}", "owner": "alice"}])
    monkeypatch.setattr(
        requests,
        "get",
        lambda *a, **k: _Resp([{"name": "concierge", "gaii": f"concierge#alice@{NODE}", "run_mode": "spawn"}]),
    )
    agents, _ = spawner.node_spawn_agents()
    assert agents == [f"concierge#alice@{NODE}"], "the identity is the GAII, never the bare name"


def test_the_roster_read_asks_the_node_to_filter(monkeypatch, tmp_path):
    """The point of the filter is that a 30-second refresh does not drag every agent across the wire
    to keep a handful. If the request stops carrying it, this fails."""
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    import requests

    from crewaimeat import spawner

    _serve(tmp_path, [{"agent": "bot", "gaii": f"bot#alice@{NODE}", "owner": "alice"}])
    seen: dict = {}

    def _get(url, **kw):
        seen["url"], seen["params"] = url, kw.get("params")
        return _Resp([{"name": "burst", "gaii": f"burst#alice@{NODE}", "run_mode": "spawn"}])

    monkeypatch.setattr(requests, "get", _get)
    spawner.node_spawn_agents()
    assert seen["params"] == {"run_mode": "spawn"}, "the node must do the filtering, not us"
    assert seen["url"].endswith("/v1/agents")


def test_an_ignored_filter_serves_nothing_extra_rather_than_the_whole_fleet(monkeypatch, tmp_path):
    """An unknown query parameter is IGNORED, not refused, so an older node answers `?run_mode=spawn`
    with EVERY agent it has. Trusting that would put the whole fleet in spawn mode."""
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    import requests

    from crewaimeat import spawner

    _serve(tmp_path, [{"agent": "bot", "gaii": f"bot#alice@{NODE}", "owner": "alice"}])
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp([{"name": "x", "mode": "task-runner"}]))
    agents, note = spawner.node_spawn_agents()
    assert agents == []
    assert note and "run_mode=spawn" in note


def test_an_unreachable_node_leaves_the_local_crews_serving(monkeypatch, tmp_path):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    import requests

    from crewaimeat import spawner

    _serve(tmp_path, [{"agent": "bot", "gaii": f"bot#alice@{NODE}", "owner": "alice"}])

    def _boom(*a, **k):
        raise OSError("connection refused")

    monkeypatch.setattr(requests, "get", _boom)
    agents, note = spawner.node_spawn_agents()
    assert agents == []
    assert note and "unreadable" in note


def test_a_repo_crew_the_daemon_does_not_carry_is_not_served(monkeypatch, tmp_path):
    """MEASURED 2026-09-02: parking on an agent this daemon does not hold is refused every time and
    never heals — it produced 14 627 rejected polls before the run was stopped."""
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    from crewaimeat import spawner

    root = _repo(
        tmp_path,
        _crew_src("stranger"),
    )
    _serve(tmp_path, [{"agent": "bot", "gaii": f"bot#alice@{NODE}", "owner": "alice"}])
    monkeypatch.setattr(spawner, "node_spawn_agents", lambda: ([f"bot#alice@{NODE}"], None))
    monkeypatch.setattr(spawner, "_LAST_NOTE", {})
    assert spawner.discover_agents(root) == [f"bot#alice@{NODE}"], "an agent the daemon lacks is not ours to park on"


def test_a_repo_crew_is_not_served_just_because_it_asks(monkeypatch, tmp_path, capsys):
    """A crew file's RUN_MODE is a REQUEST; the node's roster is the fact.

    A checkout holds whatever the developer is working on and a connector home is somebody's real
    fleet — so a repo crew declaring spawn must not appear as if its owner had asked for it. This is
    the same principle as not offering `crews/*.py` as an owner's delegable peers. It must also be
    SAID: a crew that asks for spawn and does not get it just runs as a thread, which looks like
    nothing happened.
    """
    from crewaimeat import spawner

    root = _repo(tmp_path, _crew_src("mine"))
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    _serve(tmp_path, [{"agent": "mine", "gaii": f"mine#alice@{NODE}", "owner": "alice"}])
    monkeypatch.setattr(spawner, "node_spawn_agents", lambda: ([], None))
    monkeypatch.setattr(spawner, "_LAST_NOTE", {})

    assert spawner.discover_agents(root) == []
    assert "mine" in capsys.readouterr().err  # and the reason was printed, not swallowed


def test_the_node_is_the_only_source_of_the_roster(monkeypatch, tmp_path):
    """What the node lists is served, whether or not this checkout has a crew file for it."""
    from crewaimeat import spawner

    root = _repo(tmp_path, _crew_src("mine"))
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    _serve(tmp_path, [{"agent": "bot", "gaii": f"bot#alice@{NODE}", "owner": "alice"}])
    monkeypatch.setattr(spawner, "node_spawn_agents", lambda: ([f"bot#alice@{NODE}"], None))
    monkeypatch.setattr(spawner, "_LAST_NOTE", {})
    assert spawner.discover_agents(root) == [f"bot#alice@{NODE}"]


def test_the_fleet_host_skips_exactly_what_the_spawner_serves(monkeypatch, tmp_path):
    """The two runtimes must read ONE source, or they disagree about who owns an agent.

    If the host skipped a crew the spawner does not serve, that agent would run NOWHERE — which is
    why the host now asks the node too, and why an unreachable node leaves every crew to the host.
    """
    from crewaimeat import agent_manifest, fleet_host

    root = _repo(tmp_path, _crew_src("mine"))
    monkeypatch.chdir(root)
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    agent_manifest.all_manifests(root, refresh=True)

    monkeypatch.setattr("crewaimeat.spawner.node_spawn_agents", lambda: ([], None))
    assert fleet_host._spawn_mode_files() == set(), "nothing on the node -> the host runs it"

    monkeypatch.setattr("crewaimeat.spawner.node_spawn_agents", lambda: ([f"mine#alice@{NODE}"], None))
    # The crew file is `demo_crew.py`; the AGENT it declares is `mine`. The node names AGENTS, so the
    # match is by agent name and the filename is beside the point.
    assert fleet_host._spawn_mode_files() == {(root / "crews" / "demo_crew.py").resolve()}


# --------------------------------------------------------------------------- #
# Invoke: the Crew tab's Validate / Try against an agent with no runtime up
# --------------------------------------------------------------------------- #
def _one_agent(monkeypatch, tmp_path, invoke_fn):
    from crewaimeat.spawner import AgentState

    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    sp = _spawner(agents=["a"], invoke_fn=invoke_fn)
    sp.state["a"] = AgentState("a")
    monkeypatch.setattr(sp, "_serve_port", lambda: 1)
    return sp


def test_invoke_is_answered_and_posted_back(monkeypatch, tmp_path):
    posted: list[tuple[str, dict]] = []
    sp = _one_agent(monkeypatch, tmp_path, lambda agent, frame: {"ok": True, "result": {"errors": []}})
    import requests

    monkeypatch.setattr(requests, "post", lambda url, **kw: posted.append((url, kw.get("json"))) or _Ok())
    sp._answer_invoke("a", {"id": "inv-1", "capability": "crew.validate", "input": {}})
    assert posted and posted[0][1] == {"ok": True, "result": {"errors": []}}
    assert "/local/invoke/inv-1/result" in posted[0][0]
    assert sp.state["a"].invokes == 1


def test_a_raising_invoke_worker_still_answers(monkeypatch, tmp_path):
    """A button that spins forever is worse than one that says why."""
    posted: list[dict] = []

    def boom(agent, frame):
        raise RuntimeError("worker died")

    sp = _one_agent(monkeypatch, tmp_path, boom)
    import requests

    monkeypatch.setattr(requests, "post", lambda url, **kw: posted.append(kw.get("json")) or _Ok())
    sp._answer_invoke("a", {"id": "inv-2", "capability": "crew.try", "input": {}})
    assert posted and posted[0]["ok"] is False
    assert posted[0]["result"]["code"] == "HANDLER_ERROR"


def test_a_worker_that_returns_nothing_still_answers(monkeypatch, tmp_path):
    posted: list[dict] = []
    sp = _one_agent(monkeypatch, tmp_path, lambda agent, frame: None)
    import requests

    monkeypatch.setattr(requests, "post", lambda url, **kw: posted.append(kw.get("json")) or _Ok())
    sp._answer_invoke("a", {"id": "inv-3", "capability": "crew.validate", "input": {}})
    assert posted and posted[0]["ok"] is False


def test_spawner_refuses_a_real_invoke_worker_under_pytest():
    sp = _spawner(agents=["a"])
    with pytest.raises(RuntimeError, match="pytest"):
        sp._run_invoke_worker("a", {"id": "x", "capability": "crew.validate", "input": {}})


# --------------------------------------------------------------------------- #
# The worker's own invoke path (what the spawned process actually does)
# --------------------------------------------------------------------------- #
def test_worker_answers_a_validate_frame_from_a_file(tmp_path):
    """`crew.validate` needs no node, no LLM and no agent identity — which is why a 2.6 s worker fits
    inside the node's 30 s ceiling."""
    import json

    from crewaimeat.run_once import answer_invoke

    job = tmp_path / "inv.json"
    job.write_text(
        json.dumps(
            {
                "agent": "a",
                "id": "i1",
                "capability": "crew.validate",
                "input": {"doc": {"agent_name": "x", "agents": [], "tasks": []}},
            }
        ),
        encoding="utf-8",
    )
    assert answer_invoke(job) == 0
    out = json.loads((tmp_path / "inv.out.json").read_text(encoding="utf-8"))
    assert out["ok"] is True
    assert out["result"]["errors"], "an empty agents/tasks doc must come back with problems listed"


def test_worker_answers_an_unknown_capability_rather_than_hanging(tmp_path):
    import json

    from crewaimeat.run_once import answer_invoke

    job = tmp_path / "inv.json"
    job.write_text(json.dumps({"agent": "a", "id": "i2", "capability": "nope", "input": {}}), encoding="utf-8")
    assert answer_invoke(job) == 0
    out = json.loads((tmp_path / "inv.out.json").read_text(encoding="utf-8"))
    assert out["ok"] is False

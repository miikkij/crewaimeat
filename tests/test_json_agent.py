"""The node-backed JSON agent — what it promises when a publish goes wrong.

The runtime's whole reason to exist is that the crew follows the node. That makes two failures
routine rather than exceptional — a read that does not answer, and a definition somebody just broke —
and neither may take a working agent down. An agent that goes dark because of a typo is worse than
one running yesterday's definition while the typo is fixed.

No node and no model here: `read_owner_key` and `_aimeat_call` are stubbed, so every assertion is
about what the runtime decides.
"""

from __future__ import annotations

import json

import pytest

import crewaimeat.json_agent as ja
from crewaimeat.crew_def import CrewDocError

DOC_V1 = {
    "agent_name": "node-agent",
    "agents": [{"name": "w", "role": "Writer", "goal": "Answer", "backstory": "Plainly."}],
    "tasks": [{"id": "t", "agent": "w", "description": "Do:\n{{ctx.prompt}}", "expected_output": "A line."}],
}
DOC_V2 = {**DOC_V1, "tasks": [{**DOC_V1["tasks"][0], "expected_output": "Two lines."}]}


def _envelope(doc, revision=None, published_at="2026-08-28T00:00:00Z"):
    """Both publishers in one fixture, because the runtime has to read both.

    The CLI writes `{version, publishedAt, agent_name, doc}` with NO number —
    `test_the_fixture_matches_what_the_registry_actually_writes` holds that honest. The node's own
    publish route adds an integer `revision`, numbering from the kept history. An earlier version of
    this fixture invented a `revision` key from the reader's docstring; every test passed and the
    live agent reported `revision: null`, because no writer wrote that key.
    """
    env = {
        "version": 1,
        "publishedAt": published_at,
        "agent_name": doc["agent_name"],
        "publishedBy": doc["agent_name"],
        "doc": doc,
    }
    if revision is not None:
        env["revision"] = revision
    return env


@pytest.fixture
def node(monkeypatch):
    """A fake node: `reads` is what the registry key answers, `writes` records the status reports."""
    state: dict = {"value": _envelope(DOC_V1), "writes": []}

    def _read(agent, key):
        v = state["value"]
        if isinstance(v, Exception):
            raise v
        return v

    monkeypatch.setattr(ja, "read_owner_key", _read)
    monkeypatch.setattr(
        "crewaimeat.aimeat_crew._aimeat_call",
        lambda a, tool, payload: state["writes"].append({"agent": a, "tool": tool, **payload}) or {"ok": True},
    )
    return state


def test_a_cli_publish_has_no_revision_number(node):
    """A CLI publish deliberately does not number (aimeat-dev, spec v6): a second counter is the same
    mistake as a second validator. The tab renders the absence as "published from outside this tab"."""
    doc, revision = ja.load_def("node-agent")
    assert doc == DOC_V1 and revision is None


def test_the_nodes_own_publish_carries_its_number(node):
    node["value"] = _envelope(DOC_V1, revision=4)
    doc, revision = ja.load_def("node-agent")
    assert doc == DOC_V1 and revision == 4


def test_a_revision_that_is_not_a_number_is_not_a_revision(node):
    """`publishedAt` stood in for the number once and the tab rendered "Live: revision 0" beside
    "Runtime loaded revision 2026-08-28T02:43:19+03:00" — two notions of one word in one box. `True`
    is here because bool is an int in Python and would have printed as a plausible "1"."""
    for bad in ("2026-08-28T00:00:00Z", True, 1.5, {"n": 1}):
        node["value"] = _envelope(DOC_V1, revision=bad)
        _doc, revision = ja.load_def("node-agent")
        assert revision is None, f"{bad!r} was taken for a revision number"


def test_a_bare_document_still_loads(node):
    """`crew_registry` and a hand-written def store the doc itself, not an envelope. Both must work,
    or the runtime is only compatible with the newest writer."""
    node["value"] = DOC_V1
    doc, revision = ja.load_def("node-agent")
    assert doc == DOC_V1 and revision is None


def test_an_agent_with_no_definition_says_exactly_that(node):
    node["value"] = None
    with pytest.raises(CrewDocError) as e:
        ja.load_def("node-agent")
    assert "registered but not yet defined" in " ".join(e.value.errors)


def test_a_new_revision_is_picked_up_on_the_next_build(node):
    """The hot reload. `run_crew` calls build_domain once per task, so re-reading there is the whole
    mechanism — publish, and the next task is the new agent. No restart, no push handler."""
    live = ja.Definition("node-agent", DOC_V1, 3)
    node["value"] = _envelope(DOC_V2, revision=9)

    assert live.refresh() == DOC_V2
    assert live.revision == 9
    reported = [w for w in node["writes"] if w["tool"] == "aimeat_memory_write"]
    assert reported and reported[-1]["value"]["revision"] == 9
    assert reported[-1]["value"]["ok"] is True


def test_a_broken_publish_does_not_take_the_agent_down(node, capsys):
    """Somebody saves a typo. The agent keeps running yesterday's definition and says why — going
    dark would be the worse outcome, and the tab needs the reason, not just the old number."""
    node["value"] = _envelope({**DOC_V1, "temperature": 9}, revision=9)
    live = ja.Definition("node-agent", DOC_V1, 3)

    assert live.refresh() == DOC_V1, "still the last definition that validated"
    assert live.revision == 3

    err = capsys.readouterr().err
    assert "definition REJECTED" in err and "temperature" in err
    status = [w for w in node["writes"] if w["tool"] == "aimeat_memory_write"][-1]["value"]
    assert status["ok"] is False and status["revision"] == 3
    assert any("temperature" in e for e in status["errors"]), "the tab shows WHY it is on the old one"


def test_a_read_that_fails_keeps_the_agent_on_its_feet(node, capsys):
    """A tunnel blip is not a definition change. Same floor, different reason — and no status write,
    because we learned nothing about what the node holds."""
    node["value"] = ConnectionError("tunnel down")
    live = ja.Definition("node-agent", DOC_V1, 3)

    assert live.refresh() == DOC_V1
    assert "could not read" in capsys.readouterr().err
    assert not [w for w in node["writes"] if w["tool"] == "aimeat_memory_write"]


def test_the_runtime_reports_what_it_actually_loaded(node):
    """The node knows what it stored; only the runtime knows what a fleet picked up. Without this the
    tab's 'published' state would be a guess."""
    ja.report_runtime("node-agent", revision=7, ok=True)
    w = node["writes"][-1]
    assert w["key"] == "crews.runtime.node-agent" and w["visibility"] == "owner"
    assert w["value"]["revision"] == 7 and w["value"]["ok"] is True
    assert w["value"]["runtime"].startswith("crewaimeat ") and w["value"]["loadedAt"].endswith("+00:00")


def test_a_status_write_that_fails_is_not_fatal(node, monkeypatch, capsys):
    """A status write that kills the agent whose status it describes would be an absurd way to lose
    a fleet."""
    monkeypatch.setattr(
        "crewaimeat.aimeat_crew._aimeat_call", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no tunnel"))
    )
    ja.report_runtime("node-agent", revision=1, ok=True)
    assert "could not report runtime status" in capsys.readouterr().err


def test_the_loader_carries_what_the_fleet_and_doctor_read_statically(tmp_path):
    """Fleet discovery scans crews/*_crew.py and doctor reads AGENT_NAME + build_domain from the AST,
    so a node-backed agent still needs a file — but it holds no definition, only the name."""
    path = ja.write_loader("demo-agent", crews_dir=str(tmp_path))
    src = (tmp_path / "demo_agent_crew.py").read_text(encoding="utf-8")

    assert path.endswith("demo_agent_crew.py")
    assert 'AGENT_NAME = "demo-agent"' in src
    assert "def build_domain(ctx)" in src and "def run()" in src
    assert "crews.registry.demo-agent" in src, "the file says where the crew actually is"
    assert '"agents"' not in src and '"tasks"' not in src, "no definition on disk"


def test_the_loader_never_overwrites_an_existing_crew(tmp_path):
    """Replacing a hand-written Python crew with a stub loses the whole agent."""
    (tmp_path / "demo_agent_crew.py").write_text("# a real crew", encoding="utf-8")
    with pytest.raises(FileExistsError):
        ja.write_loader("demo-agent", crews_dir=str(tmp_path))
    assert (tmp_path / "demo_agent_crew.py").read_text(encoding="utf-8") == "# a real crew"


def test_the_fixture_matches_what_the_registry_actually_writes(monkeypatch):
    """The double and the writer must have the same shape, or every test here is testing nothing.

    This is the failure the fixture's docstring describes, caught at its source: `_envelope` is
    compared against the envelope `publish_crew_def` builds, so inventing a field in either place
    fails here instead of in production three hours later.
    """
    from crewaimeat import crew_registry

    sent: dict = {}
    monkeypatch.setattr(crew_registry, "_aimeat_call", lambda a, t, p: sent.update(p) or {"ok": True})
    crew_registry.publish_crew_def(DOC_V1, agent="node-agent")

    written = sent["value"]
    assert set(written) == set(_envelope(DOC_V1)), (
        f"the registry writes {sorted(written)}; the test double builds {sorted(_envelope(DOC_V1))}"
    )
    assert written["doc"] == DOC_V1
    assert "revision" not in written, "the CLI must not number — that counter is the node route's"


def _spec_for(node, monkeypatch):
    """Run `run_json_agent` up to the point it would hand the spec to the daemon, and return it."""
    captured: dict = {}
    # `run_crew` is imported inside the function, so the patch has to land on the SOURCE module.
    monkeypatch.setattr("crewaimeat.aimeat_crew.run_crew", lambda spec: captured.setdefault("spec", spec))
    ja.run_json_agent("node-agent")
    return captured["spec"]


def test_a_publish_wake_refreshes_an_idle_agent(node, monkeypatch):
    """An idle agent builds nothing, so without this it would advertise its start-up revision forever
    and the tab would read "published, not in force" for a definition that IS in force."""
    spec = _spec_for(node, monkeypatch)
    assert "records" in spec.listen_for, "no records subscription, so the wake never arrives"

    node["value"] = _envelope(DOC_V2, revision=9)
    spec.on_record({"type": "crew.def_updated", "key": "crews.registry.node-agent"})

    status = [w for w in node["writes"] if w["tool"] == "aimeat_memory_write"][-1]["value"]
    assert status["revision"] == 9 and status["ok"] is True


def test_another_record_event_is_left_alone(node, monkeypatch):
    """The records queue is shared. Refreshing the definition on somebody else's event would read the
    node on every unrelated push, and would swallow a handler the crew declared for itself."""
    spec = _spec_for(node, monkeypatch)
    before = len([w for w in node["writes"] if w["tool"] == "aimeat_memory_write"])

    spec.on_record({"type": "workspace.record.created", "space": "notes"})

    after = len([w for w in node["writes"] if w["tool"] == "aimeat_memory_write"])
    assert after == before, "an unrelated record event triggered a definition read"


def test_a_numberless_definition_reports_no_revision_field_at_all(node):
    """OMITTED, not null. The tab reads a missing `revision` as "loaded the definition, published
    from outside this tab"; sending null would make it choose between printing the word and treating
    absent and null alike, and the second is a guess we would be forcing on it."""
    ja.report_runtime("node-agent", revision=None, ok=True)
    written = [w for w in node["writes"] if w["tool"] == "aimeat_memory_write"][-1]["value"]
    assert "revision" not in written, "a numberless publish must leave the field out, not send null"
    assert written["ok"] is True and written["loadedAt"]


# ── first start: the agent publishes what the forge staged ───────────────────────────────────────
def _stage(tmp_path, monkeypatch, doc):
    """A `crew_defs/<name>.json` on disk, the way the forge leaves one."""
    from crewaimeat.forge_json import _doc_base

    (tmp_path / "crew_defs").mkdir(parents=True, exist_ok=True)
    # The forge names the file by `_doc_base`, not by the agent name verbatim — mirror the real
    # writer, or this fixture tests a path nothing produces.
    path = tmp_path / "crew_defs" / f"{_doc_base(doc.get('agent_name', 'x'))}.json"
    path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr("crewaimeat.forge._project_root", lambda: tmp_path)
    return path


def test_an_empty_key_is_seeded_from_the_staged_definition(node, tmp_path, monkeypatch):
    """The forge cannot publish for the agent — the token does not exist until a person approves, and
    `register_agent` is deliberately non-blocking. The first party holding that token is the agent."""
    _stage(tmp_path, monkeypatch, DOC_V1)
    node["value"] = None  # registered, never defined

    published: dict = {}

    def _publish(doc, *, agent, visibility="owner", allow_foreign_namespace=False):
        published.update({"doc": doc, "agent": agent})
        node["value"] = _envelope(doc)
        return True, f"crews.registry.{agent}", "ok"

    monkeypatch.setattr("crewaimeat.crew_registry.publish_crew_def", _publish)
    doc, revision = ja.seed_from_staged("node-agent")

    assert doc == DOC_V1 and revision is None
    assert published["agent"] == "node-agent", "it must publish as ITSELF or the tab cannot see it"


def test_a_staged_definition_for_another_agent_is_refused(node, tmp_path, monkeypatch, capsys):
    """One stray file must never make an agent into somebody else."""
    _stage(tmp_path, monkeypatch, DOC_V1)  # sets _project_root; then plant the wrong doc at OUR name
    from crewaimeat.forge_json import _doc_base

    (tmp_path / "crew_defs" / f"{_doc_base('node-agent')}.json").write_text(
        json.dumps({**DOC_V1, "agent_name": "someone-else"}), encoding="utf-8"
    )
    called: list = []
    monkeypatch.setattr("crewaimeat.crew_registry.publish_crew_def", lambda *a, **k: called.append(1))

    assert ja.seed_from_staged("node-agent") is None
    assert not called and "not this agent" in capsys.readouterr().err


def test_nothing_staged_is_simply_nothing(node, tmp_path, monkeypatch):
    monkeypatch.setattr("crewaimeat.forge._project_root", lambda: tmp_path)
    assert ja.seed_from_staged("node-agent") is None


def test_seeding_is_only_ever_offered_for_an_EMPTY_key(node, tmp_path, monkeypatch, capsys):
    """THE ONE THAT MATTERS. A definition that exists and fails to validate is somebody's mistake to
    read, and an agent someone has since edited in the tab must never be reset to what the forge first
    imagined. So a bad document must reach the operator, not be overwritten from disk."""
    _stage(tmp_path, monkeypatch, DOC_V1)
    node["value"] = _envelope({**DOC_V1, "temperature": 9})  # published, and wrong
    called: list = []
    monkeypatch.setattr("crewaimeat.crew_registry.publish_crew_def", lambda *a, **k: called.append(1))
    monkeypatch.setattr("crewaimeat.aimeat_crew.run_crew", lambda spec: None)

    with pytest.raises(CrewDocError):
        ja.run_json_agent("node-agent")

    assert not called, "a staged file was published over a definition that merely failed validation"
    assert "temperature" in capsys.readouterr().err, "the operator has to see WHY it refused"

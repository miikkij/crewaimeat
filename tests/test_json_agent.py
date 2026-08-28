"""The node-backed JSON agent — what it promises when a publish goes wrong.

The runtime's whole reason to exist is that the crew follows the node. That makes two failures
routine rather than exceptional — a read that does not answer, and a definition somebody just broke —
and neither may take a working agent down. An agent that goes dark because of a typo is worse than
one running yesterday's definition while the typo is fixed.

No node and no model here: `read_owner_key` and `_aimeat_call` are stubbed, so every assertion is
about what the runtime decides.
"""

from __future__ import annotations

import pytest

import crewaimeat.json_agent as ja
from crewaimeat.crew_def import CrewDocError

DOC_V1 = {
    "agent_name": "node-agent",
    "agents": [{"name": "w", "role": "Writer", "goal": "Answer", "backstory": "Plainly."}],
    "tasks": [{"id": "t", "agent": "w", "description": "Do:\n{{ctx.prompt}}", "expected_output": "A line."}],
}
DOC_V2 = {**DOC_V1, "tasks": [{**DOC_V1["tasks"][0], "expected_output": "Two lines."}]}


def _envelope(doc, revision="2026-08-28T00:00:00Z"):
    """The envelope `crew_registry.publish_crew_def` really writes — no more, no less.

    `test_the_fixture_matches_what_the_registry_actually_writes` holds this honest. An earlier
    version of this fixture invented a `revision` key from the reader's docstring; every test passed
    and the live agent reported `revision: null`, because no writer has ever written that key.
    """
    return {"version": 1, "publishedAt": revision, "agent_name": doc["agent_name"], "doc": doc}


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


def test_the_definition_comes_from_the_node_with_its_revision(node):
    doc, revision = ja.load_def("node-agent")
    assert doc == DOC_V1 and revision == "2026-08-28T00:00:00Z"


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
    live = ja.Definition("node-agent", DOC_V1, "2026-08-28T00:00:00Z")
    node["value"] = _envelope(DOC_V2, revision="2026-08-28T09:00:00Z")

    assert live.refresh() == DOC_V2
    assert live.revision == "2026-08-28T09:00:00Z"
    reported = [w for w in node["writes"] if w["tool"] == "aimeat_memory_write"]
    assert reported and reported[-1]["value"]["revision"] == "2026-08-28T09:00:00Z"
    assert reported[-1]["value"]["ok"] is True


def test_a_broken_publish_does_not_take_the_agent_down(node, capsys):
    """Somebody saves a typo. The agent keeps running yesterday's definition and says why — going
    dark would be the worse outcome, and the tab needs the reason, not just the old number."""
    node["value"] = _envelope({**DOC_V1, "temperature": 9}, revision="2026-08-28T09:00:00Z")
    live = ja.Definition("node-agent", DOC_V1, "2026-08-28T00:00:00Z")

    assert live.refresh() == DOC_V1, "still the last definition that validated"
    assert live.revision == "2026-08-28T00:00:00Z"

    err = capsys.readouterr().err
    assert "definition REJECTED" in err and "temperature" in err
    status = [w for w in node["writes"] if w["tool"] == "aimeat_memory_write"][-1]["value"]
    assert status["ok"] is False and status["revision"] == "2026-08-28T00:00:00Z"
    assert any("temperature" in e for e in status["errors"]), "the tab shows WHY it is on the old one"


def test_a_read_that_fails_keeps_the_agent_on_its_feet(node, capsys):
    """A tunnel blip is not a definition change. Same floor, different reason — and no status write,
    because we learned nothing about what the node holds."""
    node["value"] = ConnectionError("tunnel down")
    live = ja.Definition("node-agent", DOC_V1, "2026-08-28T00:00:00Z")

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
    assert "revision" not in written, "no writer emits a revision counter — load_def falls back to publishedAt"

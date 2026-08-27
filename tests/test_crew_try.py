"""`crewaimeat try` — the bench, and the two promises it makes.

A trial is only useful if (1) a bad definition is refused BEFORE anything is built, with the same
errors the fleet would give, and (2) a trial leaves nothing behind. Both are asserted here; the LLM
and the kickoff are stubbed, so every assertion is about what the command decides.
"""

from __future__ import annotations

import json

import pytest

import crewaimeat.crew_try as ct

GOOD = {
    "agent_name": "bench-demo",
    "llm_profile": None,
    "agents": [
        {
            "name": "writer",
            "role": "Writer",
            "goal": "Answer the request",
            "backstory": "You write plainly.",
        }
    ],
    "tasks": [
        {
            "id": "write",
            "agent": "writer",
            "description": "Answer this:\n{{ctx.prompt}}",
            "expected_output": "One paragraph.",
        }
    ],
}


def _write(tmp_path, doc, name="def.json"):
    p = tmp_path / name
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return p


@pytest.fixture
def stub_run(monkeypatch):
    """A stubbed LLM + kickoff. Records whether the crew was actually built and run."""
    seen: dict = {}

    # A real BaseLLM subclass, not a bare object: crewai validates the `llm` field, so a stand-in
    # that does not mirror the real type would fail construction and the test would be measuring
    # the double instead of the command.
    from crewai import BaseLLM

    class _LLM(BaseLLM):
        def __init__(self):
            super().__init__(model="test-model")

        def call(self, messages, **kwargs):  # noqa: ARG002 - never reached; kickoff is stubbed
            return "the answer"

    class _Crew:
        def __init__(self, agents, tasks, process, verbose):
            seen["agents"], seen["tasks"] = agents, tasks

        def kickoff(self):
            seen["ran"] = True
            return "the answer"

    monkeypatch.setattr("crewaimeat.llm.get_llm", lambda **k: seen.setdefault("llm_kwargs", k) and None or _LLM())
    monkeypatch.setattr("crewaimeat.llm.resolved_model", lambda llm: "test-model")
    monkeypatch.setattr("crewai.Crew", _Crew)
    return seen


def test_a_bad_definition_is_refused_before_anything_is_built(tmp_path, capsys, stub_run):
    """The whole point of the bench: the errors arrive in seconds, not after a device-flow approval
    and a fleet restart — and they are the SAME errors the fleet would raise."""
    bad = {**GOOD, "temperature": 9, "agents": [{**GOOD["agents"][0], "tools": ["taikasauva"]}]}
    code = ct.try_crew(_write(tmp_path, bad), "hei")

    err = capsys.readouterr().err
    assert code == 1
    assert "temperature: must be a number in [0, 2]" in err
    assert "unknown tool 'taikasauva'" in err
    assert "Nothing was built" in err
    assert "ran" not in stub_run, "an invalid def must never reach a model"


def test_check_validates_without_calling_a_model(tmp_path, capsys, stub_run):
    code = ct.try_crew(_write(tmp_path, GOOD), "", check_only=True)
    assert code == 0 and "VALID" in capsys.readouterr().err
    assert "ran" not in stub_run and "llm_kwargs" not in stub_run


def test_a_run_needs_a_prompt_and_says_so(tmp_path, capsys, stub_run):
    """`{{ctx.prompt}}` with nothing in it is the classic 'agent drifts to a guessed target' bug."""
    code = ct.try_crew(_write(tmp_path, GOOD), "   ")
    assert code == 2 and "--prompt is required" in capsys.readouterr().err
    assert "ran" not in stub_run


def test_a_valid_definition_is_built_by_the_real_interpreter_and_run_once(tmp_path, capsys, stub_run):
    code = ct.try_crew(_write(tmp_path, GOOD), "miksi taivas on sininen")

    out, err = capsys.readouterr()
    assert code == 0 and stub_run["ran"] is True
    assert len(stub_run["agents"]) == 1 and len(stub_run["tasks"]) == 1
    assert "miksi taivas on sininen" in stub_run["tasks"][0].description, "ctx.prompt is injected"
    assert "the answer" in out
    assert "nothing is registered, published or written" in err


def test_the_trial_leaves_nothing_behind(tmp_path, monkeypatch, stub_run):
    """A trial whose traces you have to sweep is not a trial. Nothing may reach the node — no agent
    registration, no offer, no memory write — so the whole node dispatcher is made to explode."""

    def _boom(*a, **k):
        raise AssertionError("the bench touched the node")

    monkeypatch.setattr("crewaimeat.aimeat_crew._aimeat_call", _boom)
    monkeypatch.setattr("crewaimeat.aimeat_crew.run_crew", _boom)

    assert ct.try_crew(_write(tmp_path, GOOD), "hei") == 0


def test_as_lends_an_identity_to_the_tools_without_changing_the_crew(tmp_path, stub_run):
    """Tools reach the node under an agent's token, so an unregistered `agent_name` has none. `--as`
    borrows a registered one — the crew stays the doc's, only the credentials are somebody else's."""
    ct.try_crew(_write(tmp_path, GOOD), "hei", as_agent="web-researcher")
    assert stub_run["llm_kwargs"]["agent_name"] == "web-researcher", "routing follows the lender"
    assert stub_run["agents"][0].role == "Writer", "the crew is still the document's"


def test_a_missing_file_fails_with_the_path(tmp_path, capsys):
    code = ct.try_crew(tmp_path / "ei-ole.json", "hei")
    assert code == 2 and "FAILED to read" in capsys.readouterr().err


def test_the_summary_says_what_will_run(tmp_path, capsys, stub_run):
    """Read before you run: how many agents, how many tasks, which tools, which model profile."""
    doc = {**GOOD, "llm_profile": "content-free", "agents": [{**GOOD["agents"][0], "tools": ["web"]}]}
    ct.try_crew(_write(tmp_path, doc), "", check_only=True)
    err = capsys.readouterr().err
    assert "bench-demo: 1 agent(s), 1 task(s)" in err and "tools: web" in err and "content-free" in err

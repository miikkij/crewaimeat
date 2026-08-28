"""The Crew tab's two buttons, seen from this side.

`handle()` is the whole meaning of the contract and it is pure apart from the model call, so the
transport can be someone else's problem here. What matters: every capability ANSWERS — a button that
spins forever is worse than one that says why — and a trial leaves nothing behind.
"""

from __future__ import annotations

import pytest

import crewaimeat.crew_invoke as ci

DOC = {
    "agent_name": "node-agent",
    "agents": [{"name": "w", "role": "Writer", "goal": "Answer", "backstory": "Plainly."}],
    "tasks": [{"id": "t", "agent": "w", "description": "Do:\n{{ctx.prompt}}", "expected_output": "A line."}],
}


def test_validate_answers_with_the_validators_own_errors():
    """The node renders these verbatim and anchors them to fields, so they must arrive unedited."""
    ok, result = ci.handle("crew.validate", {"doc": {**DOC, "temperature": 9}}, agent_name="node-agent")
    assert ok is True, "the CALL succeeded — the document is what failed"
    assert any(e == "temperature: must be a number in [0, 2]" for e in result["errors"])


def test_validate_of_a_good_document_is_an_empty_list():
    ok, result = ci.handle("crew.validate", {"doc": DOC}, agent_name="node-agent")
    assert ok is True and result == {"errors": []}


def test_a_trial_refuses_an_invalid_document_with_the_validators_reasons():
    """Running it anyway would fail deep inside crewai with a worse message than the one we have."""
    ok, result = ci.handle("crew.try", {"doc": {**DOC, "temperature": 9}, "prompt": "hei"}, agent_name="node-agent")
    assert ok is False and result["code"] == "INVALID"
    assert any("temperature" in e for e in result["errors"])


def test_a_trial_without_a_prompt_says_so():
    ok, result = ci.handle("crew.try", {"doc": DOC, "prompt": "  "}, agent_name="node-agent")
    assert ok is False and result["code"] == "BAD_INPUT"


def test_a_missing_document_is_a_bad_input_not_a_crash():
    for payload in ({}, {"doc": "not an object"}, {"doc": None}):
        ok, result = ci.handle("crew.validate", payload, agent_name="node-agent")
        assert ok is False and result["code"] == "BAD_INPUT"


def test_an_unknown_capability_still_answers():
    ok, result = ci.handle("crew.explode", {"doc": DOC}, agent_name="node-agent")
    assert ok is False and result["code"] == "UNKNOWN_CAPABILITY" and "crew.validate" in result["message"]


def test_a_trial_runs_the_document_under_this_agents_identity(monkeypatch):
    """The invoke arrived on THIS agent's tunnel, so its token is the only one we may spend — and the
    document under trial may not even name a registered agent yet."""
    seen: dict = {}

    class _LLM:
        model = "test-model"

    class _Crew:
        def __init__(self, **kw):
            seen["built"] = kw

        def kickoff(self):
            return "a trial answer"

    monkeypatch.setattr("crewaimeat.llm.get_llm", lambda **k: seen.setdefault("llm", k) and None or _LLM())
    monkeypatch.setattr("crewaimeat.llm.resolved_model", lambda llm: "test-model")
    monkeypatch.setattr(
        "crewaimeat.crew_def.build_domain_from_json",
        lambda doc, ctx: seen.setdefault("doc", doc) and None or ([], []),
    )
    monkeypatch.setattr("crewai.Crew", _Crew)

    ok, result = ci.handle("crew.try", {"doc": DOC, "prompt": "miksi"}, agent_name="lender")

    assert ok is True and result["output"] == "a trial answer"
    assert isinstance(result["duration_ms"], int) and result["model"] == "test-model"
    assert seen["llm"]["agent_name"] == "lender", "routing and tools follow the invoked agent"
    assert seen["doc"]["agent_name"] == "lender", "the tools call the node as somebody who exists"


def test_a_trial_that_blows_up_answers_with_the_reason(monkeypatch):
    """A spinning button teaches nobody anything."""
    monkeypatch.setattr(
        "crewaimeat.llm.get_llm", lambda **k: (_ for _ in ()).throw(RuntimeError("no model configured"))
    )
    ok, result = ci.handle("crew.try", {"doc": DOC, "prompt": "hei"}, agent_name="node-agent")
    assert ok is False and result["code"] == "TRIAL_FAILED" and "no model configured" in result["message"]
    assert "duration_ms" in result


@pytest.mark.parametrize("capability", ci._CAPABILITIES)
def test_every_declared_capability_is_actually_handled(capability):
    """The list in the module and the branches in `handle` must not drift apart."""
    ok, result = ci.handle(capability, {"doc": DOC, "prompt": "x"}, agent_name="node-agent")
    assert result.get("code") != "UNKNOWN_CAPABILITY"


def _envelope(frame):
    """`GET /local/invoke/next` answers `{ok, data}`, like every other /local/*/next head."""
    return {"ok": True, "data": frame}


def test_the_adapter_matches_the_packages_handler_signature():
    """`CrewSpec.on_invoke` is handed straight to `run_crew_daemon`, which calls
    `handler(capability, input, invoke)` and accepts either a result or an `(ok, result)` pair.

    Pinned against the REAL call site rather than a hand-written double: the transport used to live
    in this repo and was deleted when aimeat-crewai 0.22.0 shipped a better one (a worker pool, so a
    minutes-long `crew.try` does not block the `crew.validate` behind it). If that signature ever
    changes, this fails here instead of the button spinning in somebody's browser.
    """
    import inspect

    from aimeat_crewai.daemon import run_invoke_listener

    assert list(inspect.signature(ci.on_invoke).parameters) == ["capability", "payload", "invoke"]

    ok, result = ci.on_invoke("crew.validate", {"doc": DOC}, {"agent": "node-agent", "id": "inv-1"})
    assert ok is True and result == {"errors": []}

    # The package unwraps exactly this pair shape; anything else it treats as a bare result.
    src = inspect.getsource(run_invoke_listener.__module__ and __import__("aimeat_crewai.daemon", fromlist=["x"]))
    assert "isinstance(out, tuple) and len(out) == 2 and isinstance(out[0], bool)" in src, (
        "the package no longer reads an (ok, result) pair — on_invoke must return what it now expects"
    )


def test_the_agent_name_comes_from_the_frame():
    """One daemon, many agents: a trial must spend the identity the invoke arrived for."""
    seen: dict = {}
    import crewaimeat.crew_invoke as mod

    original = mod.handle
    try:
        mod.handle = lambda cap, payload, *, agent_name: seen.setdefault("who", agent_name) and None or (True, {})
        mod.on_invoke("crew.validate", {"doc": DOC}, {"agent": "lender", "id": "x"})
    finally:
        mod.handle = original
    assert seen["who"] == "lender"

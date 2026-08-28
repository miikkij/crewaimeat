"""The Crew tab's two buttons, seen from this side.

`handle()` is the whole meaning of the contract and it is pure apart from the model call, so the
transport can be someone else's problem here. What matters: every capability ANSWERS — a button that
spins forever is worse than one that says why — and a trial leaves nothing behind.
"""

from __future__ import annotations

import threading

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


def test_a_handler_crash_is_still_posted_back(monkeypatch):
    """Our own bug must not leave the caller waiting for a timeout."""
    posted: list = []

    class _Session:
        headers: dict = {}

        def post(self, url, params=None, json=None, timeout=None):  # noqa: A002 - mirrors requests
            posted.append((url, json))

    monkeypatch.setattr(ci, "handle", lambda *a, **k: (_ for _ in ()).throw(ValueError("boom")))
    ci._answer(_Session(), "http://x", {"id": "abc123", "capability": "crew.validate", "input": {}}, "node-agent")

    assert posted and posted[0][0].endswith("/local/invoke/abc123/result")
    assert posted[0][1]["ok"] is False and posted[0][1]["result"]["code"] == "HANDLER_CRASHED"


def test_an_old_connector_is_reported_once_and_then_left_alone(monkeypatch, capsys):
    """404 every 25 s would bury the log of an agent that is otherwise working perfectly."""
    stop = threading.Event()
    calls: dict = {"n": 0}

    class _R:
        status_code = 404
        content = b""

    class _Session:
        headers: dict = {}

        def get(self, *a, **k):
            calls["n"] += 1
            if calls["n"] >= 3:
                stop.set()
            return _R()

    monkeypatch.setattr(ci, "_serve_base", lambda: "http://127.0.0.1:1")
    monkeypatch.setattr("requests.Session", lambda: _Session())
    monkeypatch.setattr(threading.Event, "wait", lambda self, t=None: None)

    ci.serve_invokes("node-agent", stop=stop)

    err = capsys.readouterr().err
    assert err.count("older connector") == 1, "said once, not once per poll"
    assert "Everything else works" in err


def test_no_serve_daemon_is_not_fatal(monkeypatch, capsys):
    """No loopback daemon means no buttons. The agent's own work is unaffected, so this returns."""
    monkeypatch.setattr(ci, "_serve_base", lambda: (_ for _ in ()).throw(RuntimeError("no discovery file")))
    ci.serve_invokes("node-agent", stop=threading.Event())
    assert "will not reach this agent" in capsys.readouterr().err


@pytest.mark.parametrize("capability", ci._CAPABILITIES)
def test_every_declared_capability_is_actually_handled(capability):
    """The list in the module and the branches in `handle` must not drift apart."""
    ok, result = ci.handle(capability, {"doc": DOC, "prompt": "x"}, agent_name="node-agent")
    assert result.get("code") != "UNKNOWN_CAPABILITY"


# The daemon's real shapes, from the shared spec (doc-mtc3ztsbxn9n, answer A). These two tests exist
# because the first version of the poller read the ENVELOPE as the frame: `id` came back empty and
# every invoke was dropped with "frame without an id" — a message that reads like the node's fault.
def _envelope(frame):
    """`GET /local/invoke/next` answers `{ok, data}`, like every other /local/*/next head."""
    return {"ok": True, "data": frame}


def test_the_poller_unwraps_the_daemons_envelope(monkeypatch):
    answered: list = []
    stop = threading.Event()

    class _R:
        status_code = 200
        content = b"{}"

        @staticmethod
        def json():
            stop.set()
            return _envelope(
                {"agent": "node-agent", "id": "inv-1", "capability": "crew.validate", "input": {"doc": DOC}}
            )

    class _Session:
        headers: dict = {}

        def get(self, *a, **k):
            return _R()

    monkeypatch.setattr(ci, "_serve_base", lambda: "http://127.0.0.1:1")
    monkeypatch.setattr("requests.Session", lambda: _Session())
    monkeypatch.setattr(ci, "_answer", lambda _s, _b, frame, _a: answered.append(frame))

    ci.serve_invokes("node-agent", stop=stop)

    assert answered and answered[0]["id"] == "inv-1", "the envelope was handed on instead of the frame"
    assert answered[0]["capability"] == "crew.validate"


def test_a_bare_frame_is_still_answered(monkeypatch):
    """If a future daemon stops wrapping, answering is cheaper than being right about the shape."""
    answered: list = []
    stop = threading.Event()

    class _R:
        status_code = 200
        content = b"{}"

        @staticmethod
        def json():
            stop.set()
            return {"id": "inv-2", "capability": "crew.try", "input": {"doc": DOC, "prompt": "x"}}

    monkeypatch.setattr(ci, "_serve_base", lambda: "http://127.0.0.1:1")
    monkeypatch.setattr("requests.Session", lambda: type("S", (), {"headers": {}, "get": lambda self, *a, **k: _R()})())
    monkeypatch.setattr(ci, "_answer", lambda _s, _b, frame, _a: answered.append(frame))

    ci.serve_invokes("node-agent", stop=stop)

    assert answered and answered[0]["id"] == "inv-2"


def test_the_result_says_which_agent_it_is_for(monkeypatch):
    """One daemon serves the whole fleet; the id alone does not say whose invoke this was, and a post
    without `agent` comes back UNKNOWN_INVOKE."""
    posted: list = []

    class _Session:
        headers: dict = {}

        def post(self, url, params=None, json=None, timeout=None):  # noqa: A002 - mirrors requests
            posted.append((url, params, json))

    monkeypatch.setattr(ci, "handle", lambda *a, **k: (True, {"errors": []}))
    ci._answer(_Session(), "http://x", {"id": "inv-3", "capability": "crew.validate", "input": {}}, "node-agent")

    assert posted and posted[0][0].endswith("/local/invoke/inv-3/result")
    assert posted[0][1] == {"agent": "node-agent"}

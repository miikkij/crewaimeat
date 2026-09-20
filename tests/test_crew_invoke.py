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


# ── the menu's decision-rule rows ─────────────────────────────────────────────────────────────
#
# These cannot live in TOOL_REGISTRY: a rule is the OWNER's, named by them and changed whenever
# they like, so any list compiled in this repo would be a copy of somebody else's vocabulary --
# the drift `crew.menu` exists to end. The runtime holds the agent's credential, so it is the one
# thing that can truthfully answer which rules THIS agent may run.


def _rules_stub(*rules):
    def _fake(agent_name=None, **kw):
        return list(rules)

    return _fake


def _menu(monkeypatch, rules_fn):
    # THE MODULE, not the attribute. `aimeat_crewai` exports a FUNCTION called `decide`, which
    # shadows the submodule of the same name on the package object -- so `import
    # aimeat_crewai.decide as m` hands back the function and `m.rules` is an AttributeError.
    # importlib goes through sys.modules and gets the real module. The code under test uses
    # `from aimeat_crewai.decide import rules`, which resolves the same way, and reads the
    # attribute per call, so patching here reaches it.
    import importlib

    decide_mod = importlib.import_module("aimeat_crewai.decide")
    monkeypatch.setattr(decide_mod, "rules", rules_fn)
    ok, result = ci.handle("crew.menu", {}, agent_name="node-agent")
    assert ok is True
    return result


def test_the_menu_offers_decide_itself():
    ok, result = ci.handle("crew.menu", {}, agent_name="node-agent")
    ids = {t["id"] for t in result["tools"]}
    assert "decide" in ids, "the bare selector is a registry tool and must always be offered"
    purpose = next(t["purpose"] for t in result["tools"] if t["id"] == "decide")
    assert purpose, "a row with no purpose line is a row a person cannot choose from"


def test_the_menu_lists_one_row_per_rule_the_owner_allows_this_agent(monkeypatch):
    result = _menu(
        monkeypatch,
        _rules_stub(
            {"id": "sort-a-message", "title": "Sort an incoming message", "decides": "which queue it goes to"},
            {"id": "send-a-reply", "title": "Send a reply unread", "decides": "whether it is sent unread"},
        ),
    )
    ids = [t["id"] for t in result["tools"]]
    assert "decide:sort-a-message" in ids and "decide:send-a-reply" in ids


def test_a_rule_row_carries_the_owners_own_words(monkeypatch):
    # The Crew tab shows this to a person choosing a tool, and nothing here can write it better
    # than the owner already did.
    result = _menu(
        monkeypatch,
        _rules_stub(
            {"id": "sort-a-message", "title": "Sort an incoming message", "decides": "which queue it goes to"},
        ),
    )
    row = next(t for t in result["tools"] if t["id"] == "decide:sort-a-message")
    assert "Sort an incoming message" in row["purpose"]
    assert "which queue it goes to" in row["purpose"]


def test_a_rule_without_a_decides_line_still_gets_a_readable_purpose(monkeypatch):
    result = _menu(monkeypatch, _rules_stub({"id": "r", "title": "Just a title"}))
    assert next(t for t in result["tools"] if t["id"] == "decide:r")["purpose"] == "Just a title"


def test_a_rule_with_no_id_is_skipped_rather_than_offered_as_a_broken_row(monkeypatch):
    result = _menu(monkeypatch, _rules_stub({"title": "nameless"}, {"id": "ok", "title": "Fine"}))
    assert [t["id"] for t in result["tools"] if t["id"].startswith("decide:")] == ["decide:ok"]


def test_an_unreachable_node_costs_the_decision_rows_and_nothing_else(monkeypatch, capsys):
    # THE POINT OF DEGRADING HERE. The menu's job is to let a person pick a tool. Failing the whole
    # invoke would empty the picker of memory, web and everything else over a feature the owner may
    # not even use -- and the node would then serve its own stale copy of the list, which is what
    # asking was meant to replace.
    def _boom(agent_name=None, **kw):
        raise RuntimeError("could not reach the node")

    result = _menu(monkeypatch, _boom)
    ids = {t["id"] for t in result["tools"]}
    assert "memory" in ids and "web" in ids and "decide" in ids
    assert not any(i.startswith("decide:") for i in ids)
    assert "decision rules not listed" in capsys.readouterr().out, "a missing group is never silent"


def test_the_llm_half_of_the_menu_is_unaffected_by_the_rules(monkeypatch):
    result = _menu(monkeypatch, _rules_stub({"id": "r", "title": "T"}))
    assert "profiles" in result["llm"] and "models" in result["llm"]
    assert result["spec"] == "aimeat.crew-menu/1"


@pytest.mark.parametrize("capability", ci._CAPABILITIES)
def test_every_declared_capability_is_actually_handled(capability, monkeypatch):
    """The list in the module and the branches in `handle` must not drift apart."""
    monkeypatch.setattr(ci, "_run_trial", lambda *a, **kw: (True, {"output": "stub trial"}))
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


def test_the_menu_file_carries_the_key_names_never_a_key(tmp_path, monkeypatch):
    """CodeQL #32 reads `api_key_env` as a password flowing into the answer file. It is the NAME of an
    environment variable, and the node's picker needs it: the model override it stores routes by that
    name. This runs the real sink — `answer_invoke` writing `<job>.out.json` — with the variable set to
    a recognisable value, so a change that ever copies the key itself into the menu fails here."""
    import json

    from crewaimeat import run_once

    secret = "sk-or-v1-THIS-VALUE-MUST-NEVER-LEAVE-THE-PROCESS"
    monkeypatch.setenv("CODEQL_PROBE_KEY", secret)
    providers = tmp_path / "llm_providers.json"
    providers.write_text(
        json.dumps(
            {
                "profiles": {
                    "default": {
                        "providers": [
                            {"type": "openrouter", "api_key_env": "CODEQL_PROBE_KEY", "models": ["openai/gpt-oss-120b"]}
                        ]
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LLM_PROVIDERS_FILE", str(providers))
    job = tmp_path / "invoke.json"
    job.write_text(json.dumps({"agent": "node-agent", "capability": "crew.menu", "input": {}}), encoding="utf-8")

    assert run_once.answer_invoke(job) == 0

    written = job.with_suffix(".out.json").read_text(encoding="utf-8")
    assert secret not in written, "the key's VALUE reached the file the spawner posts to the node"
    models = json.loads(written)["result"]["llm"]["models"]
    assert models and models[0]["api_key_env"] == "CODEQL_PROBE_KEY", "the override still needs the NAME"

"""The owner's model choice, made on the node, decides which chain a crew runs on.

Every one of these is about PRECEDENCE, because that is the whole of the feature and it is the part
that is silent when it is wrong: a crew routed to the wrong model does not fail, it answers, and the
answer looks fine. So each case pins one rung of the ladder against the rung above it.

The node is stubbed at `llm_choice.node_choice`, which is the one seam between "what the owner said"
and "which endpoints we build" — the transport, the caching and the owner-scope read are exercised
where they live, not here.
"""

from __future__ import annotations

import pytest

from crewaimeat import llm as llmmod
from crewaimeat import llm_choice

CFG = {
    "profiles": {
        "content": {"providers": [{"type": "openrouter", "name": "content-p", "models": [{"id": "m/content"}]}]},
        "coding": {"providers": [{"type": "openrouter", "name": "coding-p", "models": [{"id": "m/coding"}]}]},
        "news": {"providers": [{"type": "openrouter", "name": "news-p", "models": [{"id": "m/news"}]}]},
    },
    "default": "content",
    "crews": {"mapped-agent": "news"},
}


@pytest.fixture(autouse=True)
def _no_local_override(monkeypatch):
    """No `llm_overrides.json` in the picture unless a test puts one there."""
    monkeypatch.setattr(llmmod, "agent_override", lambda name: None)
    monkeypatch.setattr(llmmod, "_declared_profile", lambda name: None)
    llmmod._DOC_PROFILES.clear()
    yield
    llmmod._DOC_PROFILES.clear()


def _node(monkeypatch, choice, scope):
    monkeypatch.setattr(llm_choice, "node_choice", lambda name: (choice, scope))


def test_no_choice_anywhere_falls_to_the_files_default(monkeypatch):
    _node(monkeypatch, None, None)
    providers, label = llmmod._select_chain(CFG, "plain-agent")
    assert label == "content"
    assert providers[0]["name"] == "content-p"


def test_the_owners_choice_for_this_agent_beats_the_machines_map(monkeypatch):
    """`crews` is the operator's map for this box; a choice the owner made about THIS agent is more
    specific, and the person who made it is looking at the agent, not at the box."""
    _node(monkeypatch, {"kind": "profile", "profile": "coding"}, "agent")
    providers, label = llmmod._select_chain(CFG, "mapped-agent")
    assert label == "node:coding"
    assert providers[0]["name"] == "coding-p"


def test_a_pinned_model_from_the_node_is_used_whole(monkeypatch):
    """The provider block travels as the runtime built it, so nothing here has to know what a
    provider is."""
    pinned = {
        "type": "openrouter",
        "name": "pinned",
        "models": [{"id": "m/pinned"}],
        "api_key_env": "OPENROUTER_API_KEY",
    }
    _node(monkeypatch, {"kind": "model", "label": "openrouter:m/pinned", "provider": pinned}, "agent")
    providers, label = llmmod._select_chain(CFG, "any-agent")
    assert providers == [pinned]
    assert label == "node:openrouter:m/pinned"


def test_a_local_pin_still_wins_over_the_node(monkeypatch):
    """The person sitting at the machine keeps the last word: `llm_overrides.json` is how a fleet is
    debugged, and a remote setting that silently outranked it would make that impossible."""
    monkeypatch.setattr(llmmod, "agent_override", lambda name: {"kind": "profile", "profile": "news"})
    _node(monkeypatch, {"kind": "profile", "profile": "coding"}, "agent")
    _providers, label = llmmod._select_chain(CFG, "any-agent")
    assert label == "override-profile:news"


def test_the_definitions_own_llm_profile_is_honoured(monkeypatch):
    """It was carried and validated from the start and routed NOTHING: the declaration lookup reads
    `crews/<name>_crew.py` with ast, and a node-backed agent's loader is five lines that name the
    agent. `run_json_agent` now states it here at start."""
    _node(monkeypatch, None, None)
    llmmod.set_doc_profile("json-agent", "coding")
    providers, label = llmmod._select_chain(CFG, "json-agent")
    assert label == "coding"
    assert providers[0]["name"] == "coding-p"


def test_the_crews_own_declaration_beats_the_owners_default(monkeypatch):
    """A crew that states its need is stating it about itself; the owner's default is about everything
    they have not thought about yet."""
    _node(monkeypatch, {"kind": "profile", "profile": "news"}, "default")
    llmmod.set_doc_profile("json-agent", "coding")
    _providers, label = llmmod._select_chain(CFG, "json-agent")
    assert label == "coding"


def test_the_owners_default_beats_the_files_default(monkeypatch):
    _node(monkeypatch, {"kind": "profile", "profile": "news"}, "default")
    providers, label = llmmod._select_chain(CFG, "unopinionated-agent")
    assert label == "news"
    assert providers[0]["name"] == "news-p"


def test_a_node_profile_that_does_not_exist_here_does_not_strand_the_agent(monkeypatch):
    """The owner may name a profile this machine has never had. Falling through to the file's default
    is the safe direction: the agent runs on something rather than on nothing."""
    _node(monkeypatch, {"kind": "profile", "profile": "no-such-profile"}, "agent")
    providers, label = llmmod._select_chain(CFG, "any-agent")
    assert label == "content"
    assert providers[0]["name"] == "content-p"


def test_a_malformed_choice_is_ignored_rather_than_obeyed(monkeypatch):
    """The record is written from a browser and read by a process that would use whatever it finds."""
    for junk in ({"kind": "profile"}, {"kind": "model"}, {"kind": "nonsense"}, "a string", 7, None):
        assert llm_choice._valid(junk) is None


def test_a_read_the_node_cannot_answer_is_not_fatal(monkeypatch):
    """Routing must never depend on a network being up."""

    def boom(agent_name, key):
        raise RuntimeError("the node is down")

    monkeypatch.setattr("crewaimeat.memory_tools.read_owner_key", boom)
    llm_choice.forget()
    assert llm_choice.node_choice("any-agent") == (None, None)

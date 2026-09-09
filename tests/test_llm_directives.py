"""The owner's directives ride on every model call, not only on a crew task's text.

What these pin: the wrapper get_llm() installs prepends the block as a system message on a plain
string prompt and on a message list; a list that already carries the block (a crew task the scaffold
prepended it to) is left as it came; the block is fetched once per TTL and a failed fetch keeps the
last known block; and a model built with no agent name gets nothing installed. The node is stubbed at
`directives_block`'s `fetch` seam, so no daemon and no token are needed.

Before 2026-09-09 every one of the "prepends" cases failed: the 27 direct llm.call sites sent their
prompts bare, and a rule written on an agent's Directives tab never reached an image request.
"""

from __future__ import annotations

import pytest

from crewaimeat import directives as d
from crewaimeat import llm as llmmod

RULES = {
    "purpose": "Write for the julkaisupöytä.",
    "rules": [
        {"source": "owner", "description": "Never depict identifiable real people or places in image prompts."},
        {"source": "agent", "description": "Prefer Finnish sources."},
    ],
}


class Recorder:
    """A stand-in LLM: records what `call` received, answers a constant."""

    def __init__(self):
        self.seen = []

    def call(self, messages, *args, **kwargs):
        self.seen.append(messages)
        return "ok"


@pytest.fixture(autouse=True)
def _fresh_cache():
    d.clear_cache()
    yield
    d.clear_cache()


def test_block_names_purpose_and_rules_with_their_source():
    block = d.format_directives(RULES)
    assert block.startswith(d.DIRECTIVES_HEADER)
    assert "- Purpose: Write for the julkaisupöytä." in block
    assert "- [policy] Never depict identifiable real people" in block
    assert "- [standing] Prefer Finnish sources." in block
    assert d.format_directives(None) == ""
    assert d.format_directives({"purpose": "", "rules": []}) == ""


def test_a_string_prompt_becomes_system_plus_user():
    rec = Recorder()
    d.install_directives(rec, "julkaisu-linkedin", block_for=lambda a, o: d.format_directives(RULES))
    rec.call("Write the post.")
    sent = rec.seen[0]
    assert [m["role"] for m in sent] == ["system", "user"]
    assert sent[0]["content"].startswith(d.DIRECTIVES_HEADER)
    assert sent[1]["content"] == "Write the post."


def test_a_message_list_gets_the_block_in_front_of_its_system_turn():
    rec = Recorder()
    d.install_directives(rec, "julkaisu-linkedin", block_for=lambda a, o: d.format_directives(RULES))
    original = [{"role": "system", "content": "You are a writer."}, {"role": "user", "content": "Go."}]
    rec.call(original)
    sent = rec.seen[0]
    assert len(sent) == 2
    assert sent[0]["role"] == "system"
    assert sent[0]["content"].startswith(d.DIRECTIVES_HEADER)
    assert sent[0]["content"].endswith("You are a writer.")
    # The caller's own list is untouched: a pipeline that reuses its prompt list must not accumulate blocks.
    assert original[0]["content"] == "You are a writer."


def test_a_user_only_list_gets_a_new_system_turn():
    rec = Recorder()
    d.install_directives(rec, "julkaisu-x", block_for=lambda a, o: d.format_directives(RULES))
    rec.call([{"role": "user", "content": "Write the thread."}])
    sent = rec.seen[0]
    assert [m["role"] for m in sent] == ["system", "user"]


def test_a_list_that_already_carries_the_block_is_left_alone():
    rec = Recorder()
    d.install_directives(rec, "joker", block_for=lambda a, o: d.format_directives(RULES))
    task_text = d.format_directives(RULES) + "\n\nTASK: tell a joke"
    rec.call([{"role": "system", "content": "You are Joker."}, {"role": "user", "content": task_text}])
    sent = rec.seen[0]
    assert sent[0]["content"] == "You are Joker."
    assert sent[1]["content"] == task_text


def test_no_block_means_the_call_passes_through_unchanged():
    rec = Recorder()
    d.install_directives(rec, "quiet-agent", block_for=lambda a, o: "")
    rec.call("Hello")
    assert rec.seen[0] == "Hello"


def test_fetched_once_per_ttl_and_a_failed_fetch_keeps_the_last_block():
    calls = []
    answers = [RULES, None, {"purpose": "", "rules": []}]

    def fetch(agent, owner):
        calls.append(agent)
        return answers.pop(0)

    clock = [100.0]
    now = lambda: clock[0]  # noqa: E731
    b1 = d.directives_block("desk", None, ttl_s=300, fetch=fetch, now=now)
    b2 = d.directives_block("desk", None, ttl_s=300, fetch=fetch, now=now)
    assert b1 == b2 and b1.startswith(d.DIRECTIVES_HEADER)
    assert calls == ["desk"], "within the TTL the node is not asked again"
    clock[0] += 301
    b3 = d.directives_block("desk", None, ttl_s=300, fetch=fetch, now=now)
    assert b3 == b1, "a failed fetch keeps the last known block rather than dropping the rules"
    clock[0] += 301
    b4 = d.directives_block("desk", None, ttl_s=300, fetch=fetch, now=now)
    assert b4 == "", "an answer of 'no directives' is real and clears the block"
    assert calls == ["desk", "desk", "desk"]


def test_install_is_idempotent_and_inert_without_an_agent():
    rec = Recorder()
    d.install_directives(rec, None)
    rec.call("bare")
    assert rec.seen[0] == "bare"
    d.install_directives(rec, "a", block_for=lambda a, o: "BLOCK " + d.DIRECTIVES_HEADER)
    d.install_directives(rec, "a", block_for=lambda a, o: "SECOND")
    rec.call("x")
    assert rec.seen[1][0]["content"].startswith("BLOCK"), "the second install did not stack a wrapper"


def test_a_fetch_that_raises_does_not_break_the_call():
    rec = Recorder()

    def boom(agent, owner):
        raise RuntimeError("node down")

    d.install_directives(rec, "a", block_for=boom)
    assert rec.call("still works") == "ok"
    assert rec.seen[0] == "still works"


def test_get_llm_installs_the_wrapper_on_whatever_it_builds(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(llmmod, "_build_llm", lambda for_tool_use, temperature, agent_name: rec)
    monkeypatch.setattr(d, "fetch_directives", lambda agent, owner: RULES)
    llm = llmmod.get_llm(agent_name="julkaisu-kuva", owner="happydude500001")
    assert llm is rec
    llm.call("Draw the shot list.")
    sent = rec.seen[0]
    assert sent[0]["role"] == "system" and "identifiable real people" in sent[0]["content"]


def test_get_llm_without_an_agent_stays_bare(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(llmmod, "_build_llm", lambda for_tool_use, temperature, agent_name: rec)
    llm = llmmod.get_llm()
    llm.call("plain")
    assert rec.seen[0] == "plain"

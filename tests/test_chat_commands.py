"""chat.commands — the public command palette: validator (scaffold) + concierge's dynamic builder."""

from __future__ import annotations

import crews.concierge_crew as cc
from crewaimeat.aimeat_crew import _valid_chat_commands


def test_validator_keeps_wellformed_and_drops_junk():
    out = _valid_chat_commands(
        [
            {"id": "ok", "label": "OK", "template": "do {{x}}", "params": [{"name": "x", "required": True}]},
            {"no": "id"},  # no id -> dropped
            {"id": "BAD CAPS"},  # bad charset -> dropped
            "not a dict",  # dropped
            {"id": "sel", "params": [{"name": "p", "type": "select", "options": ["a", "b"]}]},
        ]
    )
    assert [c["id"] for c in out] == ["ok", "sel"]


def test_validator_coerces_param_types_and_defaults():
    out = _valid_chat_commands(
        [{"id": "c", "params": [{"name": "n", "type": "weird"}, {"name": "m", "type": "number", "default": 5}]}]
    )
    params = out[0]["params"]
    assert params[0]["type"] == "text"  # unknown type -> text
    assert params[1]["type"] == "number" and params[1]["default"] == "5"  # default coerced to str


def test_validator_caps_count():
    many = [{"id": f"c{i}"} for i in range(50)]
    assert len(_valid_chat_commands(many)) == 24


def test_validator_select_options_only_for_select():
    out = _valid_chat_commands([{"id": "c", "params": [{"name": "p", "type": "text", "options": ["a"]}]}])
    assert "options" not in out[0]["params"][0]  # options ignored unless type==select


def test_concierge_dynamic_commands(monkeypatch):
    # Mock the live roster so the builder is deterministic (two of the directory are "up").
    monkeypatch.setattr(
        cc.orchestrator,
        "live_services",
        lambda agent, directory: [
            {"name": "jingle-writer", "gaii": "jingle-writer#o@n", "desc": "jingles"},
            {"name": "web-researcher", "gaii": "web-researcher#o@n", "desc": "web research"},
        ],
    )
    cmds = cc._chat_commands("concierge")
    ids = [c["id"] for c in cmds]
    # the static base is always present
    assert {"search_web", "find_images", "find_document", "generate_image", "analyze_file"} <= set(ids)
    # one "Ask <specialist>" per LIVE specialist, with a {{request}} param
    assert "ask_jingle_writer" in ids and "ask_web_researcher" in ids
    ask = next(c for c in cmds if c["id"] == "ask_jingle_writer")
    assert ask["template"] == "Ask jingle-writer to {{request}}."
    assert ask["params"][0]["name"] == "request"
    # all commands survive the scaffold validator unchanged in count
    assert len(_valid_chat_commands(cmds)) == len(cmds)


def test_learned_commands_merge_and_save(monkeypatch):
    """A self-authored command is read from memory, merged into the palette, and republished on save."""
    mem: dict = {}
    monkeypatch.setattr(cc.orchestrator, "live_services", lambda agent, directory: [])

    def fake_call(agent, tool, payload):
        if tool == "aimeat_memory_read":
            return {"value": mem.get(payload["key"])}
        if tool == "aimeat_memory_write":
            mem[payload["key"]] = payload["value"]
        return {"ok": True}

    monkeypatch.setattr(cc, "_aimeat_call", fake_call)

    # No learned commands yet -> palette is just the base.
    assert all(not c["id"].startswith("learned_") for c in cc._chat_commands("concierge"))

    # Save one -> persisted to owner memory AND republished to the public key.
    cmd = {
        "id": "learned_funding",
        "label": "Find funding form",
        "template": "Find a {{programme}} PDF.",
        "params": [{"name": "programme", "type": "text", "required": True}],
    }
    assert cc._save_learned_command("concierge", cmd) is True
    assert mem["chat.commands.learned"]["commands"][0]["id"] == "learned_funding"
    published = mem["chat.commands"]["commands"]
    assert any(c["id"] == "learned_funding" for c in published)

    # Now the dynamic builder includes it, and a re-save dedups by id (no duplicate).
    assert any(c["id"] == "learned_funding" for c in cc._chat_commands("concierge"))
    cc._save_learned_command("concierge", {**cmd, "label": "Renamed"})
    learned = mem["chat.commands.learned"]["commands"]
    assert len([c for c in learned if c["id"] == "learned_funding"]) == 1

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

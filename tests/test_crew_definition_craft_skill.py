"""The author's guide `skills/crew-definition-craft` stays true to the runtime it describes.

A guide whose example the runtime refuses teaches the fault it was written to prevent. So the
example definition in it is validated by the runtime's own validator, and every tool id the guide
names is one the runtime resolves.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1] / "skills" / "crew-definition-craft" / "SKILL.md"


def _text() -> str:
    return SKILL.read_text(encoding="utf-8")


def test_the_skill_loads_under_the_shared_contract():
    from crewaimeat.skills import load_skills

    (skill,) = load_skills(["crew-definition-craft"])
    assert skill.name == "crew-definition-craft"


def test_the_example_definition_is_one_the_runtime_accepts():
    from crewaimeat.crew_def import validate_crew_doc

    doc = json.loads(re.search(r"```json\n(.*?)\n```", _text(), re.S).group(1))
    assert validate_crew_doc(doc) == []
    collector = doc["agents"][0]
    assert "news" in collector["tools"] and "web" not in collector["tools"], "the guide's own rule 1"
    assert "{{ctx.prompt}}" not in doc["tasks"][0]["description"], "the guide's own rule 2"
    assert "{{ctx.prompt}}" in doc["tasks"][1]["description"]


def test_every_tool_id_in_the_table_is_one_the_runtime_resolves():
    from crewaimeat.crew_def import resolve_tool

    section = _text().split("## 1. Choose the tool by the question")[1].split("**Give an agent only")[0]
    rows = [ln.split("|") for ln in section.splitlines() if ln.startswith("| ") and "---" not in ln][1:]
    ids = {i for row in rows for i in re.findall(r"`([a-z_]+(?::<server>)?)`", row[2])}  # the "Tool ids" column
    assert {"news", "web", "article_fetch", "workspace", "workspace_write", "memory", "decline"} <= ids
    for tool_id in ids:
        assert resolve_tool(tool_id.replace("<server>", "orders")) is not None, tool_id

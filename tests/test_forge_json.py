"""crew-forge's DECLARATIVE (JSON) build path — crewaimeat.forge_json + the write_and_validate_crew_json
tool.

Offline and hermetic: every write goes to a tmp project root (forge._project_root monkeypatched), so no
real crews/ or crew_defs/ is touched. The end-to-end test runs the SAME subprocess validator the fleet
uses (crewaimeat._validate_crew) against the emitted thin loader — proving a JSON-defined crew imports,
interprets its doc, and returns real Agent/Task objects with llm=None, exactly like a hand-written crew.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from crewaimeat import forge, forge_json


def _good_doc() -> dict:
    return {
        "agent_name": "release-notes-writer",
        "temperature": 0.5,
        "tags": ["release-notes", "role.task-runner"],
        "agents": [
            {"name": "drafter", "role": "Drafter", "goal": "Draft notes", "backstory": "You draft."},
            {"name": "editor", "role": "Editor", "goal": "Polish", "backstory": "You polish."},
        ],
        "tasks": [
            {
                "id": "draft",
                "agent": "drafter",
                "description": "Draft release notes for: {{ctx.prompt}}",
                "expected_output": "A draft.",
            },
            {
                "id": "polish",
                "agent": "editor",
                "description": "Polish the draft.",
                "expected_output": "Final notes.",
                "context": ["draft"],
            },
        ],
    }


@pytest.fixture
def tmp_root(tmp_path, monkeypatch):
    """Point forge's project root at a scratch dir so writes never touch the real repo."""
    monkeypatch.setattr(forge, "_project_root", lambda: tmp_path)
    return tmp_path


# ── write_json_crew: the exec-free writer ─────────────────────────────────────
def test_write_json_crew_writes_doc_and_loader(tmp_root):
    ok, detail, loader_path = forge_json.write_json_crew(_good_doc(), request="notes from a changelog")
    assert ok, detail
    doc_file = tmp_root / "crew_defs" / "release_notes_writer.json"
    loader_file = tmp_root / "crews" / "release_notes_writer_crew.py"
    assert doc_file.is_file() and loader_file.is_file()
    assert loader_path == loader_file
    src = loader_file.read_text(encoding="utf-8")
    # the loader is an ordinary crews/*_crew.py — discoverable + validatable by the fleet
    assert 'AGENT_NAME = "release-notes-writer"' in src
    assert "def build_domain(ctx):" in src and "def run()" in src
    assert "release_notes_writer.json" in src  # points at its own doc


def test_write_json_crew_invalid_writes_nothing(tmp_root):
    doc = _good_doc()
    doc["agents"][0]["tools"] = ["bogus-tool"]  # unknown tool -> INVALID
    ok, detail, loader_path = forge_json.write_json_crew(doc)
    assert not ok and loader_path is None
    assert "unknown tool" in detail and "bogus-tool" in detail
    assert not (tmp_root / "crew_defs").exists()  # nothing written for a bad def
    assert not (tmp_root / "crews").exists()


def test_write_json_crew_reports_non_dag(tmp_root):
    doc = _good_doc()
    doc["tasks"][0]["context"] = ["polish"]  # forward edge -> non-DAG
    ok, detail, _ = forge_json.write_json_crew(doc)
    assert not ok and "EARLIER task" in detail


def test_write_json_crew_requires_prompt_injection(tmp_root):
    doc = _good_doc()
    doc["tasks"][0]["description"] = "Draft generic notes."  # no {{ctx.prompt}} anywhere
    ok, detail, _ = forge_json.write_json_crew(doc)
    assert not ok and "ctx.prompt" in detail


# ── the emitted loader validates through the REAL fleet subprocess validator ──
def test_emitted_loader_passes_subprocess_validator(tmp_root):
    ok, _detail, _ = forge_json.write_json_crew(_good_doc())
    assert ok
    # crewaimeat._validate_crew imports the loader and calls build_domain(ctx) with llm=None, then checks
    # it returns non-empty (Agent, Task) lists — the SAME gate register_and_launch_crew re-runs.
    proc = subprocess.run(
        [sys.executable, "-m", "crewaimeat._validate_crew", "crews/release_notes_writer_crew.py"],
        capture_output=True,
        text=True,
        cwd=str(tmp_root),
        timeout=600,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0, out
    assert "VALID" in out and "2 agents, 2 tasks" in out


def test_agent_name_readable_from_emitted_loader(tmp_root):
    forge_json.write_json_crew(_good_doc())
    loader = tmp_root / "crews" / "release_notes_writer_crew.py"
    # forge._agent_name_of reads AGENT_NAME without importing — how reconcile/launch identify the crew
    assert forge._agent_name_of(loader) == "release-notes-writer"


# ── the write_and_validate_crew_json TOOL (parse tolerance + delegation) ───────
def _run_tool(crew_json: str, request: str = "") -> str:
    """Invoke the crewai @tool by its underlying function, tolerating crewai versions."""
    t = forge.write_and_validate_crew_json
    fn = getattr(t, "func", None) or getattr(t, "_run", None)
    if fn is not None:
        return fn(crew_json=crew_json, request=request)
    return t.run({"crew_json": crew_json, "request": request})


def test_tool_accepts_fenced_json(tmp_root):
    import json

    fenced = "```json\n" + json.dumps(_good_doc()) + "\n```"
    out = _run_tool(fenced, request="notes")
    assert "VALID" in out and "register_and_launch_crew" in out
    assert (tmp_root / "crew_defs" / "release_notes_writer.json").is_file()


def test_tool_rejects_unparseable_json(tmp_root):
    out = _run_tool("this is not json at all")
    assert "INVALID" in out and "JSON object" in out
    assert not (tmp_root / "crew_defs").exists()


def test_tool_returns_validator_errors(tmp_root):
    import json

    doc = _good_doc()
    doc["tasks"][1]["agent"] = "ghost"  # task points at a missing agent
    out = _run_tool(json.dumps(doc))
    assert "INVALID" in out and "does not match any defined agent" in out

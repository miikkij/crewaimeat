"""AIMEAT crew registry — publish + install crew defs (crewaimeat.crew_registry).

Fully offline: crew_registry._aimeat_call is replaced with an in-memory fake (never touches AIMEAT), and
install writes to a tmp project root (forge._project_root monkeypatched). The registry round-trips a
crew def through a memory envelope and RE-VALIDATES on fetch; install materializes the same thin loader
the fleet runs and (optionally) registers it. A subprocess check proves an installed crew validates
through the real fleet validator.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from crewaimeat import crew_registry as reg
from crewaimeat import forge, forge_json
from crewaimeat.crew_def import CrewDocError


def _good_doc() -> dict:
    return {
        "agent_name": "release-notes-writer",
        "temperature": 0.5,
        "tags": ["release-notes", "role.task-runner"],
        "agents": [{"name": "w", "role": "Writer", "goal": "Write", "backstory": "You write."}],
        "tasks": [{"id": "t", "agent": "w", "description": "Notes for: {{ctx.prompt}}", "expected_output": "Notes."}],
    }


class _FakeCall:
    """Records _aimeat_call(agent, tool, payload) invocations and returns a per-tool canned value
    (a value, or a callable(payload) -> value)."""

    def __init__(self, returns: dict | None = None):
        self.returns = returns or {}
        self.calls: list[tuple] = []

    def __call__(self, agent, tool, payload, **kw):
        self.calls.append((agent, tool, payload))
        r = self.returns.get(tool)
        return r(payload) if callable(r) else r

    def wrote(self, tool: str) -> list[dict]:
        return [p for (_a, t, p) in self.calls if t == tool]


@pytest.fixture
def tmp_root(tmp_path, monkeypatch):
    monkeypatch.setattr(forge, "_project_root", lambda: tmp_path)
    return tmp_path


# ── publish ───────────────────────────────────────────────────────────────────
def test_publish_validates_and_writes(monkeypatch):
    fake = _FakeCall({"aimeat_memory_write": {"ok": True}})
    monkeypatch.setattr(reg, "_aimeat_call", fake)
    ok, key, detail = reg.publish_crew_def(_good_doc(), agent="crew-forge")
    assert ok and key == "crews.registry.release-notes-writer", detail
    writes = fake.wrote("aimeat_memory_write")
    assert len(writes) == 1
    payload = writes[0]
    assert payload["key"] == key and payload["visibility"] == "owner"
    env = payload["value"]
    assert env["agent_name"] == "release-notes-writer" and env["doc"] == _good_doc() and env["publishedAt"]


def test_publish_refuses_invalid_def(monkeypatch):
    fake = _FakeCall({"aimeat_memory_write": {"ok": True}})
    monkeypatch.setattr(reg, "_aimeat_call", fake)
    doc = _good_doc()
    doc["tasks"] = []  # invalid — no tasks
    ok, key, detail = reg.publish_crew_def(doc, agent="crew-forge")
    assert not ok and "not published" in detail
    assert fake.wrote("aimeat_memory_write") == []  # a broken def is NEVER written


def test_publish_rejects_bad_visibility(monkeypatch):
    fake = _FakeCall({"aimeat_memory_write": {"ok": True}})
    monkeypatch.setattr(reg, "_aimeat_call", fake)
    ok, _key, detail = reg.publish_crew_def(_good_doc(), agent="crew-forge", visibility="secret")
    assert not ok and "visibility" in detail
    assert fake.wrote("aimeat_memory_write") == []


def test_publish_public_visibility(monkeypatch):
    fake = _FakeCall({"aimeat_memory_write": {"ok": True}})
    monkeypatch.setattr(reg, "_aimeat_call", fake)
    ok, _key, _detail = reg.publish_crew_def(_good_doc(), agent="crew-forge", visibility="public")
    assert ok and fake.wrote("aimeat_memory_write")[0]["visibility"] == "public"


# ── fetch (re-validates; never trusts stored bytes) ───────────────────────────
def _envelope(doc: dict) -> dict:
    return {"version": 1, "publishedAt": "2026-07-05T10:00:00", "agent_name": doc["agent_name"], "doc": doc}


def test_fetch_round_trips_the_doc(monkeypatch):
    env = _envelope(_good_doc())
    fake = _FakeCall({"aimeat_memory_read": {"value": env}})
    monkeypatch.setattr(reg, "_aimeat_call", fake)
    got = reg.fetch_crew_def("release-notes-writer", agent="crew-forge")
    assert got == _good_doc()


def test_fetch_missing_raises(monkeypatch):
    monkeypatch.setattr(reg, "_aimeat_call", _FakeCall({}))  # every read returns None
    with pytest.raises(CrewDocError) as ei:
        reg.fetch_crew_def("ghost", agent="crew-forge")
    assert "no crew def in the registry" in str(ei.value)


def test_fetch_invalid_stored_is_rejected(monkeypatch):
    bad = _good_doc()
    bad["tasks"][0]["agent"] = "nobody"  # a stored def that no longer validates
    fake = _FakeCall({"aimeat_memory_read": {"value": _envelope(bad)}})
    monkeypatch.setattr(reg, "_aimeat_call", fake)
    with pytest.raises(CrewDocError) as ei:
        reg.fetch_crew_def("release-notes-writer", agent="crew-forge")
    assert "re-validation" in str(ei.value)


def test_fetch_public_by_gaii(monkeypatch):
    env = _envelope(_good_doc())
    # own read + owner-scope list both empty; only the public-by-gaii read has it.
    fake = _FakeCall(
        {"aimeat_memory_read": None, "aimeat_memory_list": {"items": []}, "aimeat_memory_read_public": {"value": env}}
    )
    monkeypatch.setattr(reg, "_aimeat_call", fake)
    got = reg.fetch_crew_def("release-notes-writer", agent="crew-forge", gaii="writer#owner@node")
    assert got == _good_doc()
    pub = [p for (_a, t, p) in fake.calls if t == "aimeat_memory_read_public"]
    assert pub and pub[0]["gaii"] == "writer#owner@node"


def test_list_crew_defs(monkeypatch):
    items = {
        "items": [
            {"key": "crews.registry.a", "value": {"publishedAt": "2026-07-05T10:00:00"}, "owner_gaii": "a#o@n"},
            {"key": "crews.registry.b", "value": {"publishedAt": "2026-07-04T09:00:00"}},
            {"key": "agents.x.offers", "value": {}},  # unrelated key — must be ignored
        ]
    }
    monkeypatch.setattr(reg, "_aimeat_call", _FakeCall({"aimeat_memory_list": items}))
    got = reg.list_crew_defs(agent="crew-forge")
    assert {e["agent_name"] for e in got} == {"a", "b"}
    assert next(e for e in got if e["agent_name"] == "a")["gaii"] == "a#o@n"


# ── install (materialize; optional register + launch) ─────────────────────────
def test_install_materialize_only(tmp_root, monkeypatch):
    monkeypatch.setattr(reg, "_aimeat_call", _FakeCall({}))
    out = reg.install_crew_def(_good_doc(), agent="crew-forge", register=False)
    assert "INSTALLED (materialized only)" in out
    assert (tmp_root / "crew_defs" / "release_notes_writer.json").is_file()
    assert (tmp_root / "crews" / "release_notes_writer_crew.py").is_file()


def test_install_registers_and_launches_when_requested(tmp_root, monkeypatch):
    monkeypatch.setattr(reg, "_aimeat_call", _FakeCall({}))
    monkeypatch.setattr(forge, "register_and_launch", lambda name: f"REGISTERED+LAUNCHED {name}")
    out = reg.install_crew_def(_good_doc(), agent="crew-forge", register=True)
    assert "REGISTERED+LAUNCHED release-notes-writer" in out
    assert (tmp_root / "crews" / "release_notes_writer_crew.py").is_file()


def test_install_fetches_then_materializes(tmp_root, monkeypatch):
    env = _envelope(_good_doc())
    monkeypatch.setattr(reg, "_aimeat_call", _FakeCall({"aimeat_memory_read": {"value": env}}))
    out = reg.install_crew_def("release-notes-writer", agent="crew-forge", register=False)
    assert "INSTALLED" in out and (tmp_root / "crew_defs" / "release_notes_writer.json").is_file()


def test_installed_crew_passes_subprocess_validator(tmp_root, monkeypatch):
    monkeypatch.setattr(reg, "_aimeat_call", _FakeCall({}))
    reg.install_crew_def(_good_doc(), agent="crew-forge", register=False)
    proc = subprocess.run(
        [sys.executable, "-m", "crewaimeat._validate_crew", "crews/release_notes_writer_crew.py"],
        capture_output=True,
        text=True,
        cwd=str(tmp_root),
        timeout=600,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    assert proc.returncode == 0 and "VALID" in out, out


# ── the crew-forge fleet tools (publish_crew / install_crew) ───────────────────
def _run_tool(tool, **kwargs) -> str:
    fn = getattr(tool, "func", None) or getattr(tool, "_run", None)
    return fn(**kwargs) if fn is not None else tool.run(kwargs)


def test_publish_crew_tool_reads_local_file(tmp_root, monkeypatch):
    forge_json.write_json_crew(_good_doc())  # a crew def exists locally (as after /build-json)
    fake = _FakeCall({"aimeat_memory_write": {"ok": True}})
    monkeypatch.setattr(reg, "_aimeat_call", fake)
    publish_crew, _install = reg.make_registry_tools("crew-forge")
    out = _run_tool(publish_crew, target_agent="release-notes-writer", visibility="owner")
    assert "published crew def 'release-notes-writer'" in out
    assert fake.wrote("aimeat_memory_write")[0]["key"] == "crews.registry.release-notes-writer"


def test_publish_crew_tool_missing_local_file(tmp_root, monkeypatch):
    monkeypatch.setattr(reg, "_aimeat_call", _FakeCall({}))
    publish_crew, _install = reg.make_registry_tools("crew-forge")
    out = _run_tool(publish_crew, target_agent="never-built")
    assert "No crew def at" in out and "build it first" in out.lower()


def test_install_crew_tool_reports_missing_registry(tmp_root, monkeypatch):
    monkeypatch.setattr(reg, "_aimeat_call", _FakeCall({}))  # nothing in the registry
    _publish, install_crew = reg.make_registry_tools("crew-forge")
    out = _run_tool(install_crew, target_agent="ghost", gaii="")
    assert "INSTALL FAILED" in out and "no crew def in the registry" in out

"""crew-forge can turn on persistent CrewAI memory for a generated crew (CrewSpec memory=True).

Offline: dry-runs stage into .candidates and the subprocess validator only imports + calls build_domain
(it never runs run_crew), so memory=True is emitted and validated WITHOUT any embedder / live fleet / node.
"""

from __future__ import annotations

from crewaimeat import forge, forge_eval
from crewaimeat.forge_eval import grade

_BD = (
    "def build_domain(ctx):\n"
    "    a = Agent(role='Assistant', goal='help the user', backstory='b', llm=ctx.llm)\n"
    "    return [a], [Task(description=f'Help with: {ctx.prompt}', expected_output='o', agent=a)]\n"
)


def _order(oid):
    return next(o for o in forge_eval.ORDERS if o.id == oid)


def test_dry_run_emits_memory_true_and_validates(tmp_path, monkeypatch):
    monkeypatch.setattr(forge, "_project_root", lambda: tmp_path)
    ok, detail, path = forge.dry_run_build("mem-assistant", _BD, remember=True)
    assert ok, detail
    src = path.read_text(encoding="utf-8")
    assert "memory=True" in src  # the CrewSpec-level toggle is emitted (like discover)
    assert path.parent.name == ".candidates"  # staged, never registered/launched


def test_memory_off_by_default(tmp_path, monkeypatch):
    monkeypatch.setattr(forge, "_project_root", lambda: tmp_path)
    path = forge.write_crew_file("plain-agent", _BD)  # remember defaults False
    assert "memory=True" not in path.read_text(encoding="utf-8")


def test_write_and_validate_tool_parses_memory_yes(tmp_path, monkeypatch):
    """The Architect-facing tool accepts memory='yes' and threads it to memory=True."""
    monkeypatch.setattr(forge, "_project_root", lambda: tmp_path)
    out = forge.write_and_validate_crew.func(agent_name="mem-tool", build_domain_code=_BD, memory="yes")
    assert "VALID" in out and "MEMORY ON" in out  # prerequisite surfaced, not gated
    src = (tmp_path / "crews" / "mem_tool_crew.py").read_text(encoding="utf-8")
    assert "memory=True" in src


def test_memory_order_grades_on_the_crewspec_toggle(tmp_path, monkeypatch):
    monkeypatch.setattr(forge, "_project_root", lambda: tmp_path)
    order = _order("assistant-memory")
    with_mem = forge.write_crew_file("m1", _BD, remember=True)
    g = grade(order, with_mem)
    assert g.has_memory and not g.memory_missing and g.passed
    without = forge.write_crew_file("m2", _BD, remember=False)
    g2 = grade(order, without)
    assert not g2.has_memory and g2.memory_missing and not g2.passed  # expected memory, didn't enable it

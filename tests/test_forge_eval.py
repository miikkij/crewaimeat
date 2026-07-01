"""Deterministic tests for the crew-forge behavioral eval GRADER (no LLM, no network).

The live eval (running the real Architect) lives in scripts/eval_crew_forge.py and needs a key. Here we
prove the grader itself is trustworthy: given a synthetic generated crew, does it correctly judge whether
the right tools were wired, the shape is sane, and the request is consumed? We also assert the corpus is
well-formed (expectations reference real catalog capabilities).
"""

from __future__ import annotations

from crewaimeat import forge, forge_catalog, forge_eval
from crewaimeat.forge_eval import Grade, Order, grade


def _write(monkeypatch, tmp_path, name, caps, use_prompt=True):
    """Write a synthetic generated crew wiring `caps` (via the real forge template) and return its path."""
    monkeypatch.setattr(forge, "_project_root", lambda: tmp_path)
    desc = 'ctx.prompt or ""' if use_prompt else '"do the thing"'
    if caps:
        splat = ", ".join(f'*T["{c}"]' for c in caps)
        bd = (
            "def build_domain(ctx):\n"
            "    T = _tools(ctx)\n"
            f'    a = Agent(role="R", goal="G", backstory="B", llm=ctx.llm, tools=[{splat}])\n'
            f'    return [a], [Task(description={desc}, expected_output="o", agent=a)]\n'
        )
    else:
        bd = (
            "def build_domain(ctx):\n"
            '    a = Agent(role="R", goal="G", backstory="B", llm=ctx.llm)\n'
            f'    return [a], [Task(description={desc}, expected_output="o", agent=a)]\n'
        )
    return forge.write_crew_file(name, bd, capabilities=",".join(caps))


def _order(oid):
    return next(o for o in forge_eval.ORDERS if o.id == oid)


def test_grade_passes_when_expected_caps_present(tmp_path, monkeypatch):
    path = _write(monkeypatch, tmp_path, "g1", ["memory"])
    g = grade(_order("news-digest"), path)  # expects memory, forbids image/app_build
    assert g.built and g.caps_ok and g.structure_ok and g.passed
    assert "memory" in g.wired and g.agents >= 1 and g.tasks >= 1 and g.prompt_used


def test_grade_fails_when_expected_cap_missing(tmp_path, monkeypatch):
    path = _write(monkeypatch, tmp_path, "g2", ["web"])  # no memory
    g = grade(_order("news-digest"), path)
    assert g.built and not g.passed and g.missing == ["memory"]


def test_grade_fails_on_forbidden_cap(tmp_path, monkeypatch):
    path = _write(monkeypatch, tmp_path, "g3", ["memory", "image"])  # image is forbidden for this order
    g = grade(_order("news-digest"), path)
    assert not g.passed and g.forbidden_used == ["image"]


def test_grade_fails_when_prompt_not_used(tmp_path, monkeypatch):
    path = _write(monkeypatch, tmp_path, "g4", ["memory"], use_prompt=False)
    g = grade(_order("news-digest"), path)
    assert g.built and not g.structure_ok and not g.passed  # ignores the user's request → not done well


def test_grade_pure_reasoning_passes_with_no_tools(tmp_path, monkeypatch):
    path = _write(monkeypatch, tmp_path, "g5", [])
    g = grade(_order("pure-reasoning"), path)  # forbids every tool; a tool-less crew is correct
    assert g.wired == [] and g.passed


def test_grade_no_file_is_not_built():
    g = grade(forge_eval.ORDERS[0], None)
    assert not g.built and not g.passed


def test_corpus_is_well_formed():
    ids = [o.id for o in forge_eval.ORDERS]
    assert len(ids) == len(set(ids)), "order ids must be unique"
    catalog_ids = {c.id for c in forge_catalog.CATALOG}
    for o in forge_eval.ORDERS:
        assert o.expect <= catalog_ids, f"{o.id} expects an unknown capability"
        assert o.forbid <= catalog_ids, f"{o.id} forbids an unknown capability"
        assert not (o.expect & o.forbid), f"{o.id} expect/forbid overlap"


def test_scorecard_renders():
    passed = Grade("ok", built=True, wired=["web"], agents=2, tasks=2, prompt_used=True)
    failed = Grade("bad", built=False, detail="no crew file was produced")
    txt = forge_eval.format_scorecard([passed, failed])
    assert "ORDER" in txt and "PASS" in txt and "FAIL" in txt and "1/2 orders passed" in txt


def test_order_and_grade_types_are_stable():
    o = Order(id="x", request="do x", expect=frozenset({"web"}))
    assert o.min_agents == 1 and o.expect == frozenset({"web"})

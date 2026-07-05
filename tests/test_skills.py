"""SKILL.md skills (crewaimeat.skills + the CrewSpec/BuildContext/crew_def plumbing).

Pure, offline, deterministic — no LLM call, no network, no live state. Exercises the fail-loud
loader class-by-class (so a regression names exactly which guard broke), the plumbing that carries
skills into agents, and the live proof: the joker crew's comedians carry comedy-set-craft in their
RENDERED prompt (the crewai <skills> block), while the host stays clean.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from crewaimeat.skills import SkillLoadError, load_skills, skills_root

REPO = Path(__file__).resolve().parents[1]
SKILLS_DIR = REPO / "skills"


def _write_skill(root: Path, name: str, body: str = "Do the thing well.", frontmatter: str | None = None) -> Path:
    d = root / name
    d.mkdir(parents=True)
    fm = frontmatter if frontmatter is not None else f"---\nname: {name}\ndescription: what + when\n---\n"
    (d / "SKILL.md").write_text(fm + body, encoding="utf-8")
    return d


# ── loader: the happy path ─────────────────────────────────────────────────────
def test_load_by_name(tmp_path):
    _write_skill(tmp_path, "my-skill", body="The expertise body.")
    (skill,) = load_skills(["my-skill"], root=tmp_path)
    assert skill.name == "my-skill"
    assert skill.instructions == "The expertise body."  # activated: body loaded, prompt-ready
    assert skill.frontmatter.description == "what + when"


def test_load_by_direct_path(tmp_path):
    d = _write_skill(tmp_path, "pathy-skill")
    (skill,) = load_skills([d])
    assert skill.name == "pathy-skill"


def test_env_root_override(tmp_path, monkeypatch):
    _write_skill(tmp_path, "env-skill")
    monkeypatch.setenv("CREWAIMEAT_SKILLS_DIR", str(tmp_path))
    assert skills_root() == tmp_path
    (skill,) = load_skills(["env-skill"])
    assert skill.name == "env-skill"


def test_shipped_skills_are_valid():
    skills = load_skills(["comedy-set-craft", "sanomat-editorial-style"], root=SKILLS_DIR)
    assert [s.name for s in skills] == ["comedy-set-craft", "sanomat-editorial-style"]
    for s in skills:
        assert s.instructions  # bodies present and loaded
        assert len(s.instructions) < 50_000  # contract guideline


# ── loader: every failure is LOUD (no warn-and-skip) ──────────────────────────
def test_missing_directory_fails(tmp_path):
    with pytest.raises(SkillLoadError, match="directory not found"):
        load_skills(["no-such-skill"], root=tmp_path)


def test_missing_skill_md_fails(tmp_path):
    (tmp_path / "empty-skill").mkdir()
    with pytest.raises(SkillLoadError, match="no SKILL.md"):
        load_skills(["empty-skill"], root=tmp_path)


def test_name_dir_mismatch_fails(tmp_path):
    _write_skill(tmp_path, "dir-name", frontmatter="---\nname: other-name\ndescription: d\n---\n")
    with pytest.raises(SkillLoadError, match="does not match"):
        load_skills(["dir-name"], root=tmp_path)


def test_bad_name_charset_fails(tmp_path):
    _write_skill(tmp_path, "Bad_Name", frontmatter="---\nname: Bad_Name\ndescription: d\n---\n")
    with pytest.raises(SkillLoadError):
        load_skills(["Bad_Name"], root=tmp_path)


def test_missing_frontmatter_fails(tmp_path):
    d = tmp_path / "bare-skill"
    d.mkdir()
    (d / "SKILL.md").write_text("just a body, no frontmatter", encoding="utf-8")
    with pytest.raises(SkillLoadError, match="frontmatter"):
        load_skills(["bare-skill"], root=tmp_path)


def test_missing_description_fails(tmp_path):
    _write_skill(tmp_path, "no-desc", frontmatter="---\nname: no-desc\n---\n")
    with pytest.raises(SkillLoadError, match="description"):
        load_skills(["no-desc"], root=tmp_path)


def test_duplicate_declaration_fails(tmp_path):
    _write_skill(tmp_path, "twice")
    with pytest.raises(SkillLoadError, match="declared twice"):
        load_skills(["twice", "twice"], root=tmp_path)


def test_oversize_body_warns_but_loads(tmp_path, capsys):
    _write_skill(tmp_path, "big-skill", body="x" * 50_001)
    (skill,) = load_skills(["big-skill"], root=tmp_path)
    assert skill.name == "big-skill"
    assert "WARNING" in capsys.readouterr().err


# ── plumbing: CrewSpec / BuildContext carry skills ─────────────────────────────
def test_buildcontext_defaults_to_no_skills():
    from crewaimeat.aimeat_crew import BuildContext

    ctx = BuildContext(task={}, prompt="p", llm=None, today="t")
    assert ctx.skills is None  # None (not []) — crewai rejects an empty skills list on Agent


def test_crewspec_accepts_skills():
    from crewaimeat.aimeat_crew import CrewSpec

    spec = CrewSpec(agent_name="x", build_domain=lambda ctx: ([], []), skills=["comedy-set-craft"])
    assert spec.skills == ["comedy-set-craft"]


# ── the live proof: joker's comedians carry the skill in their rendered prompt ─
def test_joker_agents_carry_comedy_set_craft():
    from crewai.utilities.prompts import Prompts

    from crews import joker_crew
    from tests.crew_fixtures import make_ctx

    ctx = make_ctx()
    ctx.skills = load_skills(["comedy-set-craft"], root=SKILLS_DIR)
    agents, _tasks = joker_crew.build_domain(ctx)
    comedians, host = agents[:4], agents[-1]

    for comic in comedians:
        prompt = Prompts(agent=comic, i18n=comic.i18n).task_execution()["prompt"]
        assert "<skills>" in prompt
        assert "comedy-set-craft" in prompt
        assert "Punchline last" in prompt  # the BODY is injected, not just the name
    # the host only presents — no skill block
    host_prompt = Prompts(agent=host, i18n=host.i18n).task_execution()["prompt"]
    assert "<skills>" not in host_prompt


# ── declarative crew-def parity ────────────────────────────────────────────────
def _skill_doc() -> dict:
    return {
        "agent_name": "demo",
        "skills": ["comedy-set-craft"],
        "agents": [{"name": "w", "role": "Writer", "goal": "Write", "backstory": "You write."}],
        "tasks": [{"id": "t1", "agent": "w", "description": "About: {{ctx.prompt}}", "expected_output": "Text."}],
    }


def test_crew_doc_skills_validate():
    from crewaimeat.crew_def import validate_crew_doc

    assert validate_crew_doc(_skill_doc()) == []
    bad = _skill_doc() | {"skills": "comedy-set-craft"}
    assert any("skills" in e for e in validate_crew_doc(bad))
    empty = _skill_doc() | {"skills": []}
    assert any("skills" in e for e in validate_crew_doc(empty))
    non_str = _skill_doc() | {"skills": ["ok", 3]}
    assert any("skills" in e for e in validate_crew_doc(non_str))


def test_crewspec_from_json_carries_skills():
    from crewaimeat.crew_def import crewspec_from_json

    spec = crewspec_from_json(_skill_doc())
    assert spec.skills == ["comedy-set-craft"]


def test_build_domain_from_json_applies_ctx_skills():
    from crewaimeat.crew_def import build_domain_from_json
    from tests.crew_fixtures import make_ctx

    ctx = make_ctx()
    ctx.skills = load_skills(["comedy-set-craft"], root=SKILLS_DIR)
    agents, _tasks = build_domain_from_json(_skill_doc(), ctx)
    assert [s.name for s in agents[0].skills] == ["comedy-set-craft"]


def test_shipped_joker_doc_declares_the_skill():
    from crewaimeat.crew_def import load_crew_doc, validate_crew_doc

    doc = load_crew_doc(REPO / "crew_defs" / "joker.json")
    assert doc["skills"] == ["comedy-set-craft"]
    assert validate_crew_doc(doc) == []

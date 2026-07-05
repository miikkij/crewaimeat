"""SKILL.md skills for crews — fail-loud loading on top of crewai's native Agent Skills.

A skill is a portable expertise pack: a directory ``skill-name/`` holding a required
``SKILL.md`` (YAML frontmatter + markdown body) plus optional ``scripts/`` ``references/``
``assets/``. The body is injected into an agent's prompt on activation (crewai renders
activated skills as a ``<skills>`` block). Contract (shared with the AIMEAT registry side,
spec doc-sdie0se): frontmatter ``name`` (1-64 chars, lowercase alphanumeric+hyphens, MUST
match the dir name) + ``description`` (1-1024 chars, "what + when"); optional
``license``/``compatibility``/``metadata``; ``allowed-tools`` is metadata only. crewai's
parser (``crewai.skills``) enforces all of that — this module reuses it, never reimplements.

Why this wrapper exists at all: crewai's own ``discover_skills()`` WARNING-skips a malformed
skill (a silent fallback). Here a crew DECLARES the skills it needs (``CrewSpec.skills``),
so a skill that is missing or malformed is a configuration error — ``load_skills`` raises
immediately with the skill name and real cause, at daemon start, not mid-task.

Repo convention: skills live in ``<repo>/skills/<skill-name>/SKILL.md``. Bare names resolve
against ``CREWAIMEAT_SKILLS_DIR`` (env) else ``<cwd>/skills`` (the fleet runs with cwd=repo);
a path-like item (contains a separator or points at an existing dir) is used as-is.

Usage (a crew author):

    CrewSpec(agent_name="joker", build_domain=build_domain, skills=["comedy-set-craft"])

    def build_domain(ctx):
        worker = Agent(..., llm=ctx.llm, skills=ctx.skills)  # same idiom as ctx.llm
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable
from pathlib import Path

from crewai.skills.loader import activate_skill
from crewai.skills.models import Skill
from crewai.skills.parser import SKILL_FILENAME, load_skill_metadata

__all__ = ["SkillLoadError", "load_skills", "skills_root"]

_BODY_SOFT_LIMIT = 50_000  # contract guideline: keep the body focused (<~50k chars)


class SkillLoadError(ValueError):
    """A declared skill could not be loaded. Names the skill and the real cause."""


def skills_root() -> Path:
    """The default directory bare skill names resolve under.

    ``CREWAIMEAT_SKILLS_DIR`` (env) wins; else ``<cwd>/skills`` — the fleet entrypoints run
    every crew with cwd=repo root, so this is the repo's ``skills/`` in normal operation.
    """
    env = os.environ.get("CREWAIMEAT_SKILLS_DIR", "").strip()
    return Path(env) if env else Path.cwd() / "skills"


def _resolve_dir(item: str | Path, root: Path) -> Path:
    """A bare name resolves under root; anything path-like is used as given."""
    if isinstance(item, Path):
        return item
    if any(sep in item for sep in ("/", "\\")) or Path(item).is_dir():
        return Path(item)
    return root / item


def load_skills(items: Iterable[str | Path], root: str | Path | None = None) -> list[Skill]:
    """Load + validate the declared skills, activated to INSTRUCTIONS level, fail LOUD.

    Each item is a skill name (a directory under ``root``, default :func:`skills_root`) or a
    direct path to a skill directory. Every skill must parse and validate against the
    SKILL.md contract; the first failure raises :class:`SkillLoadError` naming the skill and
    the underlying cause — no warning-and-skip. Returns crewai ``Skill`` objects ready for
    ``Agent(skills=[...])`` (pre-activated, so crewai appends them without re-discovery).
    """
    base = Path(root) if root is not None else skills_root()
    loaded: list[Skill] = []
    seen: set[str] = set()
    for item in items:
        skill_dir = _resolve_dir(item, base)
        if not skill_dir.is_dir():
            raise SkillLoadError(
                f"skill '{item}': directory not found: {skill_dir} "
                f"(bare names resolve under {base}; set CREWAIMEAT_SKILLS_DIR to override)"
            )
        if not (skill_dir / SKILL_FILENAME).is_file():
            raise SkillLoadError(f"skill '{item}': no {SKILL_FILENAME} in {skill_dir}")
        try:
            skill = load_skill_metadata(skill_dir)  # parses frontmatter + dir-name match
            skill = activate_skill(skill)  # reads the body (INSTRUCTIONS level)
        except Exception as exc:
            raise SkillLoadError(f"skill '{item}' ({skill_dir}): {exc}") from exc
        if skill.name in seen:
            raise SkillLoadError(f"skill '{skill.name}' declared twice")
        seen.add(skill.name)
        body_len = len(skill.instructions or "")
        if body_len > _BODY_SOFT_LIMIT:
            print(
                f"[skills] WARNING: '{skill.name}' body is {body_len} chars "
                f"(guideline <{_BODY_SOFT_LIMIT}) — large injections dilute agent attention",
                file=sys.stderr,
                flush=True,
            )
        loaded.append(skill)
    return loaded

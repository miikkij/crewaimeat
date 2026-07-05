"""Shared fixtures for the crew test floor: the full crew list and a stub BuildContext.

``make_ctx`` returns a BuildContext-shaped stub (the fields ``build_domain`` reads). The LLM is a
real ``crewai.LLM`` object built offline with a dummy key — a real object (not a MagicMock) so it
passes CrewAI's Agent validation, but it is never called by these deterministic tests.
"""

from __future__ import annotations

from types import SimpleNamespace

from crewai import LLM

# All 27 crews under crews/ (module names, imported as ``crews.<name>``).
CREW_MODULES = [
    "aimeat_app_builder_crew",
    "aimeat_app_conductor_crew",
    "aimeat_app_designer_crew",
    "aimeat_app_editor_crew",
    "aimeat_app_specs_designer_crew",
    "aimeat_cortex_fixer_crew",
    "aimeat_crew_forge_crew",
    "aimeat_extension_builder_crew",
    "aimeat_realtime_builder_crew",
    "aimeat_sdlc_architect_crew",
    "crew_forge_crew",
    "daily_briefing_crew_crew",
    "editorial_writer_crew",
    "finnish_corporate_researcher_crew",
    "idea_feasibility_rater_crew",
    "jingle_writer_crew",
    "joker_crew",
    "joker_v2_crew",
    "librarian_crew",
    "news_fetcher_crew",
    "news_writer_crew",
    "probability_creator_crew",
    "sanity_checker_crew",
    "tagline_translator_crew",
    "web_researcher_crew",
    "web_tester_crew",
    "workflow_manager_crew",
]

# A distinctive ask so we can prove ctx.prompt reaches a task description (TSK-4 / the
# crew-builddomain-must-inject-ctx-prompt lesson).
SENTINEL = "koi-pond-XYZZY"

_TODAY = (
    "CURRENT TIME (reference for anything time/date related): 2026-06-05 12:00 UTC "
    "= 2026-06-05 15:00 EEST (Friday). Treat THIS as the single source of truth for 'today'."
)


def make_ctx(prompt: str | None = None):
    """A BuildContext-shaped stub for calling ``build_domain`` offline."""
    p = prompt or f"Build a {SENTINEL} sensor dashboard"
    return SimpleNamespace(
        llm=LLM(
            model="openrouter/x-ai/grok-4-fast",
            api_key="test-not-used",
            base_url="https://openrouter.ai/api/v1",
        ),
        prompt=p,
        today=_TODAY,
        directives="",
        task={"id": "t-0001-test", "description": p, "title": p[:40]},
        skills=None,  # BuildContext.skills — loaded SKILL.md skills; None like a skill-less run
    )

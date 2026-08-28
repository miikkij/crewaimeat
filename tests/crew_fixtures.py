"""Shared fixtures for the crew test floor: the full crew list and a stub BuildContext.

``make_ctx`` returns a BuildContext-shaped stub (the fields ``build_domain`` reads). The LLM is a
real ``crewai.LLM`` object built offline with a dummy key — a real object (not a MagicMock) so it
passes CrewAI's Agent validation, but it is never called by these deterministic tests.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from crewai import LLM

CREWS_DIR = Path(__file__).resolve().parent.parent / "crews"

# EVERY live crew under crews/ (module names, imported as ``crews.<name>``), DERIVED FROM DISK so a
# new crew is contract-tested the day it lands and a parked one leaves the list by itself. A leading
# underscore parks a crew — the SAME rule the fleet host uses (crewaimeat.forge._crew_files), so the
# test floor and the fleet can never disagree about what "a live crew" is. The hand-kept list this
# replaced went stale twice over: it named 21 of 46 live crews and still listed four crews that had
# been parked (their modules no longer exist under those names), which failed 16 tests permanently
# and hid every real regression behind the standing red.
LIVE_CREW_MODULES = sorted(p.stem for p in CREWS_DIR.glob("*_crew.py") if not p.name.startswith("_"))


def _has_build_domain(stem: str) -> bool:
    """True when the crew authors its own (agents, tasks). A brain stub does not — its behavior lives
    in the JSON brain (crewaimeat.brains), edited in the agency cockpit, and the stub only calls
    run_brain. Read from SOURCE, not by importing, so collecting the parametrization never executes
    crew module code."""
    return "\ndef build_domain(" in (CREWS_DIR / f"{stem}.py").read_text(encoding="utf-8")


def _is_node_backed(stem: str) -> bool:
    """True when the crew's definition lives at `crews.registry.<agent>` rather than in this repo.

    These tests are declared deterministic, no LLM, NO NETWORK — and a node-backed crew's
    `build_domain` has to read the node to know what it is building. There is nothing local to
    assert about: the loader holds a name. So it is excluded here and covered where it belongs, by
    `tests/test_json_agent.py` (the loading contract) and `crewaimeat try` (the definition itself).
    Read from source, like `_has_build_domain`, so collecting never imports a crew.
    """
    return '\nCREW_DEF_SOURCE = "node"' in (CREWS_DIR / f"{stem}.py").read_text(encoding="utf-8")


NODE_BACKED_MODULES = [m for m in LIVE_CREW_MODULES if _is_node_backed(m)]

# The build_domain contract floor applies to crews that HAVE a build_domain. Brain stubs are held to
# their own (smaller) contract in test_build_domain.test_brain_stubs_are_really_brain_stubs, so a
# crew can never leave the floor merely by not defining the function.
CREW_MODULES = [m for m in LIVE_CREW_MODULES if _has_build_domain(m) and m not in NODE_BACKED_MODULES]
BRAIN_STUB_MODULES = [m for m in LIVE_CREW_MODULES if not _has_build_domain(m) and m not in NODE_BACKED_MODULES]

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

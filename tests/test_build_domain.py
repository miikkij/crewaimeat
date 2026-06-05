"""L1 — per-crew ``build_domain`` contract tests (deterministic, no LLM, no network).

A crew's ``build_domain(ctx)`` is pure given a stub context, so a sub-second test catches the
regressions the fleet audit found repeatedly: dropped ``ctx.prompt`` injection, broken
``context=[...]`` chaining, an agent told to use a tool it does not have, and unbounded ``max_iter``.
The same skeleton runs across all 27 crews.
"""

from __future__ import annotations

import importlib

import pytest

from crew_fixtures import CREW_MODULES, SENTINEL, make_ctx


def _build(module_name, prompt=None):
    mod = importlib.import_module(f"crews.{module_name}")
    agents, tasks = mod.build_domain(make_ctx(prompt))
    return mod, agents, tasks


# max_iter is a deliberate BACKSTOP, not a gap to close. Field data (2026-06-05, live operator runs)
# overturned the static audit's "cap to 40": the cap fires only on NON-CONVERGENT re-authoring loops,
# is load-bearing for the builder/fixer/editor/web-tester crews, and lowering it merely makes a doomed
# loop fail faster (it cannot distinguish thrashing from legitimate build depth). The real runaway
# bound is a wall-clock (AIMEAT_AGENT_MAX_EXECUTION_TIME) plus gating completion on the verify verdict;
# see the FIELD UPDATE in docs/aimeat-guides/nextgeneration/04-general-improvement-roadmap.md. So we pin
# the real invariant (no delegation) and only flag an absurd max_iter (a typo), never a "too high" budget.
MAX_ITER_SANITY_CEILING = 120


@pytest.mark.parametrize("module_name", CREW_MODULES)
def test_build_domain_structural(module_name):
    """Every crew returns a non-empty (agents, tasks); each task has a real description and an
    agent that is part of the crew; chained tasks reference earlier tasks."""
    _mod, agents, tasks = _build(module_name)
    assert agents, f"{module_name}: build_domain returned no agents"
    assert tasks, f"{module_name}: build_domain returned no tasks"
    agent_ids = {id(a) for a in agents}
    for i, t in enumerate(tasks):
        assert isinstance(t.description, str) and t.description.strip(), (
            f"{module_name}: task #{i} has an empty description"
        )
        assert t.agent is not None and id(t.agent) in agent_ids, (
            f"{module_name}: task #{i}'s agent is not in the crew's agent list"
        )
        # CrewAI 1.14.6 defaults Task.context to a _NotSpecified sentinel; only a real list is a chain.
        ctx_list = getattr(t, "context", None)
        if isinstance(ctx_list, list):
            task_ids = {id(x) for x in tasks}
            for ctx_task in ctx_list:
                assert id(ctx_task) in task_ids, (
                    f"{module_name}: task #{i} chains a context task not built by this crew"
                )


@pytest.mark.parametrize("module_name", CREW_MODULES)
def test_ctx_prompt_is_injected(module_name):
    """The user's ask (ctx.prompt) must reach at least one task description, or the agent never
    sees the task (the crew-builddomain-must-inject-ctx-prompt failure)."""
    _mod, _agents, tasks = _build(module_name)
    joined = "\n".join((t.description or "") for t in tasks)
    assert SENTINEL in joined or "koi-pond" in joined, (
        f"{module_name}: ctx.prompt was not injected into any task description"
    )


@pytest.mark.parametrize("module_name", CREW_MODULES)
def test_workers_are_non_delegating(module_name):
    """No worker enables delegation (no accidental delegation loops). allow_delegation defaults to
    False in CrewAI 1.14.6; this pins the intent fleet-wide."""
    _mod, agents, _tasks = _build(module_name)
    for a in agents:
        assert a.allow_delegation is False, f"{module_name}: '{a.role}' must not enable delegation"


@pytest.mark.parametrize("module_name", CREW_MODULES)
def test_max_iter_is_a_sane_backstop(module_name):
    """max_iter is an intentional backstop, not a gap (field finding 2026-06-05: the cap only fires on
    non-convergent re-authoring loops and cannot tell thrashing from legitimate build depth; the real
    runaway bound is a wall-clock + verify-gated completion, not a low iteration cap). Only an absurd
    value (a typo) fails."""
    _mod, agents, _tasks = _build(module_name)
    for a in agents:
        assert (a.max_iter or 0) <= MAX_ITER_SANITY_CEILING, (
            f"{module_name}: '{a.role}' max_iter={a.max_iter} looks like a typo (> {MAX_ITER_SANITY_CEILING})"
        )


# ---- Regression tests for the two live bugs fixed in this change ----
def test_news_writer_writer_agents_have_memory_tools():
    """Regression: every news-writer agent instructed to call write_memory must actually have a
    write_memory tool (the three category writers previously had no tools=, so articles never
    reached memory)."""
    _mod, _agents, tasks = _build("news_writer_crew", prompt="2026-06-05 morning edition")
    checked = 0
    for t in tasks:
        if "write_memory(" in (t.description or ""):
            checked += 1
            tool_names = {getattr(tool, "name", "") for tool in (t.agent.tools or [])}
            assert "write_memory" in tool_names, (
                f"news_writer: agent '{t.agent.role}' is told to call write_memory but has no such tool"
            )
    assert checked >= 3, "expected the 3 category-writer tasks to instruct write_memory"


def test_finnish_researcher_has_no_unsubstituted_placeholders():
    """Regression: the synthesis report header was a non-f-string, so '{ctx.today}'/'{ctx.prompt}'
    printed verbatim. No task description may contain the literal placeholders, and the real query
    must appear."""
    _mod, _agents, tasks = _build("finnish_corporate_researcher_crew", prompt="Nokia Oyj 0112038-9")
    for t in tasks:
        d = t.description or ""
        assert "{ctx.today}" not in d, "finnish: literal {ctx.today} leaked into a task description"
        assert "{ctx.prompt}" not in d, "finnish: literal {ctx.prompt} leaked into a task description"
    joined = "\n".join((t.description or "") for t in tasks)
    assert "Nokia" in joined, "finnish: the real query did not reach the tasks"

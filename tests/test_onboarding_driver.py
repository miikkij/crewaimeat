"""Onboarding migrated to aimeat-crewai 0.12.0's deterministic run_hello_integration driver.

These exercise our WRAPPER (_run_onboarding_only) and the completion check (_onboarding_completed)
with fakes — no MCP, no node, no LLM. The driver itself is unit-tested upstream in aimeat-crewai.
"""

from __future__ import annotations

import crewaimeat.aimeat_crew as ac


class _FakeLiaison:
    def __init__(self, tools):
        self.tools = tools


class _FakeCM:
    """Stand-in for create_liaison_agent(...) used as a context manager."""

    def __init__(self, liaison):
        self._liaison = liaison

    def __enter__(self):
        return self._liaison

    def __exit__(self, *exc):
        return False


def _patch_serve(monkeypatch):
    monkeypatch.setattr(ac, "_wait_for_serve", lambda *a, **k: {})
    monkeypatch.setattr(ac, "serve_params", lambda **k: {"params": True})
    monkeypatch.setattr(ac, "get_llm", lambda **k: object())


# ── _onboarding_completed: recognizes the 1.35 summary.completable AND the old status field ──────
def test_onboarding_completed_via_summary(monkeypatch):
    monkeypatch.setattr(ac, "_aimeat_call", lambda *a, **k: {"summary": {"completable": True}})
    assert ac._onboarding_completed("x") is True


def test_onboarding_completed_via_old_status(monkeypatch):
    monkeypatch.setattr(ac, "_aimeat_call", lambda *a, **k: {"onboarding": {"status": "completed"}})
    assert ac._onboarding_completed("x") is True


def test_onboarding_not_completed(monkeypatch):
    monkeypatch.setattr(
        ac, "_aimeat_call", lambda *a, **k: {"summary": {"completable": False}, "onboarding": {"status": "in_progress"}}
    )
    assert ac._onboarding_completed("x") is False


# ── _run_onboarding_only: drives via run_hello_integration, overrides publish_commands ───────────
def test_run_onboarding_only_passes_real_commands(monkeypatch):
    _patch_serve(monkeypatch)
    monkeypatch.setattr(ac, "create_liaison_agent", lambda **k: _FakeCM(_FakeLiaison([])))
    captured: dict = {}

    def fake_driver(tools, *, agent_name, step_args=None, logger=None, **k):
        captured["agent_name"] = agent_name
        captured["step_args"] = step_args
        return {"summary": {"completable": True}}

    monkeypatch.setattr(ac, "run_hello_integration", fake_driver)
    cmds = [{"name": "/x", "description": "d", "category": "c"}]
    ac._run_onboarding_only("my-crew", services=None, commands=cmds)
    assert captured["agent_name"] == "my-crew"
    assert captured["step_args"]["publish_commands"] == {
        "key": "agents.my-crew.commands",
        "value": cmds,
        "visibility": "owner",
    }


def test_run_onboarding_only_handles_old_node(monkeypatch):
    """node <1.35 -> driver raises OnboardingError -> must be swallowed (agent already authorized)."""
    _patch_serve(monkeypatch)
    monkeypatch.setattr(ac, "create_liaison_agent", lambda **k: _FakeCM(_FakeLiaison([])))

    def boom(*a, **k):
        raise ac.OnboardingError("not completable but reports no next_required_step")

    monkeypatch.setattr(ac, "run_hello_integration", boom)
    ac._run_onboarding_only("my-crew")  # must NOT raise


# ── _effective_mode: crewaimeat crews are task-runners (so tasks auto-activate, no manual Start) ──
def _spec(**kw):
    return ac.CrewSpec(agent_name="x", build_domain=lambda ctx: ([], []), **kw)


def test_effective_mode_defaults_task_runner():
    assert ac._effective_mode(_spec()) == "task-runner"


def test_effective_mode_dm_or_self_monitor_stays_interactive():
    assert ac._effective_mode(_spec(dm_serviceable=True)) == "interactive"
    assert ac._effective_mode(_spec(self_monitor=True)) == "interactive"


def test_effective_mode_explicit_override_wins():
    assert ac._effective_mode(_spec(mode="coordinator")) == "coordinator"
    assert ac._effective_mode(_spec(mode="task-runner", dm_serviceable=True)) == "task-runner"


def test_run_onboarding_only_seeds_services(monkeypatch):
    _patch_serve(monkeypatch)
    calls: list = []

    class _DS:
        name = "aimeat_onboarding_declare_services"

        def run(self, **kw):
            calls.append(kw)

    monkeypatch.setattr(ac, "create_liaison_agent", lambda **k: _FakeCM(_FakeLiaison([_DS()])))
    monkeypatch.setattr(ac, "run_hello_integration", lambda *a, **k: {"summary": {"completable": True}})
    svcs = [{"id": "s1"}]
    ac._run_onboarding_only("my-crew", services=svcs)
    assert calls and calls[0]["services"] == svcs

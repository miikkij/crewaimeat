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
    # _run_onboarding_only pre-resolves the test task id — never let a unit test reach a live daemon
    monkeypatch.setattr(ac, "_aimeat_call", lambda *a, **k: None)


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


def test_run_onboarding_only_preresolves_test_task_id(monkeypatch):
    """The node stopped shipping hints.test_task_id -> the scaffold must hand the driver ready-made
    step_args for BOTH task steps so the package's {test_task_id} substitution is never needed."""
    _patch_serve(monkeypatch)
    monkeypatch.setattr(ac, "create_liaison_agent", lambda **k: _FakeCM(_FakeLiaison([])))
    monkeypatch.setattr(ac, "_resolve_test_task_id", lambda name: "T9")
    captured: dict = {}

    def fake_driver(tools, *, agent_name, step_args=None, logger=None, **k):
        captured["step_args"] = step_args
        return {"summary": {"completable": True}}

    monkeypatch.setattr(ac, "run_hello_integration", fake_driver)
    ac._run_onboarding_only("my-crew")
    assert captured["step_args"]["accept_test_task"] == {
        "task_id": "T9",
        "todos": [dict(t) for t in ac._TEST_TASK_TODOS],
    }
    assert captured["step_args"]["complete_test_task"]["task_id"] == "T9"


def test_safety_net_runs_even_on_onboarding_error(monkeypatch):
    """Rounds exhausted on ONE stuck step must not starve the remaining drivable steps: the
    OnboardingError from run_hello_integration is caught INSIDE the liaison context and
    _finish_pending_onboarding still runs (the publish_config-starved-forever bug)."""
    _patch_serve(monkeypatch)
    monkeypatch.setattr(ac, "create_liaison_agent", lambda **k: _FakeCM(_FakeLiaison([])))
    monkeypatch.setattr(ac, "_resolve_test_task_id", lambda name: None)

    def boom(*a, **k):
        raise ac.OnboardingError("Hello Integration not completed within 18 rounds.")

    monkeypatch.setattr(ac, "run_hello_integration", boom)
    ran: list = []
    monkeypatch.setattr(ac, "_finish_pending_onboarding", lambda *a, **k: ran.append(True))
    ac._run_onboarding_only("my-crew")  # must NOT raise
    assert ran == [True]


def test_finish_pending_resolves_propose_todos_placeholder(monkeypatch):
    """The safety net resolves an unsubstituted {test_task_id} for aimeat_task_propose_todos
    (accept_test_task) from the step's OWN details.testTaskId — not just for aimeat_task_complete."""
    monkeypatch.setattr(ac.time, "sleep", lambda *_: None)
    proposed: list = []

    class _Status:
        name = "aimeat_onboarding_status"

        def __init__(self):
            self.calls = 0

        def run(self, **kw):
            self.calls += 1
            if self.calls > 1:  # after one drive round, nothing is drivable -> loop terminates
                return {"onboarding": {"steps": []}, "step_guide": {}}
            return {
                "onboarding": {
                    "steps": [
                        {
                            "id": "accept_test_task",
                            "required": True,
                            "status": "pending",
                            "details": {"testTaskId": "T123"},
                        }
                    ]
                },
                "step_guide": {
                    "accept_test_task": {
                        "tool": "aimeat_task_propose_todos",
                        "args": {"task_id": "{test_task_id}", "todos": [{"title": "x", "verification": "y"}]},
                    }
                },
            }

    class _Propose:
        name = "aimeat_task_propose_todos"

        def run(self, **kw):
            proposed.append(kw)

    ac._finish_pending_onboarding([_Status(), _Propose()], "x", {})
    assert proposed and proposed[0]["task_id"] == "T123"


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

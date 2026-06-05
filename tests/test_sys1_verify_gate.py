"""SYS-1: gate task completion on the deterministic verify-gate outcome (not the agent's self-report),
plus the install_cortex idempotency guard. All deterministic, no LLM, no network.

SYS-1 is the field-found #1 bug: a direct (non-conductor) build whose verify_render/verify_interaction
FAILED — or that never ran a gate — was still marked `done`. These tests pin the new behaviour:
require_verify_pass=True fails the task instead of completing 'green'.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import crewaimeat.aimeat_crew as ac
import crewaimeat.author_tool as at


def _capture_calls(monkeypatch):
    """Replace _aimeat_call so we can see whether complete or fail was invoked."""
    calls = []
    monkeypatch.setattr(
        ac, "_aimeat_call",
        lambda agent, tool, payload: calls.append((tool, payload)) or {"ok": True},
    )
    return calls


# ---- the run-scoped verify-verdict registry ----
def test_verdict_registry_records_resets_and_reads():
    at.reset_verify_verdicts("tt")
    assert at.get_verify_verdicts("tt") == {}
    at._record_verify("tt", "verify_render", {"ok": True})
    at._record_verify("tt", "verify_interaction", {"ok": False})
    at._record_verify("tt", "verify_anon_render", {"ok": None, "skipped": "no playwright"})
    v = at.get_verify_verdicts("tt")
    assert v["verify_render"]["ok"] is True
    assert v["verify_interaction"]["ok"] is False
    assert v["verify_anon_render"]["ok"] is None and v["verify_anon_render"]["skipped"] is True
    at.reset_verify_verdicts("tt")
    assert at.get_verify_verdicts("tt") == {}


# ---- the completion gate ----
def test_gate_fails_task_when_a_gate_failed(monkeypatch):
    at._VERIFY_VERDICTS["t-fail"] = {"verify_render": {"ok": True}, "verify_interaction": {"ok": False}}
    calls = _capture_calls(monkeypatch)
    ac._make_complete_cb("agent-x", "t-fail", require_verify=True)(MagicMock())
    tools = [c[0] for c in calls]
    assert "aimeat_task_fail" in tools and "aimeat_task_complete" not in tools


def test_gate_fails_task_when_no_gate_ran(monkeypatch):
    at._VERIFY_VERDICTS["t-none"] = {}  # the cortex-fixer "never deployed / never verified" case
    calls = _capture_calls(monkeypatch)
    ac._make_complete_cb("agent-x", "t-none", require_verify=True)(MagicMock())
    tools = [c[0] for c in calls]
    assert "aimeat_task_fail" in tools and "aimeat_task_complete" not in tools


def test_gate_completes_when_a_gate_passed_and_none_failed(monkeypatch):
    at._VERIFY_VERDICTS["t-pass"] = {
        "verify_render": {"ok": True},
        "verify_anon_render": {"ok": None, "skipped": True},  # skipped is not a failure
    }
    calls = _capture_calls(monkeypatch)
    ac._make_complete_cb("agent-x", "t-pass", require_verify=True)(MagicMock())
    tools = [c[0] for c in calls]
    assert "aimeat_task_complete" in tools and "aimeat_task_fail" not in tools


def test_default_completes_without_gating(monkeypatch):
    calls = _capture_calls(monkeypatch)
    ac._make_complete_cb("agent-x", "t-default")(MagicMock())  # require_verify defaults False
    tools = [c[0] for c in calls]
    assert "aimeat_task_complete" in tools and "aimeat_task_fail" not in tools


# ---- the published-app rollback-baseline registry + auto-revert (SYS-1) ----
def test_baseline_registry_records_first_only_and_resets():
    at.reset_published_baselines("bk")
    assert at.get_published_baselines("bk") == {}
    at._record_publish_baseline("bk", "app.html", 4)
    at._record_publish_baseline("bk", "app.html", 9)      # later publishes don't move the baseline
    at._record_publish_baseline("bk", "new.html", None)   # brand-new app -> no prior version
    b = at.get_published_baselines("bk")
    assert b["app.html"] == 4 and b["new.html"] is None
    at.reset_published_baselines("bk")
    assert at.get_published_baselines("bk") == {}


def test_revert_apps_to_baseline_only_reverts_apps_with_a_prior_version(monkeypatch):
    at.reset_published_baselines("rb")
    at._record_publish_baseline("rb", "good.html", 3)
    at._record_publish_baseline("rb", "brandnew.html", None)
    calls = []
    monkeypatch.setattr(at, "_discover_owner", lambda a: "owner")
    monkeypatch.setattr(at, "_node_base", lambda a, o: "http://n")
    monkeypatch.setattr(
        at, "_revert_app_rest",
        lambda agent, owner, base, filename, to_version: calls.append((filename, to_version)) or (True, ""),
    )
    out = at.revert_apps_to_baseline("agent-x", "rb")
    assert calls == [("good.html", 3)]   # brandnew.html (no prior version) is skipped
    by_file = {r["filename"]: r for r in out}
    assert by_file["good.html"]["ok"] is True
    assert by_file["brandnew.html"]["ok"] is False


def test_gate_fail_with_auto_revert_reverts_and_still_fails_task(monkeypatch):
    at._VERIFY_VERDICTS["t-rev"] = {"verify_render": {"ok": False}}
    at.reset_published_baselines("t-rev")
    at._record_publish_baseline("t-rev", "broken.html", 7)
    tools = _capture_calls(monkeypatch)
    rev_calls = []
    monkeypatch.setattr(
        at, "revert_apps_to_baseline",
        lambda agent, tid, owner=None: rev_calls.append((agent, tid, owner))
        or [{"filename": "broken.html", "to_version": 7, "ok": True, "detail": ""}],
    )
    ac._make_complete_cb("agent-x", "t-rev", require_verify=True, owner="owner", auto_revert=True)(MagicMock())
    assert rev_calls and rev_calls[0][1] == "t-rev"                # live rollback attempted
    tool_names = [c[0] for c in tools]
    assert "aimeat_task_fail" in tool_names and "aimeat_task_complete" not in tool_names


def test_gate_fail_without_auto_revert_does_not_touch_the_live_app(monkeypatch):
    at._VERIFY_VERDICTS["t-norev"] = {"verify_render": {"ok": False}}
    at.reset_published_baselines("t-norev")
    at._record_publish_baseline("t-norev", "broken.html", 7)
    tools = _capture_calls(monkeypatch)
    rev_calls = []
    monkeypatch.setattr(
        at, "revert_apps_to_baseline",
        lambda agent, tid, owner=None: rev_calls.append((agent, tid, owner)) or [],
    )
    # auto_revert defaults False -> the gate fails the task, but the live app is NOT reverted
    ac._make_complete_cb("agent-x", "t-norev", require_verify=True, owner="owner")(MagicMock())
    assert rev_calls == []                                         # no live rollback
    tool_names = [c[0] for c in tools]
    assert "aimeat_task_fail" in tool_names and "aimeat_task_complete" not in tool_names

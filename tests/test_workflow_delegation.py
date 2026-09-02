"""Who a coordinator is allowed to delegate to — the roster and the mode gate.

Both rules were written after watching a live two-owner node on 2026-09-02, and both failed
SILENTLY there: the run reported success while having asked nobody. Deterministic — every node call
is faked, no LLM, no network.
"""

from __future__ import annotations

import pytest

from crewaimeat import workflow


def _rows(*specs: tuple[str, str]) -> list[dict]:
    """`aimeat_agents_list` rows as the node returns them: name + gaii + mode."""
    return [{"name": n, "gaii": f"{n}#alice@node-a", "mode": m} for n, m in specs]


@pytest.fixture
def node(monkeypatch):
    """Fake the node: agents_list answers from `state`, everything else answers empty."""
    state: dict = {"rows": [], "calls": []}

    def _call(agent_name, tool, payload=None):
        state["calls"].append((agent_name, tool, payload))
        if tool == "aimeat_agents_list":
            return {"agents": state["rows"]}
        if tool == "aimeat_task_create":
            return {"id": "11111111-2222-3333-4444-555555555555"}
        return {}

    monkeypatch.setattr(workflow, "_aimeat_call", _call)
    monkeypatch.setattr(workflow, "_crew_roster", lambda: [])  # no local checkout in the way
    monkeypatch.setattr(workflow, "_reputation", lambda *a, **k: ("", None, None))
    monkeypatch.setattr(workflow, "_read_explore_seq", lambda _n: 0)
    monkeypatch.setattr(workflow, "_write_explore_seq", lambda _n, _s: None)
    return state


def _tool(tools, name):
    return next(t for t in tools if getattr(t, "name", "") == name)


def test_discovery_lists_the_nodes_agents_not_the_local_checkout(node, monkeypatch):
    """A crew FILE is not an agent. The node says who exists for THIS owner.

    Live 2026-09-02: isoalice's workflow-manager was offered 49 peers read out of the crewaimeat
    checkout, none of which existed on its node, while the peers that did exist for that owner were
    invisible — they are defined on the node and have no local file at all.
    """
    node["rows"] = _rows(("crew-forge", "task-runner"), ("librarian-that-only-exists-here", "task-runner"))
    monkeypatch.setattr(
        workflow, "_crew_roster", lambda: [{"agent": "news-fetcher", "summary": "a crew file in this repo"}]
    )
    tools = workflow.make_workflow_tools(coordinator_name="workflow-manager#alice@node-a", run_id="r1")
    out = _tool(tools, "discover_crews").run()
    assert "crew-forge" in out
    assert "librarian-that-only-exists-here" in out
    assert "news-fetcher" not in out  # a repo crew the node has never heard of is not a peer


def test_discovery_hides_peers_whose_owner_starts_their_work_by_hand(node):
    """Only a task-runner auto-activates a queued task; anything else waits for its OWNER."""
    node["rows"] = _rows(("crew-forge", "task-runner"), ("concierge", "interactive"))
    tools = workflow.make_workflow_tools(coordinator_name="workflow-manager#alice@node-a", run_id="r1")
    out = _tool(tools, "discover_crews").run()
    assert "- crew-forge:" in out
    assert "- concierge:" not in out
    assert "concierge" in out  # named as existing-but-not-delegable, so the coordinator can say why


def test_delegating_to_a_hand_started_peer_is_refused_not_waited_out(node):
    """Live, this burned the coordinator's whole 1800 s timeout on a task nobody could start."""
    node["rows"] = _rows(("concierge", "interactive"))
    tools = workflow.make_workflow_tools(coordinator_name="workflow-manager#alice@node-a", run_id="r1")
    _tool(tools, "discover_crews").run()  # loads the roster the gate reads
    out = _tool(tools, "delegate_subtask").run(target_agent="concierge", title="Step 1", instruction="list agents")
    assert "Refused" in out and "interactive" in out
    assert not any(c[1] == "aimeat_task_create" for c in node["calls"])  # nothing was queued


def test_an_unknown_mode_is_still_offered(node):
    """The node adds fields; refusing on a value we cannot read would hide a working peer."""
    node["rows"] = [{"name": "crew-forge", "gaii": "crew-forge#alice@node-a"}]  # no mode at all
    tools = workflow.make_workflow_tools(coordinator_name="workflow-manager#alice@node-a", run_id="r1")
    assert "- crew-forge:" in _tool(tools, "discover_crews").run()


def test_self_is_blocked_by_both_spellings(node):
    """`coordinator_name` is a GAII in a multi-owner home; peers are named by their local name."""
    node["rows"] = _rows(("workflow-manager", "task-runner"), ("crew-forge", "task-runner"))
    tools = workflow.make_workflow_tools(coordinator_name="workflow-manager#alice@node-a", run_id="r1")
    out = _tool(tools, "discover_crews").run()
    assert "- workflow-manager:" not in out
    assert "- crew-forge:" in out


def test_an_unreachable_node_falls_back_to_the_checkout_and_says_so(node, monkeypatch):
    """Delegating to nobody is worse than delegating to an unverified name — but say which list it is."""
    node["rows"] = []
    monkeypatch.setattr(workflow, "_crew_roster", lambda: [{"agent": "news-fetcher", "summary": "local crew"}])
    tools = workflow.make_workflow_tools(coordinator_name="workflow-manager#alice@node-a", run_id="r1")
    out = _tool(tools, "discover_crews").run()
    assert "news-fetcher" in out
    assert "unverified" in out


def test_a_rerun_does_not_return_the_previous_attempts_answer(node, monkeypatch):
    """The wait must see a CHANGE, not merely a value.

    On the node-backed path `run_id` is the coordinator's TASK id, so a task run a second time — a
    retry, or a worker the spawner reaped — rebuilds the very same shared-tag key. Measured
    2026-09-02: `delegate_and_wait` answered in 0 s from a 37-minute-old write and left the subtask it
    had just queued running with nobody reading it.
    """
    node["rows"] = _rows(("crew-forge", "task-runner"))
    written: dict[str, str] = {}

    def _call(agent_name, tool, payload=None):
        node["calls"].append((agent_name, tool, payload))
        if tool == "aimeat_agents_list":
            return {"agents": node["rows"]}
        if tool == "aimeat_task_create":
            return {"id": "11111111-2222-3333-4444-555555555555"}
        if tool == "aimeat_memory_read":
            return {"value": written.get(payload["key"])}
        if tool == "aimeat_memory_list":
            return {"items": [{"key": k, "value": v} for k, v in written.items()]}
        return {}

    monkeypatch.setattr(workflow, "_aimeat_call", _call)
    monkeypatch.setattr(workflow, "POLL_SECONDS", 1)  # NOT 0 — `waited += POLL_SECONDS` would never reach the timeout

    tools = workflow.make_workflow_tools(coordinator_name="workflow-manager#alice@node-a", run_id="task-1", timeout=1)
    key = "agents.tag.workflow.task-1.crew-forge.1"
    written[key] = "the FIRST attempt's answer"

    out = _tool(tools, "delegate_and_wait").run(target_agent="crew-forge", title="Step 1", instruction="list agents")
    assert "the FIRST attempt's answer" not in out  # it waited, then timed out, rather than lying

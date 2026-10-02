"""The identity push writes the agent's tags only when the node does not already hold them.

`run_crew` sets the agent's tags on every start (aimeat_agent_tags_set). On a node older than
aimeat-protocol bcd4027ed that call needs agent:write for the agent's OWN record too, the basic agents
(concierge, workflow-manager) hold no agent:write on purpose, and since aimeat-crewai 0.31.0 one refused
call fails the run -- so on such a node a basic agent could finish no task (measured on a hosted place,
2026-10-02). The button that makes those agents seeds the same tags the definition declares, so on those
nodes the skip is what lets the run finish. On a node at bcd4027ed an agent sets its own tags with no
permission word, and the skip merely saves a write.

These tests hold the decision at its seam: the node is replaced by a recorder of the calls made.
"""

from __future__ import annotations

TAGS = ["crew:briefing", "role:writer"]


_TAKEN = {"ok": True}


class _Recorder:
    def __init__(self, rows: list | None, *, tags_set_answer: dict | None = _TAKEN):
        self.rows = rows
        self.tags_set_answer = tags_set_answer  # None = the node refused the write
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, agent_name, tool, payload, **_kw):
        self.calls.append((tool, payload))
        if tool == "aimeat_agents_list":
            return None if self.rows is None else {"agents": self.rows}
        if tool == "aimeat_agent_tags_set":
            return self.tags_set_answer
        return {"ok": True}

    def tools(self) -> list[str]:
        return [t for t, _ in self.calls]


def _row(name: str, tags: list[str]) -> dict:
    return {"name": name, "gaii": f"{name}#owner@node", "mode": "task-runner", "tags": tags}


def test_tags_the_node_already_holds_are_not_written():
    from crewaimeat.aimeat_crew import _set_tags_if_changed

    call = _Recorder([_row("other", ["x"]), _row("briefer", ["role:writer", "crew:briefing"])])  # order differs
    assert _set_tags_if_changed("briefer", TAGS, call=call) == "unchanged"
    assert call.tools() == ["aimeat_agents_list"], "no write for tags the node already holds"


def test_tags_that_differ_are_written():
    from crewaimeat.aimeat_crew import _set_tags_if_changed

    call = _Recorder([_row("briefer", ["crew:briefing"])])
    assert _set_tags_if_changed("briefer", TAGS, call=call) == "written"
    assert call.tools() == ["aimeat_agents_list", "aimeat_agent_tags_set"]
    assert call.calls[1][1] == {"target_agent_name": "briefer", "tags": TAGS}


def test_an_agent_the_node_does_not_list_gets_its_tags_written():
    from crewaimeat.aimeat_crew import _set_tags_if_changed

    call = _Recorder([_row("other", TAGS)])  # the same tags, on somebody else's row
    assert _set_tags_if_changed("briefer", TAGS, call=call) == "written"
    assert "aimeat_agent_tags_set" in call.tools()


def test_a_list_the_node_would_not_answer_means_write():
    # Not knowing is not "unchanged": the write goes out, and a node that refuses it says so.
    from crewaimeat.aimeat_crew import _set_tags_if_changed

    call = _Recorder(None)
    assert _set_tags_if_changed("briefer", TAGS, call=call) == "written"
    assert call.tools() == ["aimeat_agents_list", "aimeat_agent_tags_set"]


def test_a_refused_write_is_reported_as_failed_not_hidden():
    from crewaimeat.aimeat_crew import _set_tags_if_changed

    call = _Recorder([_row("briefer", [])], tags_set_answer=None)
    assert _set_tags_if_changed("briefer", TAGS, call=call) == "failed"


def test_the_agents_own_row_is_found_under_a_full_identity():
    # A worker may know itself by its GAII; the node's row carries the bare name.
    from crewaimeat.aimeat_crew import _set_tags_if_changed

    call = _Recorder([_row("briefer", TAGS)])
    assert _set_tags_if_changed("briefer#owner@node", TAGS, call=call) == "unchanged"


def test_run_crew_pushes_tags_through_the_skip():
    # The start-up path in run_crew calls the one function above, not aimeat_agent_tags_set directly.
    import ast
    import inspect

    from crewaimeat import aimeat_crew

    src = inspect.getsource(aimeat_crew.run_crew)
    tree = ast.parse(src)
    direct = [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and n.value == "aimeat_agent_tags_set"]
    assert not direct, "run_crew must set tags through _set_tags_if_changed, which reads the node first"
    assert "_set_tags_if_changed(" in src

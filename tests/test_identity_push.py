"""The identity push writes the agent's tags only when the node does not already hold them.

`run_crew` sets the agent's tags on every start (aimeat_agent_tags_set). On a node older than
aimeat-protocol bcd4027ed that call needs agent:write for the agent's OWN record too, the basic agents
(concierge, workflow-manager) hold no agent:write on purpose, and since aimeat-crewai 0.31.0 one refused
call fails the run -- so on such a node a basic agent could finish no task (measured on a hosted place,
2026-10-02). The button that makes those agents seeds the same tags the definition declares, so on those
nodes the skip is what lets the run finish. On a node at bcd4027ed an agent sets its own tags with no
permission word, and the skip merely saves a write. The read is the agent's own PUBLIC profile, never
the agent list, which asks agent:read from node 4.0.0 (2026-11-01).

These tests hold the decision at its seam: the node is replaced by a recorder of the calls made.
"""

from __future__ import annotations

TAGS = ["crew:briefing", "role:writer"]
ME = "briefer#owner@node"


_TAKEN = {"ok": True}


class _Recorder:
    """The node, as the calls a start makes. `profiles` maps a GAII to the tags its public profile
    shows; a GAII it does not hold answers None, as the connector does for a 404."""

    def __init__(self, profiles: dict | None, *, tags_set_answer: dict | None = _TAKEN):
        self.profiles = profiles or {}
        self.tags_set_answer = tags_set_answer  # None = the node refused the write
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, agent_name, tool, payload, **_kw):
        self.calls.append((tool, payload))
        if tool == "aimeat_agents_list":
            raise AssertionError("a start must not read the agent list: it asks agent:read from node 4.0.0")
        if tool == "aimeat_agent_profile":
            gaii = payload["gaii"]
            return {"gaii": gaii, "tags": self.profiles[gaii]} if gaii in self.profiles else None
        if tool == "aimeat_agent_tags_set":
            return self.tags_set_answer
        return {"ok": True}

    def tools(self) -> list[str]:
        return [t for t, _ in self.calls]


def test_tags_the_node_already_holds_are_not_written():
    from crewaimeat.aimeat_crew import _set_tags_if_changed

    call = _Recorder({ME: ["role:writer", "crew:briefing"]})  # order differs
    assert _set_tags_if_changed(ME, TAGS, call=call) == "unchanged"
    assert call.tools() == ["aimeat_agent_profile"], "no write for tags the node already holds"
    assert call.calls[0][1] == {"gaii": ME}


def test_tags_that_differ_are_written():
    from crewaimeat.aimeat_crew import _set_tags_if_changed

    call = _Recorder({ME: ["crew:briefing"]})
    assert _set_tags_if_changed(ME, TAGS, call=call) == "written"
    assert call.tools() == ["aimeat_agent_profile", "aimeat_agent_tags_set"]
    assert call.calls[1][1] == {"target_agent_name": ME, "tags": TAGS}


def test_a_profile_the_node_would_not_answer_means_write():
    # Not knowing is not "unchanged": the write goes out, and a node that refuses it says so.
    from crewaimeat.aimeat_crew import _set_tags_if_changed

    call = _Recorder(None)
    assert _set_tags_if_changed(ME, TAGS, call=call) == "written"
    assert call.tools() == ["aimeat_agent_profile", "aimeat_agent_tags_set"]


def test_a_profile_of_somebody_else_is_not_read_as_ours():
    from crewaimeat.aimeat_crew import _node_tags

    def call(agent_name, tool, payload, **_kw):
        return {"gaii": "other#owner@node", "tags": TAGS}

    assert _node_tags(ME, call=call) is None


def test_a_refused_write_is_reported_as_failed_not_hidden():
    from crewaimeat.aimeat_crew import _set_tags_if_changed

    call = _Recorder({ME: []}, tags_set_answer=None)
    assert _set_tags_if_changed(ME, TAGS, call=call) == "failed"


def _serve_json(rows: list[dict]) -> None:
    import json

    from crewaimeat import spawn_state

    home = spawn_state.aimeat_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "serve.json").write_text(json.dumps({"agents": rows}), encoding="utf-8")


def test_a_crew_file_agent_is_asked_by_its_line_in_serve_json():
    # A crew-file agent runs under its bare name; its identity is its one line in this home.
    from crewaimeat.aimeat_crew import _set_tags_if_changed

    _serve_json([{"agent": "other", "gaii": "other#owner@node"}, {"agent": "briefer", "gaii": ME}])
    call = _Recorder({ME: TAGS})
    assert _set_tags_if_changed("briefer", TAGS, call=call) == "unchanged"
    assert call.calls[0][1] == {"gaii": ME}


def test_a_name_under_two_owners_is_not_guessed():
    from crewaimeat.aimeat_crew import _own_gaii

    _serve_json([{"agent": "briefer", "gaii": ME}, {"agent": "briefer", "gaii": "briefer#second@node"}])
    assert _own_gaii("briefer") is None
    _serve_json([])
    assert _own_gaii("briefer") is None


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

"""The daemon is started with what this runtime can do (`AIMEAT_RUNTIME_FEATURES`).

The connector sends it to the node for the computer, so the node's new-agent page can offer "when
something new arrives in a workspace" on a computer that carries no agent yet to ask. It is the list
`crew.menu` answers from: a computer and its agents must not say different things.
"""

from __future__ import annotations

import os

from crewaimeat.runtime_features import FEATURES, FEATURES_ENV


def test_the_daemon_s_environment_names_the_features_crew_menu_answers(tmp_path, monkeypatch):
    import crewaimeat.serve_guard as sg
    from crewaimeat import crew_invoke as ci

    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path / "place" / ".aimeat"))
    monkeypatch.delenv(FEATURES_ENV, raising=False)
    monkeypatch.setattr("crewaimeat.llm_choice.default_is_node_road", lambda agent_name: False)  # a node read

    sg._name_this_install()

    _ok, menu = ci.handle("crew.menu", {}, agent_name="node-agent")
    assert os.environ[FEATURES_ENV] == "record_spaces,record_backfill,task_scope"
    assert os.environ[FEATURES_ENV].split(",") == menu["features"] == list(FEATURES)


def test_a_value_somebody_set_is_left_alone(tmp_path, monkeypatch):
    """A host that runs another runtime beside this connector says what that one can do."""
    import crewaimeat.serve_guard as sg

    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path / "place" / ".aimeat"))
    monkeypatch.setenv(FEATURES_ENV, "")
    sg._name_this_install()
    assert os.environ[FEATURES_ENV] == ""


def test_the_list_costs_serve_guard_no_import():
    """serve_guard starts the daemon for a manager that must not import crewai."""
    import ast
    from pathlib import Path

    import crewaimeat.runtime_features as rf

    tree = ast.parse(Path(rf.__file__).read_text(encoding="utf-8"))
    imported = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert [getattr(n, "module", None) for n in imported] == ["__future__"]

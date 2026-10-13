"""The daemon is started with what this runtime can do (`AIMEAT_RUNTIME_FEATURES`).

The connector sends it to the node for the computer, so the node's new-agent page can offer "when
something new arrives in a workspace" on a computer that carries no agent yet to ask. It is the list
`crew.menu` answers from: a computer and its agents must not say different things.
"""

from __future__ import annotations

import os

from crewaimeat import runtime_features
from crewaimeat.runtime_features import FEATURES, FEATURES_ENV


def test_the_daemon_s_environment_names_the_features_crew_menu_answers(tmp_path, monkeypatch):
    import crewaimeat.serve_guard as sg
    from crewaimeat import crew_invoke as ci

    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path / "place" / ".aimeat"))
    monkeypatch.delenv(FEATURES_ENV, raising=False)
    monkeypatch.setattr("crewaimeat.llm_choice.default_is_node_road", lambda agent_name: False)  # a node read
    monkeypatch.setattr(runtime_features, "browser_installed", lambda: False)

    sg._name_this_install()

    _ok, menu = ci.handle("crew.menu", {}, agent_name="node-agent")
    assert os.environ[FEATURES_ENV] == "record_spaces,record_backfill,task_scope"
    assert os.environ[FEATURES_ENV].split(",") == menu["features"] == list(FEATURES)


def test_a_computer_with_a_browser_says_so_and_one_without_does_not(tmp_path, monkeypatch):
    """The browser needs a package and a Chromium the core install does not bring. A computer without
    them must not announce it, and must not offer the tool: the agent could never do its work."""
    import crewaimeat.serve_guard as sg
    from crewaimeat import crew_invoke as ci

    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path / "place" / ".aimeat"))
    monkeypatch.setattr("crewaimeat.llm_choice.default_is_node_road", lambda agent_name: False)  # a node read
    seen = {}
    for installed in (True, False):
        monkeypatch.setattr(runtime_features, "browser_installed", lambda installed=installed: installed)
        monkeypatch.delenv(FEATURES_ENV, raising=False)
        sg._name_this_install()
        _ok, menu = ci.handle("crew.menu", {}, agent_name="node-agent")
        seen[installed] = (
            os.environ[FEATURES_ENV].split(","),
            "browser" in {t["id"] for t in menu["tools"]},
            menu["features"],
        )

    assert seen[True] == ([*FEATURES, "browser_domains"], True, [*FEATURES, "browser_domains"])
    assert seen[False] == (list(FEATURES), False, list(FEATURES))


def test_a_browser_is_found_on_the_disk_without_starting_anything(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "browsers"))
    assert runtime_features.browser_installed() is False, "no folder, no browser"
    (tmp_path / "browsers" / "ffmpeg-1011").mkdir(parents=True)
    assert runtime_features.browser_installed() is False, "a folder with no Chromium in it"
    (tmp_path / "browsers" / "chromium_headless_shell-1208").mkdir()
    assert runtime_features.browser_installed() is True
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    assert runtime_features.browser_installed() is False, "a Chromium and no playwright package"


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
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            names.add((node.module or "").split(".")[0])
    assert names <= {"__future__", "importlib", "os", "sys", "pathlib"}, "only the standard library, and little of it"

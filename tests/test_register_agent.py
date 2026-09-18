"""forge.register_agent — the device-auth command + output parsing.

Regression for the v1.33 connector break: the old `connect add … --mode task-runner` subcommand was
removed, so device-auth never issued a code and the agent could not connect. This pins the CURRENT
command form (`connect --url --owner --agent`) and that a realistic device-auth output is parsed into a
code + verify URL.
"""

from __future__ import annotations

import re


def test_register_agent_uses_current_connect_command_and_parses_code(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    import crewaimeat.forge as forge

    captured: dict = {}

    class FakeProc:
        def poll(self):
            return None  # "still running" — the loop breaks on (code AND url), not on exit

    def fake_popen(cmd, **kw):
        captured["cmd"] = cmd
        out = kw.get("stdout")
        if out is not None:  # mimic the connector printing its device-auth instructions
            out.write(
                b"AIMEAT Agent Connector\nRequesting device authorization...\n"
                b"Verification code: ABCD-1234\nVisit https://aimeat.io/verify to approve.\n"
            )
            out.flush()
        return FakeProc()

    monkeypatch.setattr(forge.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(forge.time, "sleep", lambda *_a, **_k: None)  # don't actually wait

    ok, msg = forge.register_agent("Mapmaker", "happydude500001", "https://aimeat.io")

    cmd = captured["cmd"]
    assert "add" not in cmd, "the removed 'connect add' subcommand must not come back"
    # `--mode` is back in connector 3.x and the owner approves it in the same consent: registration
    # asks for the mode the crew expects (task-runner unless its file declares MODE).
    assert cmd[cmd.index("--mode") + 1] == "task-runner", cmd
    assert "connect" in cmd
    # The pin is read from the ONE constant, never restated here — a hardcoded version made this test
    # go red every time the pin moved, which is drift-generating noise rather than a contract. What IS
    # a contract: the command carries a PINNED version and never `@latest` (an unpinned connector can
    # silently drop the provenance block; see the aimeat-crewai floor note in pyproject.toml).
    assert forge.AIMEAT_CONNECTOR in cmd, f"the pinned connector {forge.AIMEAT_CONNECTOR} is not in {cmd}"
    assert "aimeat@latest" not in cmd, "the connector version is pinned, not @latest"
    assert re.fullmatch(r"aimeat@\d+\.\d+\.\d+", forge.AIMEAT_CONNECTOR), (
        f"AIMEAT_CONNECTOR must be an exact pin, got {forge.AIMEAT_CONNECTOR!r}"
    )
    for need in ("--url", "https://aimeat.io", "--owner", "happydude500001", "--agent", "Mapmaker"):
        assert need in cmd, f"missing {need} in {cmd}"
    assert ok and "ABCD-1234" in msg and "verify" in msg  # the output was parsed into a code + verify URL


def test_register_agent_surfaces_raw_output_when_no_code(tmp_path, monkeypatch):
    """When device-auth fails (no code), the real connector output is surfaced — not a guessed
    'already registered' that hides why nothing reached the node."""
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    import crewaimeat.forge as forge

    class FakeProc:
        def poll(self):
            return 1  # exited (failed) without printing a code

    def fake_popen(cmd, **kw):
        out = kw.get("stdout")
        if out is not None:
            out.write(b"AIMEAT Agent Connector\nRequesting device authorization...\nAuthorization request failed.\n")
            out.flush()
        return FakeProc()

    monkeypatch.setattr(forge.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(forge.time, "sleep", lambda *_a, **_k: None)

    ok, msg = forge.register_agent("Mapmaker", "happydude500001", "https://aimeat.io")
    assert ok is False
    assert "Authorization request failed" in msg  # the real reason, not "already registered"
    assert "already registered" not in msg


def _fake_register_popen(monkeypatch, captured):
    import crewaimeat.forge as forge

    class FakeProc:
        def poll(self):
            return None

    def fake_popen(cmd, **kw):
        captured["cmd"] = cmd
        out = kw.get("stdout")
        if out is not None:
            out.write(b"Verification code: ABCD-1234\nVisit https://aimeat.io/verify to approve.\n")
            out.flush()
        return FakeProc()

    monkeypatch.setattr(forge.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(forge.time, "sleep", lambda *_a, **_k: None)


def test_register_agent_asks_for_the_mode_the_crew_declares(monkeypatch):
    """A crew that declares MODE = "interactive" is registered asking for interactive, not the default."""
    import crewaimeat.forge as forge
    from crewaimeat import agent_manifest

    captured: dict = {}
    _fake_register_popen(monkeypatch, captured)
    monkeypatch.setattr(agent_manifest, "expected_mode", lambda agent, root=None: "interactive")
    ok, _msg = forge.register_agent("desk-helper", "happydude500001", "https://aimeat.io")
    cmd = captured["cmd"]
    assert ok and cmd[cmd.index("--mode") + 1] == "interactive"


def test_register_agent_refuses_a_mode_the_node_does_not_have(monkeypatch):
    """An explicit mode is an argv element; anything but the node's five is refused before any spawn."""
    import crewaimeat.forge as forge

    captured: dict = {}
    _fake_register_popen(monkeypatch, captured)
    ok, msg = forge.register_agent("desk-helper", "happydude500001", "https://aimeat.io", mode="task-runner & x")
    assert not ok and "unknown agent mode" in msg and "cmd" not in captured


def test_expected_mode_reads_the_crew_file(tmp_path):
    """MODE in the crew file is the one source: registration and the runtime both read it."""
    from crewaimeat import agent_manifest

    crews = tmp_path / "crews"
    crews.mkdir()
    (crews / "desk_crew.py").write_text(
        'AGENT_NAME = "desk"\nMODE = "interactive"\n\ndef build_domain(ctx):\n    return [], []\n', encoding="utf-8"
    )
    (crews / "plain_crew.py").write_text(
        'AGENT_NAME = "plain"\n\ndef build_domain(ctx):\n    return [], []\n', encoding="utf-8"
    )
    (crews / "typo_crew.py").write_text(
        'AGENT_NAME = "typo"\nMODE = "task_runner"\n\ndef build_domain(ctx):\n    return [], []\n', encoding="utf-8"
    )
    agent_manifest._load.cache_clear() if hasattr(agent_manifest._load, "cache_clear") else None
    assert agent_manifest.expected_mode("desk", tmp_path) == "interactive"
    assert agent_manifest.expected_mode("desk#owner@node", tmp_path) == "interactive"  # a GAII finds it too
    assert agent_manifest.expected_mode("plain", tmp_path) == "task-runner"
    assert agent_manifest.expected_mode("typo", tmp_path) == "task-runner"  # an unknown value is not a mode
    assert agent_manifest.expected_mode("not-here", tmp_path) == "task-runner"


def test_every_live_crew_here_expects_task_runner():
    """dm_serviceable and self_monitor crews are task-runners too (owner, 2026-07-26). If a crew ever
    declares another mode on purpose, list it here with the reason."""
    from crewaimeat import agent_manifest

    modes = {m.agent: m.expected_mode for m in agent_manifest.all_manifests(refresh=True) if m.live and m.agent}
    assert modes and set(modes.values()) == {"task-runner"}, {a: m for a, m in modes.items() if m != "task-runner"}

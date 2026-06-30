"""forge.register_agent — the device-auth command + output parsing.

Regression for the v1.33 connector break: the old `connect add … --mode task-runner` subcommand was
removed, so device-auth never issued a code and the agent could not connect. This pins the CURRENT
command form (`connect --url --owner --agent`) and that a realistic device-auth output is parsed into a
code + verify URL.
"""

from __future__ import annotations


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
    assert "--mode" not in cmd and "task-runner" not in cmd, "the removed --mode flag must not come back"
    assert "connect" in cmd
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

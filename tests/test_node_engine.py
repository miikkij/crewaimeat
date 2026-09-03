"""node_engine: which connector CLI the fleet actually starts.

Deterministic — no process is spawned, only the command that WOULD be spawned is inspected.
"""

from __future__ import annotations


def test_the_connector_cli_can_be_pointed_somewhere_else(monkeypatch, tmp_path):
    """`AIMEAT_CLI` exists because the PUBLISHED connector can lag the node.

    Measured 2026-09-03 on the developer's own machine: agent v2 has the daemon generate an Ed25519
    key per agent, and that code (`connect/agent-key.js`, `connect/enrolment.js`) shipped in a local
    build only — neither the installed 3.10.0 nor the published 3.11.0 contained either file, though
    the local build calls itself 3.11.0 as well. Without an override there was no way to point a
    fleet at a connector that could do what the node was offering.
    """
    from crewaimeat.node_engine import aimeat_cli, serve_command

    monkeypatch.delenv("AIMEAT_CLI", raising=False)
    default = aimeat_cli()

    js = tmp_path / "aimeat.js"
    js.write_text("", encoding="utf-8")
    monkeypatch.setenv("AIMEAT_CLI", str(js))
    assert aimeat_cli() == str(js)
    cmd = serve_command()
    # A .js entry point is run with node — `cmd /c` would hand it to the shell's file association.
    assert isinstance(cmd, list) and cmd[1] == str(js) and "node" in cmd[0].lower()

    monkeypatch.delenv("AIMEAT_CLI")
    assert aimeat_cli() == default  # nothing changes for anyone who does not set it

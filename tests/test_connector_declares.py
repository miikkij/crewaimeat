"""What this runtime tells the node through the connector it starts: the computer's name, and which
run modes the computer can keep.

Both ride the environment the daemon is started with (`AIMEAT_INSTALL_NAME`, `AIMEAT_RUN_MODES`,
aimeat-protocol 9353ccb7e). The NAME is decided where every daemon start ends (`serve_guard`), so it
does not change with whoever restarted the daemon last. `resident` is a PROMISE that a fleet host
runs beside the daemon, so it is made only by the entrypoint that starts one — and that pairing is
what the last tests here hold in place, because setting it anywhere else makes the node accept an
always-on agent that nobody runs.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_the_default_name_tells_two_homes_on_one_machine_apart(tmp_path, monkeypatch):
    """The host name alone is one name for this checkout's fleet, a dev clone and an appliance."""
    import crewaimeat.serve_guard as sg

    monkeypatch.setattr("socket.gethostname", lambda: "kotikone")
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path / "crewfive" / ".aimeat"))
    first = sg.default_install_name()
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path / "crewfive-dev" / ".aimeat"))
    assert (first, sg.default_install_name()) == ("kotikone (crewfive)", "kotikone (crewfive-dev)")


def test_a_name_somebody_chose_is_left_alone(tmp_path, monkeypatch):
    """A hosted place sets the customer's own name; the default must never overwrite it."""
    import crewaimeat.serve_guard as sg

    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path / "place" / ".aimeat"))
    monkeypatch.setenv("AIMEAT_INSTALL_NAME", "Office server")
    sg._name_this_install()
    assert os.environ["AIMEAT_INSTALL_NAME"] == "Office server"


def _daemon_start_sees(monkeypatch, tmp_path, start: str) -> dict:
    """Run one of serve_guard's two daemon starts with the spawn replaced, and return the
    environment the daemon would have inherited."""
    import aimeat_crewai

    import crewaimeat.node_engine as node_engine
    import crewaimeat.serve_guard as sg

    seen: dict = {}

    def fake_ensure_serve(**_kw):
        seen.update({k: os.environ.get(k) for k in ("AIMEAT_INSTALL_NAME", "AIMEAT_RUN_MODES")})
        return {"pid": 1, "port": 1}

    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path / "fleet" / ".aimeat"))
    monkeypatch.delenv("AIMEAT_INSTALL_NAME", raising=False)
    monkeypatch.delenv("AIMEAT_RUN_MODES", raising=False)
    monkeypatch.setattr("socket.gethostname", lambda: "kotikone")
    monkeypatch.setattr(aimeat_crewai, "ensure_serve", fake_ensure_serve)
    monkeypatch.setattr(node_engine, "serve_command", lambda: "aimeat")
    monkeypatch.setattr(sg, "_guard_pytest", lambda: None)
    monkeypatch.setattr(sg, "_LOCK", tmp_path / "serve-spawn.lock")
    monkeypatch.setattr(sg, "_record_daemon", lambda pid: None)
    monkeypatch.setattr(sg, "_reap_duplicates", lambda keep: 0)
    monkeypatch.setattr(sg, "_assert_serve_json_owner", lambda doc: True)
    monkeypatch.setattr(sg, "this_home_serve_pids", lambda: [])
    getattr(sg, start)()
    return seen


def test_every_daemon_start_names_the_computer_and_promises_nothing(monkeypatch, tmp_path):
    """Both starts — the first, and the reload a new agent's attach needs — and neither says
    `resident`: an appliance and a lone crew reach the daemon through the same two functions, and
    nothing there keeps an agent always-on."""
    for start in ("ensure_single_serve", "restart_serve"):
        seen = _daemon_start_sees(monkeypatch, tmp_path, start)
        assert seen == {"AIMEAT_INSTALL_NAME": "kotikone (fleet)", "AIMEAT_RUN_MODES": None}, start


def _sets_run_modes(text: str) -> bool:
    return bool(re.search(r"AIMEAT_RUN_MODES\W{0,6}=\W*[\"']?spawn,resident", text))


def test_only_a_script_that_starts_the_fleet_host_promises_always_on():
    """THE ORDER RULE OF THE WISH, HELD BY THE SUITE. With `resident` declared and no host, the node
    accepts an always-on agent for this computer and nobody runs it (measured on a hosted place
    2026-10-02: the task stayed `active` for 12 minutes). So wherever the promise is written, the
    host must be started in the same file."""
    promising = []
    for path in [*(ROOT / "scripts").glob("*"), *(ROOT / "src" / "crewaimeat").rglob("*.py")]:
        if not path.is_file() or path.suffix not in {".ps1", ".sh", ".py"}:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if _sets_run_modes(text):
            promising.append(path.name)
            assert "crewaimeat.fleet_host" in text, f"{path.name} promises resident agents and starts no fleet host"
    assert sorted(promising) == ["start_fleet.ps1", "start_fleet.sh"]


def test_the_host_keeps_the_promise_the_start_script_makes(monkeypatch):
    """The other half of the pairing: the value the scripts export is one the host reads as "stay"."""
    from crewaimeat import fleet_host

    exported = re.search(r'AIMEAT_RUN_MODES:="([^"]+)"', (ROOT / "scripts" / "start_fleet.sh").read_text("utf-8"))
    assert exported, "start_fleet.sh no longer exports a default AIMEAT_RUN_MODES"
    monkeypatch.setenv("AIMEAT_RUN_MODES", exported.group(1))
    assert fleet_host.declares_resident() is True


def test_a_spawner_with_nobody_to_serve_waits_instead_of_exiting(monkeypatch, tmp_path):
    """Every newly added computer starts with no spawn agent. Measured 2026-10-11: the spawner exited 1
    with "add RUN_MODE to a crew file" and the watchdog restarted it every 20 s."""
    from crewaimeat import env_guard, spawner

    served: list = []
    monkeypatch.setattr(env_guard, "load_env", lambda *a, **k: None)
    monkeypatch.setattr(spawner, "select_agents", lambda root, wanted=None: [])
    monkeypatch.setattr(spawner, "_acquire_singleton", lambda: object())
    monkeypatch.setattr(spawner.Spawner, "serve_forever", lambda self: served.append(self.agents) or 0)
    assert spawner.main(["--root", str(tmp_path)]) == 0 and served == [[]]

    # Naming agents that are not this connector's spawn agents is still refused, not waited on.
    assert spawner.main(["--root", str(tmp_path), "--agents", "not-mine"]) == 1 and len(served) == 1

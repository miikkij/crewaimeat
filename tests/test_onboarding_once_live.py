"""Integration: a fresh agent's Hello Integration proposes its test-task plan ONCE, against a REAL node.

Sold place, 2026-10-02: the deterministic driver repeated accept_test_task -> aimeat_task_propose_todos
thirteen times in twelve seconds, each "ok", before the run went on. Here the whole real chain runs --
the node from an aimeat-protocol checkout, the real `aimeat connect serve --http` daemon, the liaison's
MCP tools over it, and the scaffold's own `_run_onboarding_only` -- and the log is counted.

Skips where the concierge live test skips (no node binary, no aimeat-protocol checkout with
`pnpm install` done). The liaison's model is built but never called; a provider key still has to exist
for the build, so the repo's .env is loaded.
"""

from __future__ import annotations

import os
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest
import requests

from tests.test_concierge_propose_live import AIMEAT_DIR, _free_port, _kill, _token

AGENT = "onboardee"

pytestmark = [
    pytest.mark.skipif(
        AIMEAT_DIR is None, reason="needs an aimeat-protocol checkout with `pnpm install` done (AIMEAT_PROTOCOL_DIR)"
    ),
    pytest.mark.loopback,
]


@pytest.fixture(scope="module")
def place(tmp_path_factory):
    """A node, its daemon, and one freshly created agent that has not onboarded."""
    from crewaimeat.env_guard import load_env

    load_env(Path(__file__).resolve().parents[1] / ".env", quiet=True)
    tmp = tmp_path_factory.mktemp("onboarding-live")
    home = tmp / "home"
    home.mkdir()
    port = _free_port()
    base = f"http://localhost:{port}"
    env = {
        **os.environ,
        "AIMEAT_PORT": str(port),
        "AIMEAT_BASE_URL": base,
        "AIMEAT_CONNECT_TUNNEL_ENABLED": "true",
        "AIMEAT_RL_GLOBAL": "100000",
        "AIMEAT_RL_AUTH": "10000",
        "AIMEAT_RL_WORK": "10000",
        "AIMEAT_RL_MEMORY": "10000",
    }
    env.pop("AIMEAT_DEFAULT_AGENT_SCOPES", None)
    node = subprocess.Popen(
        [
            "node",
            "--import",
            "tsx",
            "src/index.ts",
            "start",
            "--db",
            "sqlite",
            "--db-path",
            str(tmp / "n.db"),
            "--port",
            str(port),
        ],
        cwd=AIMEAT_DIR,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    serve_pid = None
    old_home = os.environ.get("AIMEAT_HOME")
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if node.poll() is not None:
                pytest.fail(f"node exited early ({node.returncode})")
            try:
                with urllib.request.urlopen(f"{base}/v1/spec", timeout=2) as r:
                    if r.status == 200:
                        break
            except OSError:
                time.sleep(0.3)
        else:
            pytest.fail("node did not become ready within 120 s")

        owner = f"onbowner{int(time.time())}"
        r = requests.post(f"{base}/v1/owners", json={"name": owner, "public_key": "placeholder"}, timeout=20)
        assert r.status_code == 201, r.text
        owner_headers = {"Authorization": f"Bearer {_token(base, owner, r.json()['data']['private_key'], agent=False)}"}
        r = requests.post(
            f"{base}/v1/agents",
            headers=owner_headers,
            json={"name": AGENT, "owner": owner, "capabilities": ["memory"], "scopes": ["*"]},
            timeout=20,
        )
        assert r.status_code == 201, r.text
        gaii = r.json()["data"]["agent"]["gaii"]
        token = _token(base, gaii, r.json()["data"]["private_key"], agent=True)
        (home / "tokens").mkdir()
        (home / "tokens" / f"{AGENT}@{owner}.token").write_text(token, encoding="utf-8")
        (home / "agents" / AGENT).mkdir(parents=True)
        (home / "agents" / AGENT / "config.yaml").write_text(
            f"agent: {AGENT}\nowner: {owner}\nnode_url: {base}\nprimary: true\n", encoding="utf-8"
        )
        os.environ["AIMEAT_HOME"] = str(home)
        from aimeat_crewai.mcp_client import ensure_serve

        doc = ensure_serve(
            aimeat_command=["node", "--import", "tsx", "src/index.ts"],
            spawn_cwd=str(AIMEAT_DIR),
            env={**os.environ, "AIMEAT_HOME": str(home)},
            start_timeout=120,
        )
        serve_pid = doc.get("pid")
        from crewaimeat import aimeat_crew

        aimeat_crew._serve_reset()
        yield {"base": base, "owner_headers": owner_headers, "name": AGENT, "home": home}
    finally:
        _kill(serve_pid)
        if node.poll() is None:
            node.kill()
            node.wait(timeout=20)
        if old_home is None:
            os.environ.pop("AIMEAT_HOME", None)
        else:
            os.environ["AIMEAT_HOME"] = old_home


@pytest.fixture
def home(place, monkeypatch):
    """conftest gives every test its own empty AIMEAT_HOME; this test works in the place's."""
    monkeypatch.setenv("AIMEAT_HOME", str(place["home"]))
    from crewaimeat import aimeat_crew

    aimeat_crew._serve_reset()
    return place


def test_the_test_task_plan_is_proposed_once_and_the_step_passes(home, capsys):
    from crewaimeat import aimeat_crew

    place = home
    started = time.monotonic()
    aimeat_crew._run_onboarding_only(place["name"])
    seconds = round(time.monotonic() - started, 1)
    err = capsys.readouterr().err
    dump = os.environ.get("ONBOARDING_LOG_DUMP")
    if dump:  # the whole run log, for a person reading a failure
        Path(dump).write_text(err, encoding="utf-8")
    # The driver logs a call and its answer on two lines; the call line carries the arguments source.
    proposed = [line for line in err.splitlines() if "accept_test_task -> aimeat_task_propose_todos (" in line]
    if len(proposed) != 1:
        tasks = requests.get(
            f"{place['base']}/v1/agents/{place['name']}/tasks", headers=place["owner_headers"], timeout=20
        ).json()
        status = requests.get(
            f"{place['base']}/v1/agents/{place['name']}/onboarding", headers=place["owner_headers"], timeout=20
        ).json()
        if dump:
            Path(dump).write_text(f"{err}\n\nTASKS {tasks}\n\nONBOARDING {status}", encoding="utf-8")
    assert len(proposed) == 1, f"the plan was proposed {len(proposed)} times in {seconds}s:\n" + "\n".join(proposed)

    r = requests.get(
        f"{place['base']}/v1/agents/{place['name']}/onboarding", headers=place["owner_headers"], timeout=20
    )
    assert r.status_code == 200, r.text
    steps = {s["id"]: s["status"] for s in r.json()["data"]["onboarding"]["steps"]}
    assert steps["accept_test_task"] == "passed", steps
    assert steps["complete_test_task"] == "passed", steps

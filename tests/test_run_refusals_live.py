"""Integration: a run the node refused ends as REFUSED, against a REAL node.

A real node process started from an aimeat-protocol checkout, a real database, real SCOPE_DENIED
refusals recorded by the node itself, real tasks, and crewaimeat's own code on top: the real
`_run_refusals` reading the node over the real transport (its direct authed fallback, since no serve
daemon runs here), and the real `complete_callback` deciding. The one seam is the task tool call, which
in a crew goes over the serve daemon; here it goes to the same node route directly as the agent.

What the offline tests cannot show, and these do:
  - the node really records the refused write, and crewaimeat really reads it back inside the window;
  - the task ends FAILED on the node, with the call and the permission named, and not completed;
  - a run with nothing refused still completes -- the check does not turn good runs red;
  - once the owner gives the permission, the refusal closes, so "then the task can run again" is true;
  - `--scopes` REPLACES the node default for a new agent. This is the claim the whole requested-scopes
    list is built around, and an agent that asks only for what it needs beyond the defaults is really
    approved WITHOUT memory access. Proven here against the node rather than read off its source.

Where the node comes from: AIMEAT_PROTOCOL_DIR (the repo or its `aimeat/` directory), else a sibling
`../aimeat-protocol` checkout. It needs `node` and `pnpm install` done in `aimeat/`. Without them the
module skips -- this is a fleet machine's test, not CI's, because CI has no aimeat-protocol checkout.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest
import requests

NODE_ID = "aimeat-local-001-dev"
DEFAULTS = ["memory:read", "memory:write", "memory:delete", "catalogue:read"]


def _aimeat_dir() -> Path | None:
    env = os.environ.get("AIMEAT_PROTOCOL_DIR")
    candidates = [Path(env)] if env else []
    candidates.append(Path(__file__).resolve().parents[2] / "aimeat-protocol")
    for c in candidates:
        for d in (c / "aimeat", c):
            if (d / "src" / "index.ts").is_file() and (d / "node_modules").is_dir():
                return d
    return None


AIMEAT_DIR = _aimeat_dir()


def _has_refusals_route(d: Path | None) -> bool:
    """A checkout older than aimeat-protocol 4edff2d9d answers the route 404, which crewaimeat reads as
    "no refusals known" -- correctly, for production, and useless for a test of what happens when there
    ARE refusals. Skip with that reason rather than fail for one."""
    if d is None:
        return False
    try:
        return "/v1/agents/:name/refusals" in (d / "src" / "routes" / "agent-activity.ts").read_text(encoding="utf-8")
    except OSError:
        return False


pytestmark = [
    pytest.mark.skipif(
        shutil.which("node") is None or AIMEAT_DIR is None,
        reason="needs node and an aimeat-protocol checkout with `pnpm install` done (AIMEAT_PROTOCOL_DIR)",
    ),
    pytest.mark.skipif(
        AIMEAT_DIR is not None and not _has_refusals_route(AIMEAT_DIR),
        reason=f"the aimeat-protocol checkout at {AIMEAT_DIR} predates the refusals route (4edff2d9d)",
    ),
    # A real local server: conftest's one exception to "no network in tests", loopback only.
    pytest.mark.loopback,
    # Signing an agent's token runs `node` (the node's own ed25519), a local process and nothing more.
    pytest.mark.local_process,
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _sign(priv: str, message: str) -> str:
    code = (
        "import * as ed from '@noble/ed25519';"
        "const [priv, msg] = process.argv.slice(1);"
        "const sig = await ed.signAsync(new TextEncoder().encode(msg), Buffer.from(priv, 'base64'));"
        "console.log(Buffer.from(sig).toString('base64'));"
    )
    out = subprocess.run(
        ["node", "--input-type=module", "-e", code, priv, message],
        cwd=AIMEAT_DIR,
        capture_output=True,
        text=True,
        timeout=90,
        check=True,
    )
    return out.stdout.strip()


def _token(base: str, ident: str, priv: str, *, agent: bool) -> str:
    ts = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
    msg = (ident + ts) if agent else (ident + NODE_ID + ts)
    key = "gaii" if agent else "owner"
    body = requests.post(
        f"{base}/v1/auth/token", json={key: ident, "timestamp": ts, "signature": _sign(priv, msg)}, timeout=20
    ).json()
    assert body.get("ok") is True, body
    return body["data"]["token"]


class Node:
    base = ""
    owner = ""
    owner_headers: dict = {}
    home: Path | None = None
    agents: dict = {}  # name -> (gaii, token), registered in the fixture


# Every agent a test needs, registered up front with the four defaults and nothing more. Up front because
# signing an agent's token runs `node`, and conftest allows no process but Python inside a test body.
AGENTS = ("concierge", "clean-agent", "granted-later")


@pytest.fixture(scope="module")
def node(tmp_path_factory):
    n = Node()
    tmp = tmp_path_factory.mktemp("refusals-live")
    port = _free_port()
    n.base = f"http://localhost:{port}"
    n.owner = f"refowner{int(time.time())}"
    n.home = tmp / "aimeat-home"
    n.home.mkdir()
    env = {
        **os.environ,
        "AIMEAT_PORT": str(port),
        "AIMEAT_BASE_URL": n.base,
        "AIMEAT_RL_GLOBAL": "100000",
        "AIMEAT_RL_AUTH": "10000",
        "AIMEAT_RL_WORK": "10000",
        "AIMEAT_RL_MEMORY": "10000",
    }
    # The STOCK default on purpose: the whole point is an agent that holds the four defaults and nothing
    # more, which is what the sold seat held. A test node that grants `*` could never refuse anything.
    env.pop("AIMEAT_DEFAULT_AGENT_SCOPES", None)
    proc = subprocess.Popen(
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
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                pytest.fail(f"node exited early ({proc.returncode})")
            try:
                with urllib.request.urlopen(f"{n.base}/v1/spec", timeout=2) as r:
                    if r.status == 200:
                        break
            except OSError:
                time.sleep(0.3)
        else:
            pytest.fail("node did not become ready within 120 s")
        r = requests.post(f"{n.base}/v1/owners", json={"name": n.owner, "public_key": "placeholder"}, timeout=20)
        assert r.status_code == 201, r.text
        n.owner_headers = {
            "Authorization": f"Bearer {_token(n.base, n.owner, r.json()['data']['private_key'], agent=False)}"
        }
        n.agents = {name: _register(n, name, DEFAULTS) for name in AGENTS}
        yield n
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=20)


@pytest.fixture
def agent_home(node, monkeypatch):
    """Point crewaimeat's home at this node, with no serve daemon, so the real transport goes direct."""
    monkeypatch.setenv("AIMEAT_HOME", str(node.home))
    from crewaimeat import aimeat_crew, lifecycle

    aimeat_crew._serve_reset()
    lifecycle._REFUSED.clear()
    lifecycle._WORKER_RUN_START["at"] = None
    yield
    lifecycle._REFUSED.clear()


def _agent(node: Node, name: str, _scopes: list[str]) -> tuple[str, str]:
    """The agent the fixture registered for this test. (gaii, token)"""
    return node.agents[name]


def _register(node: Node, name: str, scopes: list[str]) -> tuple[str, str]:
    """Register an agent with exactly `scopes`, store its token where crewaimeat looks. (gaii, token)"""
    r = requests.post(
        f"{node.base}/v1/agents",
        headers=node.owner_headers,
        json={"name": name, "owner": node.owner, "capabilities": ["memory"], "scopes": scopes},
        timeout=20,
    )
    assert r.status_code == 201, r.text
    gaii = r.json()["data"]["agent"]["gaii"]
    token = _token(node.base, gaii, r.json()["data"]["private_key"], agent=True)
    (node.home / "tokens").mkdir(exist_ok=True)
    (node.home / "tokens" / f"{name}@{node.owner}.token").write_text(token, encoding="utf-8")
    cfg = node.home / "agents" / name
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "config.yaml").write_text(
        f"agent: {name}\nowner: {node.owner}\nnode_url: {node.base}\nprimary: true\n", encoding="utf-8"
    )
    return gaii, token


def _active_task(node: Node, name: str) -> str:
    r = requests.post(
        f"{node.base}/v1/agents/{name}/tasks",
        headers=node.owner_headers,
        json={"title": "Write the brief", "description": "Write the brief."},
        timeout=20,
    ).json()
    assert r.get("ok"), r
    tid = (r["data"].get("task") or r["data"])["id"]
    s = requests.post(
        f"{node.base}/v1/agents/{name}/tasks/{tid}/start", headers=node.owner_headers, json={}, timeout=20
    )
    assert s.json().get("ok"), s.text
    return tid


def _task(node: Node, name: str, tid: str) -> dict:
    d = requests.get(f"{node.base}/v1/agents/{name}/tasks/{tid}", headers=node.owner_headers, timeout=20).json()["data"]
    return d.get("task") or d


def _as_agent_call(node: Node, name: str, token: str):
    """The task tools, as the agent, straight at the node's routes (in a crew they go over the daemon)."""
    routes = {"aimeat_task_complete": "complete", "aimeat_task_fail": "fail"}

    def call(_agent_name, tool, payload, **_kw):
        if tool not in routes:
            return {"ok": True}
        r = requests.post(
            f"{node.base}/v1/agents/{name}/tasks/{payload['task_id']}/{routes[tool]}",
            headers={"Authorization": f"Bearer {token}"},
            json={k: v for k, v in payload.items() if k != "task_id"},
            timeout=20,
        ).json()
        return r.get("data") if r.get("ok") else None

    return call


def _wait_for_refusals(name: str, since: str, *, seconds: float = 10.0) -> list:
    """The node writes a refusal just AFTER it answers the 403, so give that write a moment to land."""
    from crewaimeat.aimeat_crew import _run_refusals

    deadline = time.monotonic() + seconds
    got: list | None = []
    while time.monotonic() < deadline:
        got = _run_refusals(name, since)
        if got:
            return got
        time.sleep(0.25)
    return got or []


def _callbacks(call):
    from crewaimeat.aimeat_crew import _run_refusals
    from crewaimeat.lifecycle import LifecycleCallbacks

    return LifecycleCallbacks(call, lambda _i: None, lambda _a, _t: None, {}, refusals=_run_refusals)


# ── the run ──────────────────────────────────────────────────────────────────────────────────


def test_a_refused_run_ends_failed_on_the_node_and_not_completed(node, agent_home):
    from crewaimeat import lifecycle

    name = "concierge"
    _gaii, token = _agent(node, name, DEFAULTS)
    tid = _active_task(node, name)
    since = lifecycle.run_started_iso()

    # The sold seat's own write: the identity push, which needs agent:write.
    r = requests.patch(
        f"{node.base}/v1/agents/{name}/tags",
        headers={"Authorization": f"Bearer {token}"},
        json={"tags": ["x"]},
        timeout=20,
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "SCOPE_DENIED", r.text

    refused = _wait_for_refusals(name, since)
    assert refused, "crewaimeat's reader must see the refusal the node recorded"
    assert any("agent:write" in (x.get("needed") or []) for x in refused), refused

    _callbacks(_as_agent_call(node, name, token)).complete_callback(name, tid, since=since)(None)

    task = _task(node, name, tid)
    assert task["status"] == "failed", f"a refused run must not end done: {task['status']}"
    # What the owner reads: the failure is on the task's own events, in the words the run sent.
    events = requests.get(
        f"{node.base}/v1/agents/{name}/tasks/{tid}/events", headers=node.owner_headers, timeout=20
    ).json()
    said = str(events.get("data"))
    assert "PATCH /v1/agents/:name/tags" in said and "agent:write" in said, "name the call and the permission"
    assert "Manage access rights" in said, "and where the owner gives it"
    assert tid in lifecycle.refused_runs()


def test_a_run_with_nothing_refused_still_completes(node, agent_home):
    from crewaimeat import lifecycle

    name = "clean-agent"
    _gaii, token = _agent(node, name, DEFAULTS)
    tid = _active_task(node, name)
    since = lifecycle.run_started_iso()
    # A permitted write, so the run really touched the node.
    ok = requests.post(
        f"{node.base}/v1/memory",
        headers={"Authorization": f"Bearer {token}"},
        json={"key": "brief.draft", "value": "ok"},
        timeout=20,
    )
    assert ok.status_code in (200, 201), ok.text

    _callbacks(_as_agent_call(node, name, token)).complete_callback(name, tid, since=since)(None)

    # The node's word for a completed task is `done`.
    assert _task(node, name, tid)["status"] == "done"
    assert lifecycle.refused_runs() == {}


def test_once_the_owner_gives_the_permission_the_refusal_closes(node, agent_home):
    # The failure message says "then the task can run again". This is what makes that true: a refusal
    # stays open only while the permission is missing.
    from crewaimeat import lifecycle
    from crewaimeat.aimeat_crew import _run_refusals

    name = "granted-later"
    _gaii, token = _agent(node, name, DEFAULTS)
    since = lifecycle.run_started_iso()
    requests.patch(
        f"{node.base}/v1/agents/{name}/tags",
        headers={"Authorization": f"Bearer {token}"},
        json={"tags": ["x"]},
        timeout=20,
    )
    assert _wait_for_refusals(name, since), "precondition: refused"

    g = requests.patch(
        f"{node.base}/v1/agents/{name}/scopes",
        headers=node.owner_headers,
        json={"scopes": [*DEFAULTS, "agent:write"]},
        timeout=20,
    )
    assert g.json().get("ok"), g.text
    assert _run_refusals(name, since) == []


# ── what an agent asks for at approval ──────────────────────────────────────────────────────


def _approve(node: Node, name: str, scopes: list[str]) -> list[str]:
    """Device-authorize `name` asking for `scopes`, approve it as the owner without editing the list,
    and return what the node actually granted."""
    d = requests.post(
        f"{node.base}/v1/agents/device-authorize",
        json={"agent_name": name, "owner": node.owner, "scopes": scopes},
        timeout=20,
    ).json()
    assert d.get("ok"), d
    code = d["data"]["user_code"]
    owner_token = node.owner_headers["Authorization"].split(" ", 1)[1]
    v = requests.post(
        f"{node.base}/v1/agents/verify",
        json={"user_code": code, "action": "approve", "owner_token": owner_token},
        timeout=20,
    ).json()
    assert v.get("ok"), v
    got = requests.get(f"{node.base}/v1/agents/{name}/refusals", headers=node.owner_headers, timeout=20).json()
    return got["data"]["granted_scopes"]


def test_what_crewaimeat_asks_for_is_exactly_what_a_new_agent_is_granted(node):
    from crewaimeat.agent_scopes import requested_scopes

    asked = requested_scopes()
    granted = _approve(node, "asks-properly", asked)
    assert set(granted) == set(asked), granted
    assert "memory:write" in granted, "memory is where every deliverable goes"
    assert "agent:write" in granted, "the identity push the sold seat was refused"


def test_asking_only_for_what_is_needed_beyond_the_defaults_loses_memory(node):
    # THE TRAP, proven rather than read: --scopes REPLACES the node default for a new agent. A request
    # that named only the scaffold's extra needs would approve an agent that cannot write its deliverable.
    from crewaimeat.agent_scopes import REQUIRED_SCOPES

    granted = _approve(node, "asks-too-little", list(REQUIRED_SCOPES))
    assert "memory:write" not in granted and "memory:read" not in granted, granted

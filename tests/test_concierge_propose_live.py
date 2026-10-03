"""Integration: the concierge proposes an agent on a REAL node, through a REAL serve daemon.

The path a hosted place really uses, end to end: a real node, a real `aimeat connect serve --http`
daemon (schema-3 serve.json, its per-start secret), and crewaimeat's own `_aimeat_call` reaching the
connector's /local/call surface -- the one every crew uses, which is a different surface from the
node's MCP tools. Nothing below the concierge's code is faked.

The fixture builds the brief's scene: an owner whose organism keeps a CRM workspace called CADENCE
with a deal in it, and a concierge holding what the hosted anchor holds after the 2026-10-02 roll. Then
the brief's acceptance, in order:
  1. the concierge finds CADENCE, and proposes an agent whose purpose and definition name it;
  2. the proposal is on the node (GET /v1/agents/v2/agent-proposals) with a runnable crew_def, the
     runtime's own write scopes, task-runner/spawn, and the reply carries the node's approval address;
  3. "start it" before the approval sets nothing;
  4. the owner approves: the agent is created and seeded; "start it" now sets an agent_task schedule;
  5. the new agent, with the credential its approval gave it, reads CADENCE through the daemon.

Not here: the new agent's first run, which costs a real model call. Every step before it is.

Where the node comes from: AIMEAT_PROTOCOL_DIR, else a sibling `../aimeat-protocol` checkout, with
`pnpm install` done -- and new enough to carry the proposal's approval address (aimeat-protocol
0b24e1a63). Skips otherwise: a fleet machine's test, not CI's, which has no aimeat-protocol checkout.
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
CONCIERGE = "concierge"
PROPOSED = "morning-deals"
# What the hosted anchor holds after the 2026-10-02 roll (aimeat-commercial provision.ts plus the two
# organism words), so the cap the concierge applies is the one a real place applies.
HOSTED_SCOPES = [
    "memory:read",
    "memory:write",
    "memory:delete",
    "catalogue:read",
    "agent:write",
    "task:write",
    "workflow:read",
    "wallet:read",
    "organism:read",
    "organism:write",
]


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


def _has_proposal_address(d: Path | None) -> bool:
    try:
        return "approval_url" in (d / "src" / "routes" / "agents-v2" / "agent-proposals.ts").read_text(encoding="utf-8")
    except (OSError, TypeError):
        return False


pytestmark = [
    pytest.mark.skipif(
        shutil.which("node") is None or AIMEAT_DIR is None,
        reason="needs node and an aimeat-protocol checkout with `pnpm install` done (AIMEAT_PROTOCOL_DIR)",
    ),
    pytest.mark.skipif(
        AIMEAT_DIR is not None and not _has_proposal_address(AIMEAT_DIR),
        reason=f"the aimeat-protocol checkout at {AIMEAT_DIR} predates the proposal's approval address (0b24e1a63)",
    ),
    pytest.mark.loopback,  # a real local node and daemon; loopback only
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


class Scene:
    base = ""
    owner_headers: dict = {}
    home: Path | None = None
    crm: dict = {}
    reply = ""


def _kill(pid: int | None) -> None:
    if not pid:
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


@pytest.fixture(scope="module")
def scene(tmp_path_factory):
    """The node, the daemon, the owner's organism with CADENCE in it, and the concierge's proposal."""
    s = Scene()
    tmp = tmp_path_factory.mktemp("concierge-live")
    s.home = tmp / "home"
    s.home.mkdir()
    port = _free_port()
    s.base = f"http://localhost:{port}"
    env = {
        **os.environ,
        "AIMEAT_PORT": str(port),
        "AIMEAT_BASE_URL": s.base,
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
                with urllib.request.urlopen(f"{s.base}/v1/spec", timeout=2) as r:
                    if r.status == 200:
                        break
            except OSError:
                time.sleep(0.3)
        else:
            pytest.fail("node did not become ready within 120 s")

        owner = f"cpowner{int(time.time())}"
        r = requests.post(f"{s.base}/v1/owners", json={"name": owner, "public_key": "placeholder"}, timeout=20)
        assert r.status_code == 201, r.text
        s.owner_headers = {
            "Authorization": f"Bearer {_token(s.base, owner, r.json()['data']['private_key'], agent=False)}"
        }

        r = requests.post(
            f"{s.base}/v1/agents",
            headers=s.owner_headers,
            json={"name": CONCIERGE, "owner": owner, "capabilities": ["memory"], "scopes": HOSTED_SCOPES},
            timeout=20,
        )
        assert r.status_code == 201, r.text
        gaii = r.json()["data"]["agent"]["gaii"]
        token = _token(s.base, gaii, r.json()["data"]["private_key"], agent=True)
        (s.home / "tokens").mkdir()
        (s.home / "tokens" / f"{CONCIERGE}@{owner}.token").write_text(token, encoding="utf-8")
        (s.home / "agents" / CONCIERGE).mkdir(parents=True)
        (s.home / "agents" / CONCIERGE / "config.yaml").write_text(
            f"agent: {CONCIERGE}\nowner: {owner}\nnode_url: {s.base}\nprimary: true\n", encoding="utf-8"
        )

        # The real daemon every crew talks to, started from the same checkout as the node.
        os.environ["AIMEAT_HOME"] = str(s.home)
        from aimeat_crewai.mcp_client import ensure_serve

        doc = ensure_serve(
            aimeat_command=["node", "--import", "tsx", "src/index.ts"],
            spawn_cwd=str(AIMEAT_DIR),
            env={**os.environ, "AIMEAT_HOME": str(s.home)},
            start_timeout=120,
        )
        serve_pid = doc.get("pid")

        from crewaimeat import aimeat_crew, concierge_propose

        aimeat_crew._serve_reset()

        # The person's CRM: an organism with a CADENCE workspace and one deal, made through the daemon.
        def call(tool, payload):
            out = aimeat_crew._aimeat_call(CONCIERGE, tool, payload, return_error=True)
            assert isinstance(out, dict) and out.get("ok") is not False, f"{tool}: {out}"
            return out

        org = call("aimeat_organism_create", {"name": "Acme Oy"})["organism"]["id"]
        manifest = {
            "manifestVersion": "1.0",
            "id": "cadence",
            "name": "CADENCE",
            "kind": "project",
            "status": "active",
            "summary": "The CRM",
            "objectTypes": [
                {
                    "name": "deal",
                    "namespace": "deals",
                    "schemaRef": "cadence.deal",
                    "backing": "memory",
                    "writeRole": "member",
                    "mode": "records",
                }
            ],
        }
        ws = call("aimeat_workspace_create", {"organism_id": org, "name": "CADENCE", "manifest": manifest})["ws"]
        call(
            "aimeat_workspace_write",
            {
                "organism_id": org,
                "ws": ws,
                "space": "deal",
                "id": "deal-1",
                "value": {"id": "deal-1", "title": "Rantanen Ky", "stage": "proposal", "next_step_due": "2026-10-03"},
            },
        )
        s.crm = {"organism_id": org, "ws": ws}

        # The address a person opens the approval at (crewaimeat.public_url): on a hosted place the fleet
        # passes the public one; here the local node's own base is the address its owner opens.
        os.environ["AIMEAT_BASE_URL"] = s.base
        # The brief's sentence, as the concierge's tool call.
        s.reply = concierge_propose.propose(
            CONCIERGE,
            name=PROPOSED,
            display_name="Morning deals",
            purpose="Reads the open deals in CADENCE every morning and names the ones to act on today.",
            instructions="Read the open deals and list the ones whose next step is due today or overdue.",
            workspace="CADENCE",
            schedule_cron="0 7 * * *",
        )
        yield s
    finally:
        _kill(serve_pid)
        if node.poll() is None:
            node.kill()
            node.wait(timeout=20)
        os.environ.pop("AIMEAT_BASE_URL", None)
        if old_home is None:
            os.environ.pop("AIMEAT_HOME", None)
        else:
            os.environ["AIMEAT_HOME"] = old_home


@pytest.fixture
def home(scene, monkeypatch):
    """conftest gives every test its own empty AIMEAT_HOME; these tests work in the scene's."""
    monkeypatch.setenv("AIMEAT_HOME", str(scene.home))
    from crewaimeat import aimeat_crew

    aimeat_crew._serve_reset()
    return scene


def _proposals(s: Scene) -> list[dict]:
    r = requests.get(f"{s.base}/v1/agents/v2/agent-proposals", headers=s.owner_headers, timeout=20).json()
    return r["data"]["proposals"]


# ── 1-2: it finds the person's CRM and proposes on it ───────────────────────────────────────


def test_the_concierge_sees_the_persons_crm(home):
    from crewaimeat import workspace_tools

    found = workspace_tools.list_workspaces(CONCIERGE)
    hit = workspace_tools.find_workspace(found, "cadence")
    assert hit is not None and hit["ws"] == home.crm["ws"], found


def test_the_proposal_is_on_the_node_and_names_the_crm(home):
    from crewaimeat.agent_scopes import RUNTIME_WRITE_SCOPES
    from crewaimeat.crew_def import validate_crew_doc

    mine = [p for p in _proposals(home) if p["name"] == PROPOSED]
    assert len(mine) == 1, "one waiting proposal"
    p = mine[0]
    assert "CADENCE" in p["purpose"]
    assert p["mode"] == "task-runner" and p["run_mode"] == "spawn"
    assert set(RUNTIME_WRITE_SCOPES) <= set(p["scopes"]), "what every run writes with"
    d = p["crew_def"]
    assert home.crm["ws"] in d["tasks"][0]["description"], "the agent is told where its data is"
    assert validate_crew_doc(d) == [], "the runtime that will run it accepts what the node keeps"


def test_the_reply_gives_the_approval_address_and_no_outside_product(home):
    assert "/v1/profile?tab=agents" in home.reply
    for outside in ("CrewAI Studio", "HubSpot", "Salesforce", "Pipedrive", "Zapier"):
        assert outside not in home.reply
    assert f"start {PROPOSED}" in home.reply


# ── 3-4: the clock comes after the approval ─────────────────────────────────────────────────


def test_start_before_the_approval_sets_nothing(home):
    from crewaimeat import concierge_propose

    reply = concierge_propose.start_proposed(CONCIERGE, PROPOSED)
    assert "Approve it first" in reply
    r = requests.get(f"{home.base}/v1/agents/{PROPOSED}/schedules", headers=home.owner_headers, timeout=20).json()
    assert not ((r.get("data") or {}).get("managed")), "no schedule for an agent that does not exist"


def test_after_the_approval_the_agent_exists_and_start_sets_its_schedule(home):
    from crewaimeat import concierge_propose

    pid = next(p["id"] for p in _proposals(home) if p["name"] == PROPOSED)
    ap = requests.post(
        f"{home.base}/v1/agents/v2/agent-proposals/{pid}/approve", headers=home.owner_headers, json={}, timeout=60
    ).json()
    assert ap.get("ok"), ap
    assert ap["data"]["created"] is True and ap["data"]["seeded"] is True

    reply = concierge_propose.start_proposed(CONCIERGE, PROPOSED)
    assert reply.startswith("Done"), reply
    managed = requests.get(
        f"{home.base}/v1/agents/{PROPOSED}/schedules", headers=home.owner_headers, timeout=20
    ).json()["data"]["managed"]
    assert len(managed) == 1
    assert managed[0]["type"] == "agent_task" and managed[0]["cron"] == "0 7 * * *" and managed[0]["enabled"]


# ── 5: the new agent can read what it was made to read ─────────────────────────────────────


def test_the_new_agent_reads_its_crm_with_what_its_approval_gave_it(home):
    from crewaimeat import workspace_tools

    seen = workspace_tools.list_workspaces(PROPOSED)
    assert any(w["ws"] == home.crm["ws"] for w in seen), f"the new agent cannot see CADENCE: {seen}"
    index = workspace_tools.workspace_index(PROPOSED, home.crm["organism_id"], home.crm["ws"])
    assert "Rantanen Ky" in str(index), "and reads the deal in it"

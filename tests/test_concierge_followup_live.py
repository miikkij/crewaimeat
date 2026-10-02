"""Integration: the follow-up's three changes against a REAL node, through a REAL serve daemon.

Same road as test_concierge_propose_live.py: a real node, a real `aimeat connect serve --http` daemon,
and crewaimeat's own `_aimeat_call` on the connector's /local/call surface. Nothing below crewaimeat's
code is faked.

The scene: an owner whose organism keeps CADENCE, with a record space (contacts) and a row space (mail),
and two agents of that owner -- `clerk`, holding what the CRM agent of the Yrittajan peruspaketti needs to
write, and `reader`, holding the same minus organism:write. Then:
  1. `workspace_write`: clerk creates a contact, updates one field of it (the rest stays), removes one,
     appends a mail row and replaces it by its row id -- all visible to the owner;
  2. the node enforces the scope: reader's write is refused at the publish, in the node's words, and
     says that the draft is there but not shown;
  3. a declined task ends FAILED on the node with "Declined: <reason>", not done;
  4. a verify report lands on the task as a `verification` event, which is where it is kept.

Skips without an aimeat-protocol checkout (AIMEAT_PROTOCOL_DIR, else ../aimeat-protocol, `pnpm install`
done): a fleet machine's test, not CI's.
"""

from __future__ import annotations

import os
import subprocess
import time
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from test_concierge_propose_live import AIMEAT_DIR, _free_port, _kill, _token
from test_concierge_propose_live import pytestmark as _live_marks

pytestmark = _live_marks

CLERK = "clerk"
READER = "reader"
WRITER_SCOPES = [
    "memory:read",
    "memory:write",
    "agent:write",
    "task:write",
    "organism:read",
    "organism:write",
]
READER_SCOPES = [s for s in WRITER_SCOPES if s != "organism:write"]


class Scene:
    base = ""
    owner_headers: dict = {}
    home: Path | None = None
    org = ""
    ws = ""


def _start_node(tmp: Path, port: int, base: str) -> subprocess.Popen:
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
        ["node", "--import", "tsx", "src/index.ts", "start", "--db", "sqlite", "--db-path", str(tmp / "n.db")]
        + ["--port", str(port)],
        cwd=AIMEAT_DIR,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if node.poll() is not None:
            pytest.fail(f"node exited early ({node.returncode})")
        try:
            with urllib.request.urlopen(f"{base}/v1/spec", timeout=2) as r:
                if r.status == 200:
                    return node
        except OSError:
            time.sleep(0.3)
    node.kill()
    pytest.fail("node did not become ready within 120 s")


@pytest.fixture(scope="module")
def scene(tmp_path_factory):
    s = Scene()
    tmp = tmp_path_factory.mktemp("followup-live")
    s.home = tmp / "home"
    (s.home / "tokens").mkdir(parents=True)
    port = _free_port()
    s.base = f"http://localhost:{port}"
    node = _start_node(tmp, port, s.base)
    serve_pid = None
    old_home = os.environ.get("AIMEAT_HOME")
    try:
        owner = f"fuowner{int(time.time())}"
        r = requests.post(f"{s.base}/v1/owners", json={"name": owner, "public_key": "placeholder"}, timeout=20)
        assert r.status_code == 201, r.text
        s.owner_headers = {
            "Authorization": f"Bearer {_token(s.base, owner, r.json()['data']['private_key'], agent=False)}"
        }
        for name, scopes in ((CLERK, WRITER_SCOPES), (READER, READER_SCOPES)):
            r = requests.post(
                f"{s.base}/v1/agents",
                headers=s.owner_headers,
                json={"name": name, "owner": owner, "capabilities": ["memory"], "scopes": scopes},
                timeout=20,
            )
            assert r.status_code == 201, r.text
            data = r.json()["data"]
            token = _token(s.base, data["agent"]["gaii"], data["private_key"], agent=True)
            (s.home / "tokens" / f"{name}@{owner}.token").write_text(token, encoding="utf-8")
            cfg = s.home / "agents" / name
            cfg.mkdir(parents=True)
            (cfg / "config.yaml").write_text(
                f"agent: {name}\nowner: {owner}\nnode_url: {s.base}\nprimary: {'true' if name == CLERK else 'false'}\n",
                encoding="utf-8",
            )

        os.environ["AIMEAT_HOME"] = str(s.home)
        from aimeat_crewai.mcp_client import ensure_serve

        doc = ensure_serve(
            aimeat_command=["node", "--import", "tsx", "src/index.ts"],
            spawn_cwd=str(AIMEAT_DIR),
            env={**os.environ, "AIMEAT_HOME": str(s.home)},
            start_timeout=120,
        )
        serve_pid = doc.get("pid")

        from crewaimeat import aimeat_crew

        aimeat_crew._serve_reset()

        def call(tool, payload):
            out = aimeat_crew._aimeat_call(CLERK, tool, payload, return_error=True)
            assert isinstance(out, dict) and out.get("ok") is not False, f"{tool}: {out}"
            return out

        s.org = call("aimeat_organism_create", {"name": "Koe Oy"})["organism"]["id"]
        manifest = {
            "manifestVersion": "1.0",
            "id": "cadence",
            "name": "CADENCE",
            "kind": "project",
            "status": "active",
            "summary": "The CRM",
            "objectTypes": [
                {
                    "name": "contact",
                    "namespace": "crm.contacts",
                    "schemaRef": "cadence.contact",
                    "backing": "memory",
                    "writeRole": "member",
                    "mode": "records",
                },
                {
                    "name": "mailmessage",
                    "namespace": "crm.mail",
                    "schemaRef": "cadence.mail",
                    "backing": "rows",
                    "writeRole": "member",
                    "indexOn": ["contactRef"],
                },
            ],
        }
        s.ws = call("aimeat_workspace_create", {"organism_id": s.org, "name": "CADENCE", "manifest": manifest})["ws"]
        yield s
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
def live(scene, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(scene.home))
    from crewaimeat import aimeat_crew

    aimeat_crew._serve_reset()
    return scene


def _workspace(s: Scene) -> dict:
    r = requests.get(
        f"{s.base}/v1/organisms/{s.org}/workspace", params={"ws": s.ws}, headers=s.owner_headers, timeout=20
    ).json()
    assert r.get("ok"), r
    return r["data"]


def _contact(s: Scene, rid: str) -> dict | None:
    return next((c for c in _workspace(s)["objects"].get("contact") or [] if c.get("id") == rid), None)


def _active_task(s: Scene, name: str, title: str) -> str:
    r = requests.post(
        f"{s.base}/v1/agents/{name}/tasks",
        headers=s.owner_headers,
        json={"title": title, "description": title},
        timeout=20,
    ).json()
    assert r.get("ok"), r
    tid = (r["data"].get("task") or r["data"])["id"]
    st = requests.post(f"{s.base}/v1/agents/{name}/tasks/{tid}/start", headers=s.owner_headers, json={}, timeout=20)
    assert st.json().get("ok"), st.text
    return tid


def _task(s: Scene, name: str, tid: str) -> dict:
    r = requests.get(f"{s.base}/v1/agents/{name}/tasks/{tid}", headers=s.owner_headers, timeout=20).json()
    assert r.get("ok"), r
    return r["data"]


# ── 1. writing CADENCE ──────────────────────────────────────────────────────────────────────


def test_the_clerk_creates_a_contact_the_owner_sees_published(live):
    from crewaimeat import workspace_tools

    done = workspace_tools.write_record(
        CLERK,
        live.org,
        live.ws,
        "contact",
        {"name": "Testi Asiakas", "email": "testi.asiakas@example.com", "company": "Koe Oy"},
        "c-testi",
    )
    assert done == {"id": "c-testi", "space": "contact", "namespace": "crm.contacts", "created": True}
    got = _contact(live, "c-testi")
    assert got and got["name"] == "Testi Asiakas", "published: in the owner's objects, not only a draft"


def test_an_update_of_one_field_keeps_the_rest_and_a_none_removes_one(live):
    from crewaimeat import workspace_tools

    done = workspace_tools.write_record(
        CLERK, live.org, live.ws, "contact", {"phone": "040 123 4567", "company": None}, "c-testi"
    )
    assert done["created"] is False
    got = _contact(live, "c-testi")
    assert got["phone"] == "040 123 4567"
    assert got["name"] == "Testi Asiakas" and got["email"] == "testi.asiakas@example.com", "nothing else wiped"
    assert "company" not in got


def test_rows_are_appended_and_replaced_by_their_row_id(live):
    from crewaimeat import workspace_tools

    first = workspace_tools.append_rows(
        CLERK,
        live.org,
        live.ws,
        "mailmessage",
        [{"row_id": "m-1", "body": {"contactRef": "c-testi", "subject": "Hei"}}],
    )
    assert first["row_ids"] == ["m-1"]
    workspace_tools.append_rows(
        CLERK,
        live.org,
        live.ws,
        "mailmessage",
        [{"row_id": "m-1", "body": {"contactRef": "c-testi", "subject": "Moi"}}],
    )
    rows = requests.get(
        f"{live.base}/v1/organisms/{live.org}/workspace/rows/mailmessage",
        params={"ws": live.ws},
        headers=live.owner_headers,
        timeout=20,
    ).json()
    assert rows.get("ok"), rows
    text = str(rows["data"])
    assert "Moi" in text and "Hei" not in text, f"one row, replaced: {rows['data']}"


def test_the_crew_definition_tool_writes_through_the_same_road(live):
    from crewaimeat.crew_def import resolve_tool

    tools = {t.name: t for t in resolve_tool("workspace_write")(CLERK, SimpleNamespace(task={"id": "t"}))}
    out = tools["write_workspace_record"].run(
        workspace="CADENCE", space="contact", fields_json='{"name": "Toinen Asiakas"}'
    )
    assert out.startswith("Created and published contact "), out
    rid = out.split("contact ", 1)[1].split(" ", 1)[0]
    assert _contact(live, rid)["name"] == "Toinen Asiakas"


# ── 2. the node enforces the scope ──────────────────────────────────────────────────────────


def test_an_agent_without_organism_write_is_refused_in_the_node_s_words(live):
    from crewaimeat import workspace_tools

    with pytest.raises(workspace_tools.WorkspaceWriteError) as exc:
        workspace_tools.write_record(READER, live.org, live.ws, "contact", {"name": "Ei Saa"}, "c-reader")
    msg = str(exc.value)
    assert "organism:write" in msg, msg
    assert "written but is not published" in msg
    assert _contact(live, "c-reader") is None, "and the owner's CRM does not show it"


# ── 3. a declined task is not done ──────────────────────────────────────────────────────────


def test_a_declined_task_ends_failed_on_the_node_with_its_reason(live):
    from crewaimeat import aimeat_crew
    from crewaimeat.crew_def import resolve_tool
    from crewaimeat.lifecycle import run_started_iso

    since = run_started_iso()
    tid = _active_task(live, CLERK, "Post this on LinkedIn")
    [decline_tool] = resolve_tool("decline")(CLERK, SimpleNamespace(task={"id": tid}))
    decline_tool.run(reason="Posting is LAHETIN's work, not the CRM's.")
    aimeat_crew._make_complete_cb(CLERK, tid, mem_key=f"crews.{CLERK}.{tid}", since=since)(None)

    data = _task(live, CLERK, tid)
    task = data.get("task") or data
    assert task["status"] == "failed", task["status"]
    assert "Declined: the request was not carried out. Posting is LAHETIN's work" in str(data)


# ── 4. the verify report is kept on the task ────────────────────────────────────────────────


def test_the_verify_report_is_a_verification_event_on_the_task(live):
    from crewaimeat import aimeat_crew
    from crewaimeat.verify_report import split_verify

    tid = _active_task(live, CLERK, "Write the brief")
    body, report = split_verify("The brief.\n\nVerify: faithfulness | score=4 | unsupported=0 | fine\n")
    assert body.strip() == "The brief."
    aimeat_crew._record_verify_report(CLERK, tid, report)

    ev = requests.get(
        f"{live.base}/v1/agents/{CLERK}/tasks/{tid}/events", headers=live.owner_headers, timeout=20
    ).json()
    assert ev.get("ok"), ev
    events = ev["data"].get("events") if isinstance(ev["data"], dict) else ev["data"]
    ver = [e for e in events or [] if e.get("type") == "verification"]
    assert ver, f"no verification event: {ev}"
    assert "score=4" in ver[-1]["message"]

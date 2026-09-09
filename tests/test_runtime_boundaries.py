"""Failure and recovery contracts at transport, lifecycle, storage and test isolation boundaries."""

import socket
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from crewaimeat.lifecycle import LifecycleCallbacks
from crewaimeat.transport import NodeTransport


def transport(session=None):
    return NodeTransport(
        serve_api=Mock(return_value=("http://127.0.0.1:40390", session) if session else None),
        reset=Mock(),
        identity_guard=Mock(return_value=True),
        read_token=Mock(return_value=("test-token", "https://node.invalid")),
        subprocess_call=Mock(),
        transient_error=Mock(return_value=False),
        warn_provenance=Mock(),
    )


def test_raw_read_recovers_after_disconnect_and_server_error():
    broken = Mock(status_code=503)
    good = Mock(status_code=200)
    session = Mock()
    session.request.side_effect = [requests.ConnectionError("disconnected"), broken, good]
    tx = transport(session)
    assert tx.request("agent", "GET", "/v1/memory/key", backoff=0) is good
    assert tx.reset.call_count == 2
    broken.close.assert_called_once()
    assert session.request.call_args.kwargs["headers"]["X-Aimeat-Agent"] == "agent"


def test_raw_mutation_is_not_repeated_after_ambiguous_disconnect():
    session = Mock()
    session.request.side_effect = requests.ConnectionError("reply lost")
    tx = transport(session)
    with pytest.raises(requests.ConnectionError):
        tx.request("agent", "POST", "/v1/storage", json={"filename": "test.png"})
    assert session.request.call_count == 1


def test_raw_write_refuses_identity_mismatch_before_sending():
    session = Mock()
    tx = transport(session)
    tx.identity_guard.return_value = False
    with pytest.raises(PermissionError):
        tx.request("agent", "POST", "/v1/storage")
    session.request.assert_not_called()


@pytest.mark.parametrize("header", ["Authorization", "X-Aimeat-Agent", "Host"])
def test_raw_caller_cannot_override_identity(header):
    tx = transport(Mock())
    with pytest.raises(ValueError, match="owns authentication"):
        tx.request("agent", "GET", "/v1/storage/key", headers={header: "other"})


def test_binary_direct_read_keeps_owner_and_never_uses_text_tunnel(monkeypatch):
    tx = transport(Mock())
    response = requests.Response()
    response.status_code = 200
    response._content = b"\x00\xff\x80PNG"
    call = Mock(return_value=response)
    monkeypatch.setattr(requests, "request", call)
    assert tx.request("agent", "GET", "/v1/storage/key", owner="owner", direct=True).content == b"\x00\xff\x80PNG"
    tx.serve_api.assert_not_called()
    tx.read_token.assert_called_once_with("agent", owner="owner")
    assert call.call_args.kwargs["headers"]["Authorization"] == "Bearer test-token"


def test_signing_identity_without_daemon_never_sends_empty_bearer(monkeypatch):
    tx = transport()
    tx.read_token.return_value = ("", "https://node.invalid")
    call = Mock()
    monkeypatch.setattr(requests, "request", call)
    with pytest.raises(PermissionError, match="serve daemon"):
        tx.request("agent", "GET", "/v1/storage/key")
    call.assert_not_called()


def test_public_discovery_document_uses_node_origin_without_credentials(monkeypatch):
    tx = transport(Mock())
    tx.read_token.return_value = ("", "https://node.invalid")
    call = Mock(return_value=Mock(status_code=200))
    monkeypatch.setattr(requests, "request", call)
    tx.request("agent", "GET", "/llms.txt")
    tx.serve_api.assert_not_called()
    assert call.call_args.args[1] == "https://node.invalid/llms.txt"
    assert call.call_args.kwargs["headers"] == {}


def test_node_inspection_rejects_foreign_urls_before_transport(monkeypatch):
    from crewaimeat import author_tool

    monkeypatch.setattr(author_tool, "_node_base", lambda *a: "https://node.invalid")
    request = Mock(
        return_value=SimpleNamespace(status_code=200, content=b"node data", encoding="utf-8", text="node data")
    )
    monkeypatch.setattr(author_tool, "_aimeat_request", request)
    tools, _ = author_tool.make_author_tools("agent", owner="owner")
    read = next(t for t in tools if t.name == "read_node_api").func
    for url in ("https://foreign.invalid/v1/agents", "//foreign.invalid/v1/agents"):
        assert read(url).startswith("ERROR:")
    request.assert_not_called()
    assert "node data" in read("https://node.invalid/v1/agents")
    assert request.call_args.args == ("agent", "GET", "/v1/agents")


@pytest.mark.parametrize("failed_write", [1, 2])
def test_publication_failure_does_not_report_success(failed_write):
    call = Mock(side_effect=[None] if failed_write == 1 else [{"key": "own"}, None])
    lc = LifecycleCallbacks(call, lambda _: None, Mock(), {})
    with pytest.raises(RuntimeError, match="publication failed"):
        lc.publish_callback("agent", "own", shared_key="shared")(SimpleNamespace(raw="deliverable"))
    assert call.call_count == failed_write


def test_completion_retry_keeps_contract_deliverable_until_acknowledged():
    call = Mock(side_effect=[None, {"state": "done"}])
    keys = {"task": "contract.key"}
    lc = LifecycleCallbacks(call, lambda _: None, Mock(), keys)
    complete = lc.complete_callback("agent", "task", "wrapper.key")
    with pytest.raises(RuntimeError, match="completion failed"):
        complete(None)
    assert keys == {"task": "contract.key"}
    complete(None)
    assert keys == {}
    assert all(c.args[2]["deliverable_key"] == "contract.key" for c in call.call_args_list)


def test_verify_lookup_error_fails_task_without_completing(monkeypatch):
    from crewaimeat import author_tool

    monkeypatch.setattr(author_tool, "get_verify_verdicts", Mock(side_effect=OSError("unreadable verdict")))
    call = Mock(return_value={"state": "failed"})
    lc = LifecycleCallbacks(call, lambda _: None, Mock(), {})
    lc.complete_callback("agent", "task", require_verify=True)(None)
    assert [c.args[1] for c in call.call_args_list] == ["aimeat_task_fail"]
    lc.mark_todos_done.assert_not_called()


def test_offline_guard_blocks_network_and_unmarked_processes():
    with pytest.raises(pytest.fail.Exception, match="Offline test attempted network"):
        socket.getaddrinfo("node.invalid", 443)
    with socket.socket() as sock, pytest.raises(pytest.fail.Exception, match="Offline test attempted network"):
        sock.connect(("127.0.0.1", 9))
    with pytest.raises(pytest.fail.Exception, match="attempted a subprocess"):
        subprocess.run([sys.executable, "-c", "pass"], check=True)


@pytest.mark.local_process
def test_child_inherits_offline_guard():
    result = subprocess.run(
        [sys.executable, "-c", "import socket; socket.getaddrinfo('node.invalid', 443)"], capture_output=True, text=True
    )
    assert result.returncode != 0
    assert "Offline child blocked socket.getaddrinfo" in result.stderr


@pytest.mark.parametrize(
    "module", ["session_store", "local_memory", "brains", "agency.chat_store", "agency.events", "agency.apps"]
)
def test_each_store_closes_its_database_handle(module):
    import importlib
    import sqlite3

    store = importlib.import_module(f"crewaimeat.{module}")
    with store._conn() as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")


def test_schema_failure_also_closes_connection(tmp_path):
    import sqlite3

    from crewaimeat._sqlite import database

    seen = []

    def invalid_schema(conn):
        seen.append(conn)
        conn.execute("THIS IS NOT SQL")

    with pytest.raises(sqlite3.OperationalError), database(str(tmp_path / "db"), invalid_schema):
        pytest.fail("bad schema must not yield")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        seen[0].execute("SELECT 1")


def test_reset_aborts_before_deletion_when_shutdown_fails(tmp_path, monkeypatch):
    from crewaimeat.agency.reset import reset_agency
    from crewaimeat.tui import actions

    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    saved = tmp_path / "agency_account.json"
    saved.write_text("{}")
    stop = Mock(side_effect=RuntimeError("shutdown failed"))
    monkeypatch.setattr(actions, "stop_fleet", stop)
    with pytest.raises(RuntimeError, match="Cannot reset"):
        reset_agency(Mock())
    stop.assert_called_once_with(strict=True)
    assert saved.exists()


def test_reset_reports_partial_cleanup(tmp_path, monkeypatch):
    from pathlib import Path

    from crewaimeat import forge
    from crewaimeat.agency.reset import reset_agency
    from crewaimeat.tui import actions

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    monkeypatch.setattr(forge, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(actions, "stop_fleet", Mock(return_value="stopped"))
    saved = tmp_path / "agency_account.json"
    saved.write_text("{}")
    unlink = Path.unlink

    def locked(path, *args, **kwargs):
        if path == saved:
            raise PermissionError("account file locked")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", locked)
    result = reset_agency(Mock())
    assert result["ok"] is False
    assert any("account file locked" in error for error in result["errors"])
    assert saved.exists()

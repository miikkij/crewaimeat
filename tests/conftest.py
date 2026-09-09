"""Pytest bootstrap for the AIMEAT crew test floor (L1/L2, deterministic, no LLM, no network).

Puts the repo root and tests/ on sys.path (so ``import crews.<x>`` and ``import crew_fixtures``
work as namespace packages) and provides dummy env so any incidental ``get_llm()`` constructs an
LLM object without a real key. These tests never hit the network: they exercise pure scaffold
functions, the guardrails, and each crew's ``build_domain`` wiring with a stub context.
"""

from __future__ import annotations

import os
import shlex
import shutil
import socket
import sys
import tempfile
from contextvars import ContextVar
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Construction-only: a dummy key lets get_llm()/LLM(...) build without raising; nothing is called.
_collection_home = tempfile.TemporaryDirectory(prefix="crewaimeat-collection-")
os.environ["AIMEAT_HOME"] = _collection_home.name
os.environ["LLM_PROVIDERS_FILE"] = str(Path(_collection_home.name) / "no-providers.json")
for _key in list(os.environ):
    if _key.endswith(("_API_KEY", "_API_TOKEN")) or _key in {
        "AIMEAT_TOKEN",
        "AIMEAT_OWNER",
        "GH_TOKEN",
        "GITHUB_TOKEN",
    }:
        os.environ.pop(_key, None)
os.environ["OPENROUTER_API_KEY"] = "test-not-used"
os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
os.environ["OTEL_SDK_DISABLED"] = "true"
os.environ["CREWAI_TELEMETRY_DISABLED"] = "true"
# Keep the default offline web-search path (SearXNG) and the OpenRouter LLM path.
os.environ.pop("USE_TAVILY", None)
os.environ.pop("USE_XAI", None)

_PROCESS_TEST_ACTIVE = False
_PROCESS_ALLOWED = False
_NODE_SYNTAX_ALLOWED = False


def _audit_subprocess(event, args):
    if event in {"socket.sendto", "socket.sendmsg"} and _PROCESS_TEST_ACTIVE:
        pytest.fail("Offline test attempted datagram network access")
    if event == "subprocess.Popen" and _PROCESS_TEST_ACTIVE:
        executable, argv, cwd, env = args
        if isinstance(argv, str):  # Windows reports the serialized command line, not argv.
            argv = [arg.strip('"') for arg in shlex.split(argv, posix=False)]
        if _NODE_SYNTAX_ALLOWED and len(argv) == 3 and argv[1] == "--check":
            resolved = shutil.which(executable or argv[0])
            node = shutil.which("node")
            if resolved and node and Path(resolved).resolve() == Path(node).resolve():
                return  # Node parses this file without executing its code.
        if not _PROCESS_ALLOWED:
            pytest.fail("Offline test attempted a subprocess; mock it or declare @pytest.mark.local_process")
        if Path(executable or argv[0]).resolve() != Path(sys.executable).resolve():
            pytest.fail("local_process permits only the test Python interpreter")
        if any(arg in {"-I", "-S", "-E"} for arg in argv):
            pytest.fail("local_process cannot disable the inherited offline guard")
        child_env = os.environ if env is None else env
        if child_env.get("CREWAIMEAT_OFFLINE_TEST") != "1" or str(
            ROOT / "tests" / "offline_child"
        ) not in child_env.get("PYTHONPATH", ""):
            pytest.fail("local_process must inherit the offline test environment")


sys.addaudithook(_audit_subprocess)


@pytest.fixture(autouse=True)
def offline_processes(monkeypatch, request):
    global _PROCESS_TEST_ACTIVE, _PROCESS_ALLOWED, _NODE_SYNTAX_ALLOWED
    monkeypatch.setenv("CREWAIMEAT_OFFLINE_TEST", "1")
    monkeypatch.setenv("WEB_SEARCH", "ddg")
    monkeypatch.setenv(
        "PYTHONPATH", str(ROOT / "tests" / "offline_child") + os.pathsep + os.environ.get("PYTHONPATH", "")
    )
    _PROCESS_ALLOWED = request.node.get_closest_marker("local_process") is not None
    _NODE_SYNTAX_ALLOWED = request.node.get_closest_marker("node_syntax") is not None
    _PROCESS_TEST_ACTIVE = True
    try:
        yield
    finally:
        _PROCESS_TEST_ACTIVE = False
        _PROCESS_ALLOWED = False
        _NODE_SYNTAX_ALLOWED = False


@pytest.fixture(autouse=True)
def offline_runtime(tmp_path, monkeypatch, request):
    """Each test owns its connector state. Unexpected network calls fail even inside broad catches.

    In-process ASGI clients do not need network access. A test with a real local server must declare
    `@pytest.mark.loopback`; that exception permits loopback only, never an external node or model.
    """
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path / "aimeat"))
    monkeypatch.setenv("LLM_PROVIDERS_FILE", str(tmp_path / "no-providers.json"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-not-used")
    allow_loopback = request.node.get_closest_marker("loopback") is not None
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex
    getaddrinfo = socket.getaddrinfo
    socketpair = socket.socketpair
    in_socketpair = ContextVar("offline_socketpair", default=False)

    def check(address):
        if isinstance(address, tuple):
            host = address[0]
            # Windows implements the stdlib socketpair used by asyncio with a private TCP pair.
            if in_socketpair.get() and host in ("127.0.0.1", "::1"):
                return
            if allow_loopback and host in ("127.0.0.1", "::1", "localhost"):
                return
            pytest.fail(
                f"Offline test attempted network access to {host!r}; mock the transport or mark a loopback test"
            )

    def guarded_connect(sock, address):
        check(address)
        return connect(sock, address)

    def guarded_connect_ex(sock, address):
        check(address)
        return connect_ex(sock, address)

    def guarded_dns(host, *args, **kwargs):
        if host not in ("127.0.0.1", "::1", "localhost"):
            check((host,))
        return getaddrinfo(host, *args, **kwargs)

    def local_socketpair(*args, **kwargs):
        token = in_socketpair.set(True)
        try:
            return socketpair(*args, **kwargs)
        finally:
            in_socketpair.reset(token)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_dns)
    monkeypatch.setattr(socket, "socketpair", local_socketpair)


@pytest.fixture(autouse=True)
def offline_search_discovery(monkeypatch):
    """Crew construction chooses a deterministic search backend without probing a local service."""
    from crewaimeat import crew
    from crewaimeat.agency import cockpit

    monkeypatch.setattr(crew, "_searxng_reachable", lambda: False)
    monkeypatch.setattr(cockpit, "_ollama_probe", lambda: (False, []))


@pytest.fixture
def no_pipeline_memory(monkeypatch):
    """Structural crew tests exercise wiring with the optional memory service unavailable."""
    from crewaimeat import forge, pipeline_memory

    def unavailable(*args, **kwargs):
        raise RuntimeError("offline structural fixture: no embedding service")

    monkeypatch.setattr(pipeline_memory, "resolve_embedder", unavailable)
    monkeypatch.setattr(pipeline_memory, "_OPEN", {})
    monkeypatch.setattr(forge, "_FORGE_STORE", [])

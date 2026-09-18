"""Connecting an agent to an instance: the connector's device flow, started by agency 2.0.

    aimeat connect --url <instance> --owner <user> --agent <name> --mode task-runner

`--mode task-runner` is asked for HERE, at registration, because that is the one place the OWNER decides
it: the node records the requested mode and the person approves it in the same consent (proven
2026-09-18). The runtime never writes the mode itself (commit 1db112d) — a task-runner is what lets the
agent start its tasks without the person pressing Start each time.

The connector sends no scopes, so the node grants its four defaults unless the person ticks more on the
consent page. `REQUIRED_SCOPES` is what the scaffold needs beyond those; the UI lists them next to the
code, and the health view flags an agent that was approved without them.

After approval the serve daemon is restarted: the connector loads its agent set only at startup
(`serve_guard.restart_serve`, which touches THIS home's daemon only).
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time

from crewaimeat.agency2 import engine

# Beyond the node defaults (memory:read/write/delete, catalogue:read). agent:write is what the scaffold's
# identity push (tags) needs — measured: without it `aimeat_agent_tags_set` answers SCOPE_DENIED.
REQUIRED_SCOPES = ("agent:write",)

_CODE_RE = re.compile(r"Verification code:\s*([A-Z0-9]{3,}-[A-Z0-9]{3,})")
_URL_RE = re.compile(r"Open\s+(https?://\S+/v1/agents/verify)\S*")

_LOCK = threading.Lock()
_STATE: dict[str, dict] = {}


def state(name: str) -> dict | None:
    with _LOCK:
        s = _STATE.get(name)
        return dict(s) if s else None


def _set(name: str, **kw) -> None:
    with _LOCK:
        _STATE.setdefault(name, {}).update(kw)


def parse(text: str) -> dict:
    """code / verify URL / approved / token-stored out of the connector's output (tested verbatim)."""
    code = _CODE_RE.search(text)
    url = _URL_RE.search(text)
    return {
        "code": code.group(1) if code else None,
        "verify_url": url.group(1) if url else None,
        "approved": "Approved!" in text,
        "stored": "Token stored" in text,
    }


def start(name: str, instance_url: str, owner: str, *, on_done=None) -> dict:
    """Start the device flow in the background. Returns the state row; poll `state(name)`."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        raise RuntimeError("connect.start spawns the connector — never under pytest")
    cur = state(name)
    if cur and cur.get("status") in ("starting", "waiting"):
        return cur
    argv = engine.connector_argv(
        "connect", "--url", instance_url, "--owner", owner, "--agent", name, "--mode", "task-runner"
    )
    _set(
        name,
        status="starting",
        code=None,
        verify_url=None,
        output="",
        started=time.time(),
        error=None,
        instance=instance_url,
        owner=owner,
    )
    proc = subprocess.Popen(
        argv,
        env=engine.child_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    def _pump() -> None:
        from crewaimeat.agency2 import paths

        buf = ""
        t0 = time.time()
        with open(paths.logs_dir() / f"connect-{name}.log", "a", encoding="utf-8") as log:
            for line in proc.stdout:  # type: ignore[union-attr]
                log.write(f"+{time.time() - t0:6.1f}s {line}")  # when each line came is measured, not assumed
                log.flush()
                buf += line
                p = parse(buf)
                if p["code"] and not (state(name) or {}).get("code"):
                    _set(
                        name,
                        status="waiting",
                        code=p["code"],
                        verify_url=p["verify_url"] or f"{instance_url}/v1/agents/verify",
                        code_after_s=round(time.time() - t0, 1),
                    )
                _set(name, output=buf[-4000:])
        rc = proc.wait()
        p = parse(buf)
        if rc == 0 and p["stored"]:
            _set(name, status="approved", finished=time.time())
        else:
            # Never guess ("maybe already registered"): the connector's own words are the reason.
            _set(name, status="failed", error=_tail(buf) or f"connector exited with {rc}", finished=time.time())
        if on_done:
            try:
                on_done(name, state(name))
            except Exception as exc:  # noqa: BLE001 — surfaced on the row, never swallowed
                _set(name, status="failed", error=f"after approval: {type(exc).__name__}: {exc}")

    threading.Thread(target=_pump, name=f"connect-{name}", daemon=True).start()
    return state(name) or {}


def _tail(text: str, n: int = 12) -> str:
    lines = [
        ln for ln in text.splitlines() if ln.strip() and "ExperimentalWarning" not in ln and "trace-warnings" not in ln
    ]
    return "\n".join(lines[-n:])

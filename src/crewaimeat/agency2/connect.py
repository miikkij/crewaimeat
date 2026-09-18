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

# Beyond the node defaults (memory:read/write/delete, catalogue:read):
#   agent:write     the scaffold's identity push (tags) — measured: without it tags_set answers SCOPE_DENIED
#   task:write      creating the agent's own `agent_task` schedule (services/schedule-gate.ts)
#   workflow:read   listing schedules (GET /v1/schedules)
#   wallet:read     reading what the agents spent (GET /v1/ledger/usage, routes/ledger.ts)
REQUIRED_SCOPES = ("agent:write", "task:write", "workflow:read", "wallet:read")

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
        "already": "Already connected!" in text,
    }


def _token_path(name: str, owner: str):
    from crewaimeat.agency2 import paths

    return paths.aimeat_home() / "tokens" / f"{name}@{owner}.token"


def _set_aside(name: str, owner: str):
    """Move the stored credential out of the connector's sight, so it runs a NEW device flow.

    The connector has no "force": with a valid token it answers "Already connected!" and exits, and a
    reconnect is exactly the case where the old token is valid but carries the wrong scopes (scopes are
    baked into the token at approval). `tokens/.replaced/` is not read — the connector lists only
    `*.token` files directly in `tokens/` (cli/connect/keychain.ts). Returns where it went, or None."""
    src = _token_path(name, owner)
    if not src.is_file():
        return None
    dst = src.parent / ".replaced" / f"{src.stem}.{int(time.time())}.token"
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.replace(dst)
    return dst


def start(name: str, instance_url: str, owner: str, *, on_done=None, fresh: bool = False) -> dict:
    """Start the device flow in the background. Returns the state row; poll `state(name)`.

    fresh=True (a reconnect) sets the stored credential aside first, so the person approves again —
    and puts it back if the new flow does not end with a new token."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        raise RuntimeError("connect.start spawns the connector — never under pytest")
    cur = state(name)
    if cur and cur.get("status") in ("starting", "waiting"):
        return cur
    aside = _set_aside(name, owner) if fresh else None
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
        if rc == 0 and (p["stored"] or (p["already"] and not fresh)):
            _set(name, status="approved", finished=time.time())
        else:
            if aside is not None and not _token_path(name, owner).is_file():
                aside.replace(_token_path(name, owner))  # the old key is better than none
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

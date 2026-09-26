"""Reading the nodes — through this home's serve daemon, as one of the person's agents.

agency 2.0 holds no owner credential: everything it knows about an instance it asks AS AN AGENT, over
`POST /local/call/<tool>` with `X-Aimeat-Agent` (the same door every crew uses). `aimeat_agents_list`
on an agent token returns the owner's whole roster, including agents made on the node that no machine
runs (proven 2026-09-18, spec §7). The only unauthenticated call is `/v1/health`.

A refusal is returned as a refusal, never as an empty list: `Refused` carries the node's HTTP status and
error code, so the health view can say "the key for X was revoked" instead of "X has no agents".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

from crewaimeat.agency2 import paths
from crewaimeat.spawn_state import serve_auth_headers


@dataclass
class Refused(Exception):
    status: int
    code: str
    message: str

    def __str__(self) -> str:
        return f"HTTP {self.status} {self.code}: {self.message}"


class NoDaemon(Exception):
    """This home has no serve daemon running (serve.json missing or its port does not answer)."""


def serve_doc() -> dict | None:
    return paths.read_json(paths.aimeat_home() / "serve.json", None)


def serve_port() -> int | None:
    doc = serve_doc()
    return int(doc["port"]) if doc and doc.get("port") else None


def daemon_headers(ident: str) -> dict[str, str]:
    """Who is asking, and the secret the daemon made at its current start (read fresh: it changes with
    every start)."""
    return {"X-Aimeat-Agent": ident, **serve_auth_headers(serve_doc())}


def served_agents() -> dict[str, dict]:
    """name -> {gaii, node_url, transport} for every agent the daemon carries."""
    doc = serve_doc() or {}
    out: dict[str, dict] = {}
    for a in doc.get("agents") or []:
        name = a.get("agent") or str(a.get("gaii", "")).split("#")[0]
        if name:
            out[name] = {"gaii": a.get("gaii"), "node_url": a.get("node_url"), "transport": a.get("transport")}
    return out


def call(agent: str, tool: str, args: dict | None = None, *, timeout: float = 30) -> Any:
    """Call a connector tool as `agent` (bare name or GAII). Returns the node's `data` (or the body)."""
    port = serve_port()
    if not port:
        raise NoDaemon("no serve daemon for this home")
    ident = served_agents().get(agent, {}).get("gaii") or agent
    try:
        r = requests.post(
            f"http://127.0.0.1:{port}/local/call/{tool}",
            json=args or {},
            headers=daemon_headers(ident),
            timeout=timeout,
        )
    except requests.ConnectionError as exc:
        raise NoDaemon(f"serve daemon on port {port} does not answer") from exc
    try:
        body = r.json()
    except ValueError:
        body = {"error": {"code": "NOT_JSON", "message": r.text[:300]}}
    if r.status_code >= 400 or (isinstance(body, dict) and body.get("ok") is False):
        err = (body or {}).get("error") or {}
        raise Refused(r.status_code, str(err.get("code") or "ERROR"), str(err.get("message") or r.text[:300]))
    return body.get("data", body) if isinstance(body, dict) else body


def health(url: str, *, timeout: float = 8) -> dict:
    """{ok, node_id, detail} from GET <url>/v1/health — the one call that needs no identity."""
    try:
        r = requests.get(f"{url}/v1/health", timeout=timeout)
        body = r.json()
        return {
            "ok": bool(body.get("ok")) and r.status_code == 200,
            "node_id": body.get("node"),
            "detail": body.get("data"),
        }
    except (requests.RequestException, ValueError) as exc:
        from crewaimeat.agency2 import problems

        return {"ok": False, "node_id": None, "detail": problems.say(exc, "instance.health", kind="unreachable")}


def roster(agent: str) -> list[dict]:
    data = call(agent, "aimeat_agents_list")
    return list((data or {}).get("agents", data) or []) if isinstance(data, (dict, list)) else []


def tasks(agent: str, *, per_page: int = 20) -> list[dict]:
    data = call(agent, "aimeat_task_list", {"per_page": per_page})
    if isinstance(data, dict):
        return list(data.get("tasks") or data.get("items") or [])
    return list(data or [])


def runtime_report(agent: str) -> dict | None:
    """`crews.runtime.<agent>` — what the runtime last loaded and whether it was valid (json_agent)."""
    try:
        data = call(agent, "aimeat_memory_read", {"key": f"crews.runtime.{agent}"})
    except Refused as exc:
        if exc.code in ("NOT_FOUND", "MEMORY_NOT_FOUND"):
            return None
        raise
    if isinstance(data, dict):
        v = data.get("value", data)
        return v if isinstance(v, dict) else None
    return None


def deliverable_text(agent: str, key: str, *, limit: int = 6000) -> str | None:
    """What a finished task produced (its deliverable record), as text for the person to read.

    A failure to read it is said in the text rather than hidden: the run is done either way, and the
    person should see why its result is not shown."""
    try:
        data = call(agent, "aimeat_memory_read", {"key": key})
    except (Refused, NoDaemon) as exc:
        from crewaimeat.agency2 import problems

        return "(" + problems.say(exc, "deliverable", kind="read") + ")"
    v = data.get("value", data) if isinstance(data, dict) else data
    if isinstance(v, dict):
        v = v.get("body") or v.get("content") or v.get("text") or v
    text = v if isinstance(v, str) else __import__("json").dumps(v, ensure_ascii=False, indent=1)
    return text[:limit]


def rest_get(agent: str, path: str, params: dict | None = None, *, timeout: float = 30) -> Any:
    """GET a node `/v1/...` route AS `agent`, through the daemon's forward proxy (the same door
    `transport` uses for `_aimeat_rest`). A refusal raises `Refused`, never returns empty."""
    port = serve_port()
    if not port:
        raise NoDaemon("no serve daemon for this home")
    ident = served_agents().get(agent, {}).get("gaii") or agent
    try:
        r = requests.get(
            f"http://127.0.0.1:{port}{path}", params=params or {}, headers=daemon_headers(ident), timeout=timeout
        )
    except requests.ConnectionError as exc:
        raise NoDaemon(f"serve daemon on port {port} does not answer") from exc
    try:
        body = r.json()
    except ValueError:
        body = {"error": {"code": "NOT_JSON", "message": r.text[:300]}}
    if r.status_code >= 400 or (isinstance(body, dict) and body.get("ok") is False):
        err = (body or {}).get("error") or {}
        raise Refused(r.status_code, str(err.get("code") or "ERROR"), str(err.get("message") or r.text[:300]))
    return body.get("data", body) if isinstance(body, dict) else body

"""Node transport with explicit runtime hooks; no CrewAI import or fleet startup."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass

import requests


@dataclass
class NodeTransport:
    serve_api: Callable
    reset: Callable
    identity_guard: Callable
    read_token: Callable | None
    subprocess_call: Callable
    transient_error: Callable
    warn_provenance: Callable

    def request(
        self,
        agent_name: str,
        method: str,
        path: str,
        *,
        owner: str | None = None,
        direct: bool = False,
        timeout: float = 60,
        retries: int | None = None,
        backoff: float = 1.5,
        **kwargs,
    ) -> requests.Response:
        """Return a node response, including text and streamed binary bodies.

        Binary downloads opt into direct bearer transport because the loopback tunnel is text-only.
        Reads retry transport failures; mutations default to one attempt because their outcome may
        be ambiguous after a connection loss. The caller owns and closes streamed responses.
        """
        method = method.upper()
        public_document = method in {"GET", "HEAD"} and path in {"/", "/llms.txt"}
        if (not path.startswith("/v1/") and not public_document) or "\\" in path or "\r" in path or "\n" in path:
            raise ValueError("node request requires a /v1/ path or a public discovery document")
        attempts = retries if retries is not None else (3 if method in {"GET", "HEAD"} else 1)
        if attempts < 1:
            raise ValueError("retries must be positive")
        if not direct and not self.identity_guard(agent_name, method):
            raise PermissionError("node proxy identity mismatch")
        headers = dict(kwargs.pop("headers", {}))
        if any(k.lower() in {"authorization", "x-aimeat-agent", "host"} for k in headers):
            raise ValueError("node transport owns authentication and attribution headers")
        for attempt in range(attempts):
            api = None if direct or public_document else self.serve_api()
            try:
                if api is not None:
                    base, session = api
                    response = session.request(
                        method,
                        f"{base}{path}",
                        headers={**headers, "X-Aimeat-Agent": agent_name},
                        timeout=timeout,
                        **kwargs,
                    )
                else:
                    if self.read_token is None:
                        raise PermissionError("no daemon and no token reader")
                    token, base = self.read_token(agent_name, owner=owner)
                    if not token and not public_document:
                        raise PermissionError("this agent requires its serve daemon; no bearer token is available")
                    auth = {} if public_document else {"Authorization": f"Bearer {token}"}
                    response = requests.request(
                        method,
                        f"{base.rstrip('/')}{path}",
                        headers={**headers, **auth},
                        timeout=timeout,
                        **kwargs,
                    )
            except requests.RequestException as exc:
                self.reset()
                if attempt + 1 == attempts:
                    print(f"[{agent_name}] {method} {path} failed after {attempts} attempt(s): {exc}", file=sys.stderr)
                    raise
                time.sleep(backoff * 2**attempt)
                continue
            if response.status_code >= 500 and attempt + 1 < attempts:
                response.close()
                self.reset()
                time.sleep(backoff * 2**attempt)
                continue
            if response.status_code >= 400:
                print(f"[{agent_name}] {method} {path}: HTTP {response.status_code}", file=sys.stderr)
            return response
        raise RuntimeError("node request exhausted its attempts")

    def call(
        self,
        agent_name: str,
        tool: str,
        payload: dict,
        *,
        retries: int = 3,
        backoff: float = 1.5,
        quiet: bool = False,
        return_error: bool = False,
    ) -> dict | None:
        """Deterministic AIMEAT tool call (no LLM).

        Primary path: POST /local/call/<tool> on the shared loopback serve daemon (same tool name +
        JSON input as `connect call`; returns the envelope's data). Fallback when no daemon exists:
        the legacy one-shot `aimeat connect call` subprocess.

        RESILIENCE: a transient TRANSPORT failure (tunnel reconnecting, connection dropped, 5xx) is
        RETRIED up to `retries` times with exponential backoff — the serve daemon is reset between tries
        so the next attempt re-discovers/re-establishes it. Tool-level errors (e.g. a key that isn't
        there yet) are NOT retried — they return None immediately so "not found yet" polls stay cheap.

        `return_error=True` hands back the node's own envelope on a settled tool error instead of None, so
        a caller can tell "there is nothing there" apart from "the answer never arrived". Without it the
        two are indistinguishable: `list_memory` reported "No memory keys found under prefix 'news.'" when
        the node had in fact refused a 25 MB answer, and a model reads that as the upstream stage not
        having run — which is how a fetch failure turns into a fabricated article."""
        for attempt in range(retries):
            api = self.serve_api()
            if api is None:
                data = self.subprocess_call(agent_name, tool, payload)
                if "ai_provenance" in payload:
                    self.warn_provenance(agent_name, tool, data)
                return data
            base, session = api
            last = attempt + 1 >= retries
            try:
                r = session.post(
                    f"{base}/local/call/{tool}",
                    json=payload,
                    headers={"X-Aimeat-Agent": agent_name},
                    timeout=90,
                )
            except requests.RequestException as exc:
                self.reset()  # daemon gone mid-flight -> re-discover / auto-restart it on the next try
                if last:
                    print(
                        f"[{agent_name}] {tool} loopback POST failed ({exc}); gave up after {retries} tries",
                        file=sys.stderr,
                    )
                    return None
                print(f"[{agent_name}] {tool} POST failed ({exc}); retry {attempt + 1}/{retries}", file=sys.stderr)
                time.sleep(backoff * (2**attempt))
                continue
            try:
                body = r.json()
            except ValueError:
                print(
                    f"[{agent_name}] {tool} returned non-JSON (HTTP {r.status_code}): {r.text[:120]}", file=sys.stderr
                )
                return None
            if not isinstance(body, dict) or not body.get("ok"):
                err = (body or {}).get("error") if isinstance(body, dict) else None
                if self.transient_error(err) and not last:
                    self.reset()
                    print(
                        f"[{agent_name}] {tool} transient failure ({err}); retry {attempt + 1}/{retries}",
                        file=sys.stderr,
                    )
                    time.sleep(backoff * (2**attempt))
                    continue
                if not quiet:  # quiet=True for EXPECTED probe failures (e.g. listing an org you don't serve)
                    print(f"[{agent_name}] {tool} failed: {err or f'HTTP {r.status_code}'}", file=sys.stderr)
                if return_error and isinstance(body, dict):
                    return dict(body, http_status=r.status_code)
                return None
            # NB we return `data` and DISCARD `body["meta"]` — the same envelope-carrier discard that cost
            # the connector its inbound provenance (it unwrapped `resp.data ?? resp`, binning the envelope
            # that GET /v1/memory/:key serves the record on; fixed connector-side in 2.5.0 by folding the
            # block into the result). Nothing here needs `meta` today, and the block now arrives inside
            # `data`. If something ever DOES need an envelope-level field, add it explicitly — reading it
            # off a return value that never carried it is the bug, one layer up.
            data = body.get("data")
            if "ai_provenance" in payload:  # only when WE declared — a read never carries one outbound
                self.warn_provenance(agent_name, tool, data)
            return data
        return None

    def rest(
        self,
        agent_name: str,
        method: str,
        path: str,
        body: dict | None = None,
        *,
        retries: int = 3,
        backoff: float = 1.5,
        raw: bool = False,
        return_error: bool = False,
    ) -> dict | None:
        """Deterministic REST call on the agent's behalf (a `/v1/...` node route). No LLM.

        The sibling of `_aimeat_call` for routes the connector publishes no MCP tool for — today that is
        PATCH /v1/memory/:key (the RFC 7386 merge patch six crews use to share one status record). Same
        transport and the same retry policy: the loopback serve daemon proxies ANY /v1 path over its
        persistent tunnel, so an in-fleet call costs one keep-alive request.

        Fallback when no daemon is running: a DIRECT authed request with the agent's stored token. That
        path deliberately differs from `_aimeat_call`'s subprocess fallback — `aimeat connect call` only
        reaches TOOLS, and a REST route has none — and it is the one that keeps off-fleet scripts honest
        (the connector tool surface returns empty off-fleet; a direct authed call really works or really
        fails).

        Returns the envelope's `data` on success, None on failure (logged loud).

        `return_error=True` hands the caller the node's own ENVELOPE on a verdict (a 4xx, or `ok:false`)
        instead of None, transport failures still being None. Use it where the caller must SAY what the
        node said rather than infer it: `call_app_tool` used to read None, look at the tool's price, and
        announce a payment wall — so the app's own owner was told their tool was priced and foreign when
        the node had actually answered TOOL_NOT_INVOKABLE (measured 2026-09-03). The retry policy is
        unchanged; only what a settled failure gives back."""
        if not self.identity_guard(agent_name, method):
            return None
        for attempt in range(retries):
            last = attempt + 1 >= retries
            api = self.serve_api()
            try:
                if api is not None:
                    base, session = api
                    r = session.request(
                        method, f"{base}{path}", json=body, headers={"X-Aimeat-Agent": agent_name}, timeout=60
                    )
                else:
                    if self.read_token is None:
                        print(f"[{agent_name}] {method} {path}: no daemon and no token reader", file=sys.stderr)
                        return None
                    token, node_url = self.read_token(agent_name)
                    r = requests.request(
                        method,
                        f"{node_url.rstrip('/')}{path}",
                        json=body,
                        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                        timeout=60,
                    )
            except requests.RequestException as exc:
                self.reset()  # daemon gone mid-flight -> re-discover it on the next try
                if last:
                    print(
                        f"[{agent_name}] {method} {path} failed ({exc}); gave up after {retries} tries", file=sys.stderr
                    )
                    return None
                print(f"[{agent_name}] {method} {path} failed ({exc}); retry {attempt + 1}/{retries}", file=sys.stderr)
                time.sleep(backoff * (2**attempt))
                continue
            try:
                env = r.json()
            except ValueError:
                print(f"[{agent_name}] {method} {path} returned non-JSON (HTTP {r.status_code})", file=sys.stderr)
                return None
            if r.status_code >= 400 or (not raw and not (isinstance(env, dict) and env.get("ok"))):
                err = (env or {}).get("error") if isinstance(env, dict) else None
                # A 5xx / tunnel hiccup is worth another try; a 400/403 (malformed patch, missing scope)
                # is the node's verdict and must fail fast and LOUD — it is a bug in us, not weather.
                if (r.status_code >= 500 or self.transient_error(err)) and not last:
                    self.reset()
                    print(
                        f"[{agent_name}] {method} {path} transient ({err}); retry {attempt + 1}/{retries}",
                        file=sys.stderr,
                    )
                    time.sleep(backoff * (2**attempt))
                    continue
                print(f"[{agent_name}] {method} {path} failed: HTTP {r.status_code} {err or ''}", file=sys.stderr)
                if return_error and isinstance(env, dict):
                    return dict(env, http_status=r.status_code)
                return None
            return env if raw else env.get("data")
        return None

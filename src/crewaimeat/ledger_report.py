"""report_llm_usage — send a DIRECT (non-CrewAI) model call's usage to the AIMEAT ledger.

crewfive makes some model calls straight to OpenRouter via requests.post — image generation
(seedream_gen) and vision describe / document read (vision, image_contract, browser_tool) — instead
of through CrewAI's LLM. Those calls bypass aimeat-crewai's event-bus usage hook, so their tokens +
cost would be missing from /v1/ledger/usage. Each such call site calls report_llm_usage() after a
successful response to close that gap.

Add `"usage": {"include": True}` to the OpenRouter request body so the response's `usage` carries the
authoritative cost, and pass that `usage` dict here. Best-effort: never raises (metering must not break
a tool), and skips silently when it can't resolve the agent or there are no tokens to report. Reuses the
same loopback-serve / token path as the rest of crewfive.
"""

from __future__ import annotations

import sys
from typing import Any

import requests


def _resolve_agent(agent: str | None) -> str | None:
    """The explicit agent, else the AIMEAT agent whose crew kickoff is running on this context —
    aimeat-crewai's usage_run sets that in the fleet host, so call sites that don't thread `agent`
    (image_contract, browser_tool) still attribute correctly. The contextvar is private, so it is
    read defensively: if it ever moves, this degrades to "skip" rather than crashing."""
    if agent:
        return agent
    try:
        from aimeat_crewai.usage_telemetry import _current_agent_name

        return _current_agent_name.get()
    except Exception:  # noqa: BLE001
        return None


def _resolve_run_id() -> str | None:
    """The AIMEAT run/task id of the crew kickoff on this context — aimeat-crewai's usage_run sets it,
    so a direct-call row also shows under /v1/ledger/usage/runs per deliverable. None on the record/idle
    paths (no kickoff): the row still aggregates fine in /v1/ledger/usage, it just has no run to drill
    into. Private contextvar, read defensively — degrade to None rather than crash if it ever moves."""
    try:
        from aimeat_crewai.usage_telemetry import _current_run_id

        return _current_run_id.get()
    except Exception:  # noqa: BLE001
        return None


def report_llm_usage(
    model: str,
    usage: dict | None,
    *,
    agent: str | None = None,
    provider: str = "openrouter",
) -> None:
    """POST one `llm_call` telemetry event (model + tokens + optional cost) to the node ledger for a
    direct model call. No-op when there are no tokens or the agent can't be resolved."""
    try:
        u = usage if isinstance(usage, dict) else {}
        pt = int(u.get("prompt_tokens") or 0)
        ct = int(u.get("completion_tokens") or 0)
        if pt <= 0 and ct <= 0:
            return  # nothing metered to report
        who = _resolve_agent(agent)
        if not who:
            return  # can't attribute -> skip rather than mis-post to the wrong agent
        data: dict[str, Any] = {"model": model, "prompt_tokens": pt, "completion_tokens": ct, "provider": provider}
        cost = u.get("cost")
        if isinstance(cost, (int, float)) and cost >= 0:
            data["cost_usd"] = float(cost)  # authoritative provider cost wins over the node's table
        run_id = _resolve_run_id()  # nice-to-have: ties the row to its deliverable at /v1/ledger/usage/runs
        if run_id:
            data["run_id"] = run_id
        payload: dict[str, Any] = {"type": "llm_call", "data": data}
        if run_id:
            payload["task_id"] = run_id  # node falls back task_id -> run_id (matches the package hook)

        def _log(resp, via: str) -> None:
            """Turn the (previously silent) telemetry result LOUD: a non-2xx from the node — the most
            likely reason usage never lands in /v1/ledger/usage — is now visible in the fleet log."""
            code = getattr(resp, "status_code", None)
            if code is None or code >= 300:
                body = getattr(resp, "text", "")[:200] if resp is not None else "no response"
                print(f"[ledger] {who} telemetry REJECTED via {via}: HTTP {code} {body}", file=sys.stderr)
            else:
                extra = f" cost=${data['cost_usd']}" if "cost_usd" in data else ""
                print(f"[ledger] {who} metered {model} pt={pt} ct={ct}{extra} via {via}", file=sys.stderr)

        from crewaimeat.aimeat_crew import _serve_api  # lazy: avoid an import cycle

        api = _serve_api()
        if api is not None:
            base, session = api
            _log(
                session.post(
                    f"{base}/v1/agents/{who}/telemetry",
                    json=payload,
                    headers={"X-Aimeat-Agent": who},
                    timeout=15,
                ),
                "loopback serve",
            )
            return
        # No loopback serve daemon — fall back to a direct call with the agent's own bearer token.
        from crewaimeat.generator_tool import _discover_owner, _token

        tok, url = _token(who, _discover_owner(who))
        if tok and url:
            _log(
                requests.post(
                    f"{url.rstrip('/')}/v1/agents/{who}/telemetry",
                    json=payload,
                    headers={"Authorization": f"Bearer {tok}"},
                    timeout=15,
                ),
                "agent token",
            )
    except Exception as exc:  # noqa: BLE001 — metering must never break the calling tool
        print(f"[ledger] usage report failed ({model}): {exc!r}", file=sys.stderr)

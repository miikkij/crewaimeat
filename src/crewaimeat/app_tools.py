"""app_tools — let a crewai agent read the app-tool catalog and CALL an app-tool.

This is item **C** from the division-of-labour doc (`doc-mtgwbuadi9wo`): the one genuinely new
crewaimeat capability the whole app-tool vision needs. The node hosts app-tools (the callable
functions single-file apps publish); a crewai agent should be able to find one, read how it is
called and what it does, and invoke it — the same tools a human, an app, or a chat agent can call.

VERIFIED AGAINST THE LIVE NODE (2026-08-31), not assumed:
- `GET /v1/commerce/tools` is the catalog. It is NOT the `{ok, data}` envelope, so it is read with
  `_aimeat_rest(..., raw=True)`. Each entry carries `sku`, `app` (owner/appId), `ownerName`, `name`,
  `description`, `inputSchema` (how to call it), `fulfillment` (`call` | `task`), `price`, and
  `webmcp.invoke` (the direct invoke URL).
- Invoking hits the tool's own `webmcp.invoke` path, which DOES answer in the `{ok, data}` envelope,
  so `_aimeat_rest` returns its data; the tool's real return sits at `data.result`.
- **Same-owner tools run FREE.** A tool owned by this agent's owner — priced or not — returned
  `metered: false` and its real result on a same-owner token. The price is what OTHER owners pay.
  A foreign PRICED tool answers 402 (payment is the invocation); we surface that honestly rather
  than pretend it ran, and the checkout path is a later build.

So `free_for_you` in the listing is computed, not guessed: the tool's owner GHII compared to this
agent's owner. The transport is `_aimeat_rest`, which goes through the loopback tunnel in-fleet and a
direct authed request off it — the agent reaching the node for its own call is ordinary outbound.
"""

from __future__ import annotations

import json
from typing import Any

_CATALOG_PATH = "/v1/commerce/tools"


def _owner_of(agent_name: str) -> str:
    """This agent's owner GHII, from its token filename `<agent>@<owner>.token`. Deterministic and
    always present in-fleet; empty string if it cannot be found (the free hint then just abstains)."""
    try:
        from crewaimeat._home import aimeat_home

        for f in (aimeat_home() / "tokens").glob(f"{agent_name}@*.token"):
            return f.stem.split("@", 1)[1]
    except Exception:  # noqa: BLE001 — the hint is a convenience, never a hard dependency
        pass
    return ""


def _catalog(agent_name: str) -> list[dict]:
    from crewaimeat.aimeat_crew import _aimeat_rest

    body = _aimeat_rest(agent_name, "GET", _CATALOG_PATH, raw=True)
    tools = (body or {}).get("tools") if isinstance(body, dict) else None
    return tools if isinstance(tools, list) else []


def _tool_owner(entry: dict) -> str:
    """The owner GHII behind a catalog entry. `ownerName` is either a GHII (`happydude500001`) or a
    GAII whose owner is after the `#` (`claude-desktop-home-mcp#happydude500001`)."""
    return str(entry.get("ownerName") or "").split("#")[-1]


def _free_for(entry: dict, owner: str) -> bool:
    return bool(owner) and _tool_owner(entry) == owner


def _find(tools: list[dict], ref: str) -> dict | None:
    """Resolve a user-given reference to one catalog entry. Accepts the full sku, `app:tool`, or a
    bare tool name — but only when it is UNAMBIGUOUS, because calling the wrong tool silently is worse
    than saying 'be more specific'."""
    ref = ref.strip()
    exact = [t for t in tools if t.get("sku") == ref]
    if exact:
        return exact[0]
    cands = [
        t
        for t in tools
        if ref in (t.get("sku", ""), f"{t.get('app')}:{t.get('name')}", t.get("name", ""))
        or ref
        and ref in t.get("sku", "")
    ]
    return cands[0] if len(cands) == 1 else None


def make_app_tools(agent_name: str, ctx: Any = None) -> list:
    """Two crewai tools: find app-tools, and call one. Bound to `agent_name`'s identity, because the
    call spends that agent's family's free access (or hits the same 402 a stranger would)."""
    from crewai.tools import tool

    owner = _owner_of(agent_name)

    @tool("list_app_tools")
    def list_app_tools(query: str = "") -> str:
        """List the app-tools on AIMEAT you can call. Each entry shows its `sku` (pass it to
        call_app_tool), what it does, the JSON input it expects (`input`), and whether it is free for
        you or priced. Give `query` to filter by words in the sku or description; leave it empty for
        all. Read the `input` schema before calling — the model has no other way to know the shape."""
        tools = _catalog(agent_name)
        if not tools:
            return "The app-tool catalog is empty or could not be read."
        q = query.lower().strip()
        rows = []
        for t in tools:
            hay = f"{t.get('sku', '')} {t.get('description', '')}".lower()
            if q and q not in hay:
                continue
            free = _free_for(t, owner)
            rows.append(
                {
                    "sku": t.get("sku"),
                    "does": (t.get("description") or "").strip()[:280],
                    "input": t.get("inputSchema"),
                    "free_for_you": free,
                    "price": None if free else t.get("price"),
                }
            )
        if not rows:
            return f"No app-tool matched {query!r}. Call list_app_tools with an empty query to see all."
        return json.dumps(rows, ensure_ascii=False)

    @tool("call_app_tool")
    def call_app_tool(sku: str, input_json: str = "{}") -> str:
        """Call an app-tool by its `sku` (from list_app_tools). `input_json` is a JSON object matching
        that tool's input schema. Returns the tool's result as JSON. Your own family's tools run free;
        a priced tool owned by someone else needs payment, and I report that rather than pretend it
        ran."""
        try:
            payload = json.loads(input_json) if input_json.strip() else {}
        except ValueError as exc:
            return f"input_json is not valid JSON: {exc}"
        if not isinstance(payload, dict):
            return 'input_json must be a JSON object (e.g. {"text": "..."}).'
        tools = _catalog(agent_name)
        entry = _find(tools, sku)
        if entry is None:
            return f"No single app-tool matches {sku!r}. Call list_app_tools to see the exact sku to use."
        invoke = (entry.get("webmcp") or {}).get("invoke") or ""
        if "/v1/" not in invoke:
            return f"{entry.get('sku')} has no usable invoke address."
        path = "/v1/" + invoke.split("/v1/", 1)[1]

        from crewaimeat.aimeat_crew import _aimeat_rest

        data = _aimeat_rest(agent_name, "POST", path, payload)
        if data is None:
            priced = not _free_for(entry, owner) and entry.get("price")
            if priced:
                return (
                    f"{entry.get('sku')} is priced ({entry.get('price')}) and owned by "
                    f"{_tool_owner(entry)!r}, not your family — it requires payment (checkout), which "
                    f"this tool does not do yet. It did not run."
                )
            return f"{entry.get('sku')} returned no result (it failed — see the fleet log for the node's reason)."
        result = data.get("result", data) if isinstance(data, dict) else data
        return json.dumps(result, ensure_ascii=False)

    return [list_app_tools, call_app_tool]

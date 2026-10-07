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

THE OWNER'S OWN UNPRICED TOOLS (2026-10-06, wish-app-tools-an-agent-sees-and-calls-its-own-owner-s-
unpriced-a). The commerce catalog lists PRICED tools only ("sellable through the commerce checkout"), so
an app's free tools were invisible here and a call by sku answered "No single app-tool matches" -- even
for the agent's own owner's tool, which the node runs for it free (an unpriced callable tool invokes
directly for an authenticated caller). CADENCE's crm agent could not reach CADENCE's own import_records.
So the listing adds the agent's OWN OWNER's unpriced callable tools, read from what already exists:
  - which apps carry a tool manifest: the owner's `apps.<file>.tools` records (an owner-scoped memory
    listing; owner-scoped, so another owner's tools are never in it);
  - each app's tools: the node's own WebMCP listing of that manifest (GET /v1/apps/<owner>/<file>/webmcp),
    the same PUBLIC manifest the invoke route reads, so what is listed is what can be called;
and calls them on that listing's invoke path. Priced tools and other owners' tools are untouched: they
come from the catalog as before, and a foreign priced tool still answers that payment is the call.
ONE READ ON A NODE THAT CAN (aimeat-protocol 08b619ad8, 2026-10-07): GET /v1/commerce/tools?include=own
returns, after the priced entries, the caller's own owner's unpriced callable tools with `price: null` and
`own: true` -- the node reads only that owner's records and only public manifests with an action_id, the
same rule as above. So the catalog is always asked with the flag. When an entry comes back marked `own`,
that answer is the whole list. When none does, the node is older (it ignores an unknown flag) or the
owner has no free tools, and the two reads above run as before: on a new node they find nothing more.
"""

from __future__ import annotations

import json
from typing import Any

_CATALOG_PATH = "/v1/commerce/tools"
# The node adds the caller's own owner's unpriced callable tools to the catalog with this flag (aimeat-
# protocol 08b619ad8); an older node ignores it and answers the priced catalog.
_CATALOG_WITH_OWN = _CATALOG_PATH + "?include=own"


def _invoke_via_mcp(agent_name: str, owner: str, app: str, tool: str, payload: dict) -> dict:
    """Call `aimeat_app_tool_invoke` through the CONNECTOR'S MCP door (`POST /v1/mcp`).

    This is the door the platform designates for this act, and it is designated deliberately: the tool
    is not in the shell dispatch (`/local/call` answers 404 UNKNOWN_TOOL) because a two-sided act under
    a metered contract needs a server-side session, and a loopback door would be a second, weaker copy
    of the metering. `serve_params` puts the identity in `X-Aimeat-Agent`, so the session says who it
    is before a tool is named.

    Returns `{"text": …}` with whatever the door said — the node's own envelope on a refusal. The MCP
    client is async and a crewai tool is not, so the session runs in its own thread with its own loop
    rather than assuming this one has none.
    """
    import asyncio
    import concurrent.futures

    from aimeat_crewai.mcp_client import serve_params
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def _go() -> dict:
        p = serve_params(agent_name=agent_name, auto_start=False)
        async with (
            streamablehttp_client(p["url"], headers=p["headers"]) as (r, w, _),
            ClientSession(r, w) as session,
        ):
            await session.initialize()
            res = await session.call_tool(
                "aimeat_app_tool_invoke", {"owner": owner, "app": app, "tool": tool, "input": payload}
            )
            text = "\n".join(getattr(c, "text", "") or "" for c in res.content).strip()
            return {"is_error": bool(res.isError), "text": text}

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(_go())).result()


def _owner_of(agent_name: str) -> str:
    """This agent's owner GHII. The IDENTITY answers first, then the credential file.

    A GAII (`<agent>#<owner>@<node>`) carries the owner in the middle, and that is both cheaper and
    surer than any file. Falling through to disk covers a bare name — and it must look in `keys/` as
    well as `tokens/`: agent v2 stores an Ed25519 key, not a bearer, so a v2 home has no `tokens/`
    entry for the agent at all. Measured 2026-09-03 on a two-owner v2 home: this returned `''` for
    every agent, so `free_for_you` read False even for the owner of the tool. An abstaining hint
    that says "not yours" is not abstaining."""
    ident = str(agent_name or "")
    if "#" in ident:
        return ident.split("#", 1)[1].split("@", 1)[0]
    try:
        from crewaimeat._home import aimeat_home

        home = aimeat_home()
        for sub, ext in (("keys", ".key"), ("tokens", ".token")):
            for f in (home / sub).glob(f"{ident}@*{ext}"):
                return f.stem.split("@", 1)[1]
    except Exception:  # noqa: BLE001 — the hint is a convenience, never a hard dependency
        pass
    return ""


def _priced_catalog(agent_name: str) -> list[dict]:
    """The catalog, asked with ?include=own: priced entries, and on a node that serves the flag the
    caller's own owner's free callable tools (`own: true`)."""
    from crewaimeat.aimeat_crew import _aimeat_rest

    body = _aimeat_rest(agent_name, "GET", _CATALOG_WITH_OWN, raw=True)
    tools = (body or {}).get("tools") if isinstance(body, dict) else None
    return tools if isinstance(tools, list) else []


_MANIFEST_PREFIX = "apps."
_MANIFEST_SUFFIX = ".tools"


def _own_manifest_files(agent_name: str, owner: str) -> list[str]:
    """The files of the owner's apps that carry a tool manifest (`apps.<file>.tools`), from an
    owner-scoped listing -- only this owner's records are in it. A record another owner wrote is never
    listed by it; one whose recorded owner is someone else is skipped all the same."""
    from crewaimeat.aimeat_crew import _aimeat_call
    from crewaimeat.workflow import _items_of

    listing = _aimeat_call(
        agent_name,
        "aimeat_memory_list",
        {"owner_scope": True, "prefix": _MANIFEST_PREFIX, "limit": 500},
        quiet=True,
    )
    files: list[str] = []
    for it in _items_of(listing):
        key = str(it.get("key") or "")
        if not (key.startswith(_MANIFEST_PREFIX) and key.endswith(_MANIFEST_SUFFIX)):
            continue
        writer = str(it.get("owner_gaii") or it.get("owner") or "")
        if writer and writer.split("#")[-1].split("@")[0] != owner:
            continue
        name = key[len(_MANIFEST_PREFIX) : -len(_MANIFEST_SUFFIX)]
        if name and name not in files:
            files.append(name)
    return files


def _own_unpriced(agent_name: str, owner: str) -> list[dict]:
    """The owner's own unpriced CALLABLE app tools, shaped like catalog entries (sku, app, ownerName,
    name, description, inputSchema, fulfillment, price, webmcp.invoke). Read from each app's WebMCP
    listing; a priced tool there is skipped (it is in the catalog), and so is an unpriced TASK tool,
    which has nothing to run (the node answers TOOL_NOT_INVOKABLE)."""
    if not owner:
        return []
    from urllib.parse import quote

    from crewaimeat.aimeat_crew import _aimeat_rest

    out: list[dict] = []
    for file in _own_manifest_files(agent_name, owner):
        listing = _aimeat_rest(agent_name, "GET", f"/v1/apps/{quote(owner)}/{quote(file)}/webmcp", raw=True)
        tools = listing.get("tools") if isinstance(listing, dict) else None
        for t in tools if isinstance(tools, list) else []:
            if not isinstance(t, dict) or not t.get("name"):
                continue
            if (t.get("payment") or {}).get("required") or t.get("fulfillment") != "call":
                continue
            out.append(
                {
                    "sku": f"app-tool:{owner}/{file}:{t['name']}",
                    "app": f"{owner}/{file}",
                    "ownerName": owner,
                    "name": t["name"],
                    "description": t.get("description") or "",
                    "inputSchema": t.get("inputSchema"),
                    "fulfillment": "call",
                    "price": None,
                    "webmcp": {"invoke": (t.get("invoke") or {}).get("url") or ""},
                    "own_unpriced": True,
                }
            )
    return out


def _catalog(agent_name: str, owner: str = "") -> list[dict]:
    """Every app tool this agent can call: the priced catalog, plus its own owner's unpriced callable
    tools -- from the catalog itself when the node marks them `own`, else from the two reads above. An
    sku in both stays the catalog's."""
    tools = _priced_catalog(agent_name)
    if any(t.get("own") is True for t in tools):
        for t in tools:
            if t.get("own") is True:
                t["own_unpriced"] = True  # the same route as the listing's own entries: webmcp invoke
        return tools
    seen = {t.get("sku") for t in tools}
    return tools + [t for t in _own_unpriced(agent_name, owner) if t["sku"] not in seen]


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
        """List the app-tools on AIMEAT you can call: the priced ones, and your own owner's apps' tools
        that cost you nothing. Each entry shows its `sku` (pass it to call_app_tool), what it does, the
        JSON input it expects (`input`), and whether it is free for you or priced. Give `query` to filter
        by words in the sku or description; leave it empty for all. Read the `input` schema before
        calling — the model has no other way to know the shape. A tool whose input takes a LIST (rows,
        csv, items) takes the WHOLE list in ONE call: never call it once per record."""
        tools = _catalog(agent_name, owner)
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
        ran. A tool whose input takes a LIST (rows, csv, items) gets the WHOLE list in ONE call: put
        every record in that one input, never call it once per record. A tool with a dry-run mode is
        called with the dry run first."""
        try:
            payload = json.loads(input_json) if input_json.strip() else {}
        except ValueError as exc:
            return f"input_json is not valid JSON: {exc}"
        if not isinstance(payload, dict):
            return 'input_json must be a JSON object (e.g. {"text": "..."}).'
        tools = _catalog(agent_name, owner)
        entry = _find(tools, sku)
        if entry is None:
            return f"No single app-tool matches {sku!r}. Call list_app_tools to see the exact sku to use."
        return _call_webmcp(agent_name, entry, payload)

    def _call_webmcp(agent_name: str, entry: dict, payload: dict) -> str:
        """POST the tool's webmcp invoke path and say what the node said."""
        invoke = (entry.get("webmcp") or {}).get("invoke") or ""
        if "/v1/" not in invoke:
            return f"{entry.get('sku')} has no usable invoke address."
        path = "/v1/" + invoke.split("/v1/", 1)[1]

        from crewaimeat.aimeat_crew import _aimeat_rest

        data = _aimeat_rest(agent_name, "POST", path, payload, return_error=True)
        if data is None:
            return (
                f"{entry.get('sku')} could not be reached (the call never got an answer — see the "
                f"fleet log). It did not run."
            )
        # SAY WHAT THE NODE SAID. This used to read a bare None, look at the price field and announce
        # a payment wall — so on 2026-09-03 the app's OWN owner was told their tool was priced and
        # belonged to someone else, when the node had answered TOOL_NOT_INVOKABLE: nothing is wired to
        # it. Two consumers of one gate reporting different reasons for the same call is precisely the
        # divergence this scenario exists to catch, and the divergence was ours.
        if isinstance(data, dict) and data.get("ok") is False:
            err = data.get("error") or {}
            line = f"{entry.get('sku')} did NOT run. The node answered {err.get('code') or 'an error'}"
            status = data.get("http_status")
            if status:
                line += f" (HTTP {status})"
            msg = str(err.get("message") or "").strip()
            if msg:
                line += f": {msg}"
            pay = data.get("payment") or {}
            if pay.get("required"):
                line += (
                    f" — the price is {json.dumps(pay.get('price'), ensure_ascii=False)} and paying IS "
                    f"the call: open and complete a checkout session. I do not do that yet."
                )
            return line
        result = data.get("result", data) if isinstance(data, dict) else data
        return json.dumps(result, ensure_ascii=False)

    @tool("invoke_app_tool")
    def invoke_app_tool(sku: str, input_json: str = "{}") -> str:
        """Call an app-tool through the connector's MCP door, the platform's own route for this act.
        Same arguments as call_app_tool — a `sku` from list_app_tools and a JSON input object (a LIST
        input takes the whole list in one call). Use this when call_app_tool cannot reach the tool; it
        returns the node's answer verbatim, including the checkout terms when the tool is priced. Your
        own owner's free tools go to their webmcp invoke path, as call_app_tool does."""
        try:
            payload = json.loads(input_json) if input_json.strip() else {}
        except ValueError as exc:
            return f"input_json is not valid JSON: {exc}"
        if not isinstance(payload, dict):
            return 'input_json must be a JSON object (e.g. {"text": "..."}).'
        entry = _find(_catalog(agent_name, owner), sku)
        if entry is None:
            return f"No single app-tool matches {sku!r}. Call list_app_tools to see the exact sku to use."
        if entry.get("own_unpriced"):
            # The MCP door's aimeat_app_tool_invoke needs a metered contract, which nobody holds against
            # their own owner's free tool; the node runs it on the webmcp invoke path instead.
            return _call_webmcp(agent_name, entry, payload)
        app_ref = str(entry.get("app") or "")  # "<owner>/<appId>"
        tool_owner, _, app_id = app_ref.partition("/")
        if not tool_owner or not app_id:
            return f"{entry.get('sku')} has no usable app reference ({app_ref!r})."
        try:
            out = _invoke_via_mcp(agent_name, tool_owner, app_id, str(entry.get("name")), payload)
        except Exception as exc:  # noqa: BLE001 — the crew needs the real cause, not a stack in a log
            return f"{entry.get('sku')}: the MCP door could not be reached ({type(exc).__name__}: {exc})."
        return out["text"] or ("(the door answered with no content)" if out["is_error"] else "(no content)")

    return [list_app_tools, call_app_tool, invoke_app_tool]

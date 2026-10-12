"""Attached MCP servers — the tools an owner connected to their node, used from inside a crew.

Why this exists. An owner attaches other MCP servers to their node (an issue tracker, a wiki, a
warehouse system) so that the AI they already talk to can reach them: the node holds the address and
the credential and proxies the call. The connector has carried the three tools for it behind
`/local/call` since 2026-09-16 (aimeat-protocol tool-dispatch/tool-call-defs-mcp-proxy.ts), and
nothing here called them: no crew, no tool id, no line in the liaison's list. An agent this runtime
ran could not use what its owner had attached for exactly that purpose (owner, 2026-10-12).

Two consumers, one implementation, as in workspace_tools:
  - the `mcp` tool id in crew_def's TOOL_REGISTRY: every server the node lets this agent see;
  - `mcp:<server>`: ONE server, named in the definition the owner reads and approves, so an agent
    made for the warehouse is not handed the wiki as well.

THE MODEL NAMES; CODE LOOKS UP. A far tool's name and arguments are the far side's own and nothing
here can know them, so the model reads them first (list_mcp_tools prints each tool with its argument
schema as the server gave it) and passes the arguments as one JSON object.

A CALL HAS THREE ENDINGS, AND THEY ARE NOT ONE "FAILED". The node answers each differently and the
difference is who has to act (aimeat-protocol services/mcp-client/invoke.ts, statusForRemoteRefusal):
  - the tool ran and answered: the answer is the result;
  - THE TOOL SAID NO: it ran, and its own answer explains why (`is_error` on a 200). The model can
    correct the arguments or report what the tool said. Nothing is broken;
  - NOTHING RAN: the node refused this caller (a permission, a grant, a switched-off server), or
    the server cannot be reached or its key was refused, which a PERSON repairs.
Each ending starts with its own line, so a model that reads only the first words still knows which
one happened. A missing answer is a fourth: it is not known whether the tool ran, and it says so.

Access is the node's. Seeing what is attached takes `mcp:read` or `mcp:use`; calling a tool takes
`mcp:use` (routes/mcp-servers.ts). Both are inside `*`. On top of the permission the owner can narrow
one agent to named tools of one server (a grant), which answers NOT_GRANTED. Nothing here checks any
of it first: the node does, and its refusal is handed to the model in its own words.
Everything goes through `_aimeat_call`, the connector's /local/call surface every crew uses.
"""

from __future__ import annotations

import json
import re

from crewaimeat.aimeat_crew import _aimeat_call

# What the node asks (see the module docstring for where each is enforced). Listing accepts either
# word; calling needs `mcp:use`, which therefore covers everything the tool id does.
READ_SCOPES = ("mcp:read",)
USE_SCOPES = ("mcp:use",)

# A server is named by its slug (`^[a-z0-9][a-z0-9-]{0,30}[a-z0-9]$`, models/mcp-server-schemas.ts)
# or by its id, and the node resolves either. A selector that could be neither is refused when the
# definition is validated, before anything is built.
SERVER_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$")

# WHO HAS TO ACT, BY THE NODE'S CODE. The REST route says it with the HTTP status (403 this caller may
# not, 503 this node cannot serve it yet, 502 the far side failed), and the connector's /local/call
# answers every refusal with 400 and no status (measured 2026-10-12 against aimeat 3.26.1), so the code
# is what reaches a crew. The two sets mirror statusForRemoteRefusal (services/mcp-client/invoke.ts)
# plus the scope gate's own code. A code in neither is passed on in the node's words under NOT DONE,
# which is honest about a refusal this file has not met and never calls it a repair or a permission.
_NOT_ALLOWED_CODES = frozenset({"SCOPE_DENIED", "NOT_GRANTED", "SERVER_DISABLED"})
_NEEDS_REPAIR_CODES = frozenset(
    {
        "UNREACHABLE",
        "UPSTREAM_UNAUTHORIZED",
        "NO_ENCRYPTION_KEY",
        "CREDENTIAL_UNREADABLE",
        "TRANSPORT_UNSUPPORTED",
        "STDIO_DISABLED",
        "STDIO_NOT_ALLOWED",
        "PEER_UNKNOWN",
        "PEER_NOT_ROUTABLE",
    }
)

# The first words of each ending. A test holds them, and the tool descriptions quote them.
TOOL_SAID_NO = "THE TOOL SAID NO."
NOT_ALLOWED = "NOT ALLOWED, NOTHING RAN."
NEEDS_REPAIR = "SERVER NEEDS REPAIR, NOTHING RAN."
NO_SUCH_SERVER = "NO SUCH SERVER."
NOT_DONE = "NOT DONE."
NO_ANSWER = "NO ANSWER."


def refusal(answer, what: str) -> str | None:
    """What to tell the model when `answer` is not a success, else None.

    `answer` is what `_aimeat_call(..., return_error=True)` gave: the data on success, the node's own
    envelope on a verdict, None when nothing came back. The node's code and sentence are always in the
    text; the first line says who has to act.
    """
    if answer is None:
        return (
            f"{NO_ANSWER} The node did not answer the {what}. It is not known whether anything ran, so do "
            "not report it as done and do not assume it failed."
        )
    if not (isinstance(answer, dict) and answer.get("ok") is False):
        return None
    err = answer.get("error") if isinstance(answer.get("error"), dict) else {}
    code = str(err.get("code") or "REFUSED")
    said = f"The node's answer to the {what} ({code}): {str(err.get('message') or '').strip() or 'no reason given'}"
    if code == "NOT_FOUND":
        return f"{NO_SUCH_SERVER} {said} No server of that name is attached for you to use; never guess a name."
    if code in _NOT_ALLOWED_CODES:
        return (
            f"{NOT_ALLOWED} {said} This is your owner's decision on the node, so the same call will be "
            "refused again. Say what you were not allowed to do; do not report the work as done."
        )
    if code in _NEEDS_REPAIR_CODES:
        return (
            f"{NEEDS_REPAIR} {said} A person has to repair this on the node (reconnect the server, or bring "
            "it back up); calling again does not repair it. Say that the server needs repair; do not report the work "
            "as done."
        )
    return f"{NOT_DONE} {said}"


def list_servers(agent_name: str):
    """`(servers, "")` or `(None, what to say instead)`: the servers the node lets this agent use.

    Each is the node's public row: slug, title, description, status (active, needs_reauth, unreachable,
    disabled), enabled, toolCount. The address and the credential never leave the node.
    """
    answer = _aimeat_call(agent_name, "aimeat_mcp_list", {}, return_error=True)
    why = refusal(answer, "list of attached servers")
    if why:
        return None, why
    got = answer.get("servers") if isinstance(answer, dict) else None
    return [s for s in got if isinstance(s, dict)] if isinstance(got, list) else [], ""


def list_tools(agent_name: str, server: str, *, refresh: bool = False):
    """`(tools, "")` or `(None, what to say instead)`: one server's tools, as the server described them.

    The node answers from what the server said at its last look; `refresh` asks the server again.
    """
    payload: dict = {"server": server}
    if refresh:
        payload["refresh"] = True
    answer = _aimeat_call(agent_name, "aimeat_mcp_tools", payload, return_error=True)
    why = refusal(answer, f"tool list of {server}")
    if why:
        return None, why
    got = answer.get("tools") if isinstance(answer, dict) else None
    return [t for t in got if isinstance(t, dict)] if isinstance(got, list) else [], ""


def call_tool(agent_name: str, server: str, tool: str, arguments: dict | None = None) -> tuple[str, str]:
    """Run one tool on one server. Returns `(ending, text)`.

    `ending` is "ok" (the tool's answer), "tool_said_no" (it ran and answered no) or "refused"
    (nothing is known to have run; the text starts with which refusal it was). ONE attempt: a far tool
    may write, and a call whose answer was lost in transit may have been carried out, so repeating it
    is the caller's decision and never the transport's.
    """
    answer = _aimeat_call(
        agent_name,
        "aimeat_mcp_call",
        {"server": server, "tool": tool, "arguments": arguments or {}},
        retries=1,
        return_error=True,
    )
    why = refusal(answer, f"call of {tool} on {server}")
    if why:
        return "refused", why
    text = render_content(answer)
    if isinstance(answer, dict) and answer.get("is_error") is True:
        return "tool_said_no", (
            f"{TOOL_SAID_NO} {tool} on {server} ran and refused; the server itself is working. Its answer:\n"
            f"{text}\n"
            "Correct the arguments (list_mcp_tools shows what the tool takes) and call again, or report "
            "what the tool answered."
        )
    return "ok", text


def render_content(answer) -> str:
    """A far tool's answer as text: its text parts as they are, anything else as JSON, nothing dropped."""
    if not isinstance(answer, dict):
        return json.dumps(answer, ensure_ascii=False)
    parts: list[str] = []
    content = answer.get("content")
    for part in content if isinstance(content, list) else ([content] if content is not None else []):
        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
            parts.append(part["text"])
        else:
            parts.append(json.dumps(part, ensure_ascii=False))
    if answer.get("structured_content") is not None:
        parts.append(json.dumps(answer["structured_content"], ensure_ascii=False))
    return "\n".join(parts) if parts else "(the tool answered with no content)"


def render_servers(servers: list[dict]) -> str:
    if not servers:
        return "No MCP server is attached to your owner's node for you to use."
    lines = ["MCP servers attached to your owner's node (call them by the name in quotes):"]
    for s in servers:
        slug = s.get("slug") or s.get("id")
        title = str(s.get("title") or "").strip()
        about = str(s.get("description") or "").strip()
        line = f'- "{slug}"' + (f" ({title})" if title and title != slug else "") + (f": {about}" if about else "")
        line += f" [{s.get('toolCount', '?')} tools]"
        status = s.get("status")
        if s.get("enabled") is False or status == "disabled":
            line += " SWITCHED OFF by your owner; calls are refused."
        elif status == "needs_reauth":
            line += " NEEDS REPAIR: its sign-in ran out, and your owner has to connect it again."
        elif status == "unreachable":
            line += " NEEDS REPAIR: the node could not reach it at its last try."
        lines.append(line)
    return "\n".join(lines)


def render_tools(server: str, tools: list[dict]) -> str:
    if not tools:
        return f'"{server}" offers no tools.'
    lines = [f'Tools of "{server}", each with the arguments it takes (JSON Schema, as the server gave it):']
    for t in tools:
        lines.append(f"- {t.get('name')}: {str(t.get('description') or '').strip() or '(no description)'}")
        schema = t.get("inputSchema")
        if isinstance(schema, dict):
            shown = {k: v for k, v in schema.items() if k != "$schema"}
            lines.append(f"  arguments: {json.dumps(shown, ensure_ascii=False)}")
        notes = t.get("annotations")
        if isinstance(notes, dict) and notes:
            lines.append(f"  the server's own marks: {json.dumps(notes, ensure_ascii=False)}")
    return "\n".join(lines)


def _arguments(raw) -> tuple[dict | None, str]:
    """`(the arguments, "")` or `(None, why not)` for what the model passed as arguments_json."""
    if raw in (None, ""):
        return {}, ""
    try:
        got = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError as exc:
        return None, f"arguments_json is not valid JSON: {exc}"
    if not isinstance(got, dict):
        return None, "arguments_json has to be ONE JSON object, the tool's arguments by name."
    return got, ""


_ENDINGS = (
    f'An answer that starts with "{TOOL_SAID_NO}" means the tool ran and refused: read why, correct the '
    f'arguments or report it. One that starts with "{NOT_ALLOWED}" or "{NEEDS_REPAIR}" means nothing ran and '
    "a person has to act: do not call again, and do not report the work as done."
)


def _described(name: str, description: str, fn):
    """`fn` as a CrewAI tool whose description is `description`.

    The descriptions here quote the endings from the constants above and name the server a tool is
    bound to, and an f-string is not a docstring. The text is set as the docstring BEFORE the tool is
    made, because CrewAI builds what the model reads (the name, the arguments, the description) once,
    from the docstring, when it wraps the function.
    """
    from crewai.tools import tool

    fn.__doc__ = description
    return tool(name)(fn)


def make_mcp_tools(agent_name: str) -> list:
    """The crew_def `mcp` tool: list the attached servers, list one's tools, call one."""

    def list_mcp_servers() -> str:
        servers, why = list_servers(agent_name)
        return why if servers is None else render_servers(servers)

    def list_mcp_tools(server: str, refresh: bool = False) -> str:
        tools, why = list_tools(agent_name, server, refresh=refresh)
        return why if tools is None else render_tools(server, tools)

    def call_mcp_tool(server: str, tool: str, arguments_json: str = "{}") -> str:
        args, why = _arguments(arguments_json)
        if args is None:
            return f"{NOT_DONE} {why}"
        return call_tool(agent_name, server, tool, args)[1]

    return [
        _described(
            "list_mcp_servers",
            "List the MCP servers your owner attached to their node: other systems (an issue tracker, a "
            "wiki, a warehouse) you can use through it. Call this FIRST when the work needs a system you "
            "have no other tool for; never guess a server name.",
            list_mcp_servers,
        ),
        _described(
            "list_mcp_tools",
            "List one attached server's tools, each with the arguments it takes. `server` is the name "
            "list_mcp_servers shows. Read this BEFORE call_mcp_tool: the tool names and argument shapes "
            "are the server's own. `refresh` only when a call failed as if the tool had changed.",
            list_mcp_tools,
        ),
        _described(
            "call_mcp_tool",
            "Run ONE tool on ONE attached server. `server` and `tool` are the names list_mcp_servers and "
            "list_mcp_tools show; `arguments_json` is one JSON object with the tool's arguments, in the "
            'shape list_mcp_tools printed (e.g. {"order_id": "4417"}). The answer is the tool\'s own. '
            f"{_ENDINGS}",
            call_mcp_tool,
        ),
    ]


def make_mcp_server_tools(agent_name: str, server: str) -> list:
    """The crew_def `mcp:<server>` tool: ONE server's tool list and calls, and no other server."""
    stem = server.replace("-", "_")

    def list_server_tools(refresh: bool = False) -> str:
        tools, why = list_tools(agent_name, server, refresh=refresh)
        return why if tools is None else render_tools(server, tools)

    def call_server_tool(tool: str, arguments_json: str = "{}") -> str:
        args, why = _arguments(arguments_json)
        if args is None:
            return f"{NOT_DONE} {why}"
        return call_tool(agent_name, server, tool, args)[1]

    return [
        _described(
            f"list_{stem}_tools",
            f'List the tools of "{server}", a system your owner attached to their node for you, each with '
            f"the arguments it takes. Read this BEFORE call_{stem}_tool: the tool names and argument shapes "
            "are the server's own. `refresh` only when a call failed as if the tool had changed.",
            list_server_tools,
        ),
        _described(
            f"call_{stem}_tool",
            f'Run ONE tool of "{server}". `tool` is a name list_{stem}_tools shows; `arguments_json` is one '
            f'JSON object with its arguments, in the shape that list printed (e.g. {{"order_id": "4417"}}). '
            f"The answer is the tool's own. {_ENDINGS}",
            call_server_tool,
        ),
    ]

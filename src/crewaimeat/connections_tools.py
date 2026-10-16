"""Connected accounts as a crew tool: what was published through them, and the replies it got. READ-ONLY.

Why this exists. An agent that reads the replies to what was published, sorts them (a question, buying
intent, criticism, praise, junk) and drafts answers for a person to approve needs two reads the node
has had since 2026-10-15 (aimeat-protocol 26defe519): the publish history and the replies to one item.
Nothing a crew held reached either. This module is the `connections` tool id in crew_def's
TOOL_REGISTRY, and it mirrors the node's MCP tools `aimeat_connection_history` and
`aimeat_connection_replies` (tool-catalog/definitions/connections.ts) in wording and parameters.

THE TOOL NEVER SENDS. Publishing, metric reads (which spend a provider request and are stored) and
everything else under /v1/connections are not reachable from here: every request goes through
`_node_read`, which lets exactly two method and path shapes through and refuses anything else before
the transport is asked. The replies read is a POST on the node because it spends a provider request
(and money on X); it stores nothing and changes nothing at the provider.

The routes (aimeat-protocol routes/connections.ts, routes/connections-replies.ts,
services/connections/history.ts, services/connections/replies.ts):
  GET  /v1/connections/attempts?limit=N            connections:read or connections:use. Newest first,
       at most 200. The node takes no other filter, so the provider and date filters are applied here.
  POST /v1/connections/attempts/{id}/replies {limit} connections:use or connections:read-through.
       409 NOT_READABLE is a platform that will never allow it (LinkedIn by design, a provider with no
       reader, comments turned off, a deleted post); 502 REPLIES_UNAVAILABLE may work later.

WHOSE HISTORY. Both reads take the holders the caller may read (aimeat-protocol
services/connections/access.ts connectionHoldersOf): the agent's own items always, and its OWNER's
items when the agent holds `connections:read-as-owner`, the permission the owner ticks by hand on the
agent's page and that no wildcard covers (Jouni, 2026-10-16). Each history item carries `holder`:
"own" or "owner", and the list shows it. An agent without the permission sees only what it published
itself, so a list with no owner's item says what it would take; the model must not read it as "the
owner published nothing".

Access is the node's. READ_SCOPES names the three words an agent needs for both reads, its owner's
items included, without being able to publish (`connections:use` would cover the agent's own items,
and it also publishes). Nothing here checks them first: the node does, and its refusal reaches the
model in the node's own words.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from urllib.parse import urlencode

from crewaimeat.aimeat_crew import _aimeat_rest

# The words the node asks for (see the module docstring). `connections:read` opens the history,
# `connections:read-through` the replies, and `connections:read-as-owner` adds the owner's items to
# both; none of them publishes. The last one is outside every wildcard, so `*` does not hold it.
READ_AS_OWNER = "connections:read-as-owner"
READ_SCOPES = ("connections:read", "connections:read-through", READ_AS_OWNER)

HISTORY_PATH = "/v1/connections/attempts"
NODE_HISTORY_MAX = 200  # the route's own ceiling (routes/connections.ts)
NODE_REPLIES_MAX = 200  # the service's own ceiling (services/connections/replies.ts)
X_PROVIDER = "x"  # the provider whose every reply read is billed

# A publish attempt id is a UUID; anything that could add a path segment or a query is refused.
ATTEMPT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_REPLIES_PATH_RE = re.compile(r"^/v1/connections/attempts/[A-Za-z0-9][A-Za-z0-9_-]{0,127}/replies$")
_HISTORY_PATH_RE = re.compile(r"^/v1/connections/attempts(\?limit=\d{1,3})?$")

# How much one answer shows. A reply's text past REPLY_TEXT_SHOWN characters is cut, and the answer
# stops adding replies past ANSWER_SHOWN characters; both say so in the answer, with the count left.
REPLY_TEXT_SHOWN = 1500
ANSWER_SHOWN = 30_000

# The first words of each ending. A test holds them, and the tool descriptions quote them.
NOT_READABLE = "NOT READABLE."
NOT_NOW = "NOT READ NOW."
NOT_ALLOWED = "NOT ALLOWED."
NOT_DONE = "NOT DONE."
NO_ANSWER = "NO ANSWER."
REFUSED_HERE = "REFUSED BY THIS TOOL, NOTHING SENT."

_NOT_ALLOWED_CODES = frozenset({"SCOPE_DENIED", "CONNECTIONS_DISABLED", "FORBIDDEN", "READ_AS_OWNER_REQUIRED"})


class NotARead(ValueError):
    """A request this tool does not make: anything but the two reads."""


def _node_read(agent_name: str, method: str, path: str, body: dict | None = None, *, retries: int):
    """The one place this module reaches the node, and it lets only the two reads through."""
    history = method == "GET" and bool(_HISTORY_PATH_RE.match(path)) and body is None
    replies = method == "POST" and bool(_REPLIES_PATH_RE.match(path)) and set(body or {}) <= {"limit"}
    if not (history or replies):
        raise NotARead(f"{method} {path} is not one of the two reads the connections tool makes")
    return _aimeat_rest(agent_name, method, path, body, retries=retries, return_error=True)


def refusal(answer, what: str) -> str | None:
    """What to tell the model when `answer` is not a success, else None. The node's words are kept."""
    if answer is None:
        return (
            f"{NO_ANSWER} The node did not answer the {what}. It is not known whether the provider was asked "
            "(on X a read may have been billed), so do not call again in this run and do not report that "
            "there were no replies."
        )
    if not (isinstance(answer, dict) and answer.get("ok") is False):
        return None
    err = answer.get("error") if isinstance(answer.get("error"), dict) else {}
    code = str(err.get("code") or "REFUSED")
    said = f"The node's answer to the {what} ({code}): {str(err.get('message') or '').strip() or 'no reason given'}"
    if code == "NOT_READABLE":
        return (
            f"{NOT_READABLE} {said}. The platform will not allow this read, now or later: do not call again "
            "for this item, and say that its replies cannot be read rather than that nobody answered."
        )
    if code == "REPLIES_UNAVAILABLE":
        return f"{NOT_NOW} {said}. It may work later; do not call again in this run, and say it was not read."
    if code in _NOT_ALLOWED_CODES or answer.get("http_status") == 403:
        return (
            f"{NOT_ALLOWED} {said}. This is your owner's decision on the node, so the same call is refused "
            "again. Say what you could not read; do not report the work as done."
        )
    return f"{NOT_DONE} {said}"


def _as_int(raw, default: int, ceiling: int) -> int:
    """A model sends numbers as strings, or not at all; either is a number between 1 and the ceiling."""
    try:
        n = int(str(raw).strip()) if raw not in (None, "") else default
    except ValueError:
        n = default
    return max(1, min(n, ceiling))


def _providers(raw) -> set[str]:
    items = raw if isinstance(raw, list | tuple) else re.split(r"[,\s]+", str(raw or ""))
    return {str(p).strip().lower() for p in items if str(p).strip()}


def _since(raw) -> tuple[datetime | None, str]:
    """`(the moment, "")`, `(None, "")` for no filter, or `(None, why not)`."""
    text = str(raw or "").strip()
    if not text:
        return None, ""
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None, f"since={text!r} is not a date: write it as 2026-10-15 or 2026-10-15T06:00:00Z"
    return (when if when.tzinfo else when.replace(tzinfo=UTC)), ""


def _created(item: dict) -> datetime | None:
    try:
        when = datetime.fromisoformat(str(item.get("createdAt") or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def list_attempts(agent_name: str, *, limit=20, providers="", exclude_providers="", since="", status=""):
    """`(items, notes, "")` or `(None, [], what to say instead)`: the history, filtered here.

    The node takes only `limit`, so with any filter the node's whole window (200) is read once and the
    filter applied to it; `notes` says when that window may not reach back to `since`.
    """
    want = _as_int(limit, 20, NODE_HISTORY_MAX)
    only, skip = _providers(providers), _providers(exclude_providers)
    after, why = _since(since)
    if why:
        return None, [], f"{NOT_DONE} {why}"
    status = str(status or "").strip().lower()
    filtered = bool(only or skip or after or status)
    ask = NODE_HISTORY_MAX if filtered else want
    answer = _node_read(agent_name, "GET", f"{HISTORY_PATH}?{urlencode({'limit': ask})}", retries=3)
    why = refusal(answer, "publish history")
    if why:
        return None, [], why
    got = answer.get("attempts") if isinstance(answer, dict) else None
    rows = [a for a in got if isinstance(a, dict)] if isinstance(got, list) else []
    notes: list[str] = []
    if filtered and len(rows) >= NODE_HISTORY_MAX:
        oldest = _created(rows[-1])
        if after is None or oldest is None or oldest > after:
            notes.append(f"The node lists at most {NODE_HISTORY_MAX} items; older ones were not looked at.")
    keep = []
    for a in rows:
        prov = str(a.get("provider") or "").lower()
        if (only and prov not in only) or prov in skip:
            continue
        if status and str(a.get("status") or "").lower() != status:
            continue
        if after is not None:
            when = _created(a)
            if when is None or when < after:
                continue
        keep.append(a)
    if len(keep) > want:
        notes.append(f"{len(keep) - want} more matched; raise limit to see them.")
    if not any(a.get("holder") == "owner" for a in rows):
        notes.append(OWNER_NOTE)
    return keep[:want], notes, ""


# Said whenever the node's answer holds none of the owner's items: either the owner published nothing
# here, or the agent lacks the permission that shows them. Which one cannot be told from the answer.
OWNER_NOTE = (
    "None of these is your owner's. Your owner's published items are listed only when you hold "
    f"{READ_AS_OWNER}, a permission your owner gives you by hand on your page under Agents; without it, "
    "an empty or short list does not mean your owner published nothing. Say which one you cannot tell."
)


def _metrics(latest) -> str:
    if not isinstance(latest, dict):
        return "no numbers read yet"
    parts = [f"{k} {latest[k]}" for k in ("impressions", "likes", "comments", "shares") if latest.get(k) is not None]
    return (", ".join(parts) or "no numbers") + f" (read {latest.get('fetchedAt') or '?'})"


def render_attempts(items: list[dict], notes: list[str]) -> str:
    if not items:
        lines = ["No published item matches."]
    else:
        lines = [
            f"{len(items)} published item(s), newest first. Pass an item's id to read_connection_replies; only "
            "an item with status done was published, so only it can have replies. `yours` is an item you "
            "published, `your owner's` one your owner published."
        ]
        for a in items:
            holder = "your owner's" if a.get("holder") == "owner" else "yours"
            line = (
                f"- id {a.get('id')} | {holder} | {a.get('provider') or 'unknown'} ({a.get('accountLabel') or '?'}) | "
                f"{a.get('status') or '?'} | {a.get('createdAt') or '?'}"
            )
            if a.get("externalRef"):
                line += f" | {a['externalRef']}"
            line += f" | {_metrics(a.get('latest'))}"
            if a.get("error"):
                line += f" | error: {a['error']}"
            lines.append(line)
    return "\n".join([*lines, *notes])


def read_replies(agent_name: str, attempt_id: str, *, limit=100):
    """`(answer, "")` or `(None, what to say instead)`. ONE attempt: on X a read is billed, so a read
    whose answer was lost is not repeated by the transport."""
    attempt_id = str(attempt_id or "").strip()
    if not ATTEMPT_ID_RE.match(attempt_id):
        return None, (
            f"{REFUSED_HERE} {attempt_id!r} is not an item id. Use an id exactly as list_connection_history shows it."
        )
    body = {"limit": _as_int(limit, 100, NODE_REPLIES_MAX)}
    answer = _node_read(agent_name, "POST", f"{HISTORY_PATH}/{attempt_id}/replies", body, retries=1)
    why = refusal(answer, f"replies to item {attempt_id}")
    if why:
        return None, why
    return (answer if isinstance(answer, dict) else {}), ""


def _one_line(text: str) -> str:
    return " ".join(str(text or "").split())


def render_replies(answer: dict) -> str:
    replies = [r for r in answer.get("replies") or [] if isinstance(r, dict)]
    head = (
        f"Replies to {answer.get('url') or answer.get('attemptId')} on {answer.get('provider') or '?'}, read "
        f"{answer.get('fetchedAt') or 'now'}: {len(replies)}"
        + (", and the provider has more than this answer holds" if answer.get("truncated") else "")
        + "."
    )
    lines = [head]
    if answer.get("note"):
        lines.append(f"Note from the node: {answer['note']}.")
    if not replies:
        lines.append("Nobody has answered this item.")
    size = sum(len(x) + 1 for x in lines)
    for i, r in enumerate(replies):
        text = _one_line(r.get("text"))
        if len(text) > REPLY_TEXT_SHOWN:
            text = f"{text[:REPLY_TEXT_SHOWN]} [cut here, {len(text) - REPLY_TEXT_SHOWN} characters more: open the url]"
        who = r.get("authorHandle") or r.get("author") or "someone"
        line = f"- reply {r.get('id')} by {who} at {r.get('createdAt') or '?'}"
        if r.get("parentId"):
            line += f", answering reply {r['parentId']}"
        if r.get("likes") is not None:
            line += f", {r['likes']} likes"
        if r.get("url"):
            line += f", {r['url']}"
        line += f"\n  {text}"
        if size + len(line) + 1 > ANSWER_SHOWN:
            lines.append(
                f"{len(replies) - i} older reply(ies) not shown: this answer is full. Call again with a smaller "
                "limit to read the newest ones only."
            )
            break
        lines.append(line)
        size += len(line) + 1
    return "\n".join(lines)


def _described(name: str, description: str, fn):
    """`fn` as a CrewAI tool whose description is `description` (set before CrewAI reads the docstring)."""
    from crewai.tools import tool

    fn.__doc__ = description
    return tool(name)(fn)


_X_RULE = (
    "On X every read is billed and X search reaches back only seven days, so read X replies at most once "
    'a week: on other runs leave X out (exclude_providers="x" in list_connection_history) and do not '
    "read an X item's replies."
)


def make_connections_tools(agent_name: str) -> list:
    """The crew_def `connections` tool: the publish history and the replies to one item. Nothing else."""

    def list_connection_history(
        limit: int = 20, providers: str = "", exclude_providers: str = "", since: str = "", status: str = ""
    ) -> str:
        items, notes, why = list_attempts(
            agent_name,
            limit=limit,
            providers=providers,
            exclude_providers=exclude_providers,
            since=since,
            status=status,
        )
        return why if items is None else render_attempts(items, notes)

    def read_connection_replies(attempt_id: str, limit: int = 100) -> str:
        answer, why = read_replies(agent_name, attempt_id, limit=limit)
        return why if answer is None else render_replies(answer)

    return [
        _described(
            "list_connection_history",
            "What YOU have published through your connected accounts, and what your OWNER published when you "
            f"hold {READ_AS_OWNER} (a permission your owner gives by hand), newest first. Each item says whose "
            "it is (yours or your owner's), its status (done, failed, rejected, held, queued or in_flight), where "
            "it landed (the post's address at the provider), the account it went out through, and the latest "
            "numbers the node took of it. The item id is what read_connection_replies takes. Filters: `limit` "
            '(default 20, max 200), `providers` (only these, comma-separated, e.g. "mastodon,bluesky"), '
            '`exclude_providers` (leave these out, e.g. "x"), `since` (a date, 2026-10-15, or a time), '
            '`status` ("done" lists only what was published). ' + _X_RULE,
            list_connection_history,
        ),
        _described(
            "read_connection_replies",
            "Read what people answered to one item you or your owner published (its id from list_connection_history is "
            "attempt_id; `limit` is how many replies at most, default 100, max 200). Works for Mastodon, "
            "Bluesky, YouTube, X, a Facebook Page, Instagram and Reddit; LinkedIn refuses by design, because it "
            "lets only select partners read the comments on a member's post. It reads from the provider NOW "
            "and stores nothing, so every call spends a request against the account's own rate limit; read "
            f"each item once per run. {_X_RULE} Then judge the replies and report what matters: the questions "
            "that need an answer, the criticism worth taking seriously, the overall tone; do not paste them "
            f'all back. An answer that starts with "{NOT_READABLE}" means the platform will never allow it '
            f'(LinkedIn, a provider with no reader, comments turned off, a deleted post); "{NOT_NOW}" means it '
            f'may work later; "{NOT_ALLOWED}" means your owner has not given you the permission (an item of '
            f"your owner's needs {READ_AS_OWNER}). This tool "
            "never posts, answers or deletes anything.",
            read_connection_replies,
        ),
    ]

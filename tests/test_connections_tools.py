"""The `connections` crew tool: the publish history and the replies to one item, READ-ONLY.

The node's answers below follow aimeat-protocol 26defe519: GET /v1/connections/attempts answers
`{attempts: [PublishHistoryItem]}` (services/connections/history.ts), POST
/v1/connections/attempts/:id/replies answers `{attemptId, provider, url, replies, truncated, note?,
fetchedAt}`, and a refusal arrives as the envelope `_aimeat_rest(..., return_error=True)` hands back:
`{ok: false, error: {code, message}, http_status}`. The HTTP layer is faked where `_aimeat_rest` stands.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from crewaimeat import connections_tools as ct

AID = "6f1c2a9e-0b7d-4c55-9a43-2f0e8d1b7c10"


def _attempt(id_, provider, created, status="done", **kw):
    return {
        "id": id_,
        "connectionId": "conn-1",
        "provider": provider,
        "accountLabel": f"@me on {provider}",
        "status": status,
        "externalRef": f"https://{provider}.example/status/{id_}" if status == "done" else None,
        "storageKey": "files/post.txt",
        "error": None if status == "done" else "provider said no",
        "createdAt": created,
        "latest": None,
        **kw,
    }


HISTORY = {
    "attempts": [
        _attempt(
            AID,
            "mastodon",
            "2026-10-15T09:00:00.000Z",
            latest={"fetchedAt": "2026-10-15T12:00:00Z", "impressions": None, "likes": 4, "comments": 2, "shares": 1},
        ),
        _attempt("a-x-1", "x", "2026-10-14T09:00:00.000Z"),
        _attempt("a-bsky-1", "bluesky", "2026-10-12T09:00:00.000Z", status="failed"),
        _attempt("a-li-1", "linkedin", "2026-10-01T09:00:00.000Z"),
    ]
}

REPLIES = {
    "attemptId": AID,
    "provider": "mastodon",
    "url": f"https://mastodon.example/@me/{AID}",
    "replies": [
        {
            "id": "113",
            "author": "Ann",
            "authorHandle": "@ann@social.example",
            "text": "How much does it cost\nfor a team of five?",
            "createdAt": "2026-10-15T10:00:00Z",
            "url": "https://social.example/@ann/113",
            "parentId": None,
            "likes": 2,
        },
        {
            "id": "114",
            "author": "Bob",
            "authorHandle": None,
            "text": "Same question.",
            "createdAt": "2026-10-15T10:05:00Z",
            "url": None,
            "parentId": "113",
            "likes": None,
        },
    ],
    "truncated": False,
    "fetchedAt": "2026-10-16T06:00:00Z",
}


def _refused(code: str, message: str, status: int) -> dict:
    return {"ok": False, "error": {"code": code, "message": message}, "http_status": status}


class Node:
    """Stands where `_aimeat_rest` is: answers by (method, path prefix) and records every request."""

    def __init__(self, history=None, replies=None):
        self.history, self.replies = history, replies
        self.calls: list[tuple] = []

    def __call__(self, agent, method, path, body=None, **kw):
        self.calls.append((agent, method, path, body, kw))
        if method == "GET" and path.startswith(ct.HISTORY_PATH + "?"):
            return self.history
        if method == "POST" and path.endswith("/replies"):
            return self.replies
        raise AssertionError(f"the tool reached {method} {path}")


@pytest.fixture
def node(monkeypatch):
    def make(**answers) -> Node:
        n = Node(**answers)
        monkeypatch.setattr(ct, "_aimeat_rest", n)
        return n

    return make


def _tools(agent="gtm-vastaukset"):
    return {t.name: t for t in ct.make_connections_tools(agent)}


# ── the history ─────────────────────────────────────────────────────────────────────────────


def test_the_history_lists_each_item_with_its_id_place_and_numbers(node):
    n = node(history=HISTORY)
    out = _tools()["list_connection_history"].run()
    assert f"id {AID}" in out and "mastodon (@me on mastodon)" in out
    assert f"https://mastodon.example/status/{AID}" in out
    assert "likes 4, comments 2, shares 1" in out and "impressions" not in out, "a null number is not shown"
    assert "error: provider said no" in out, "a failed item says why"
    agent, method, path, body, kw = n.calls[0]
    assert (agent, method, path, body) == ("gtm-vastaukset", "GET", "/v1/connections/attempts?limit=20", None)
    assert kw["return_error"] is True


def test_the_node_takes_only_limit_so_the_filters_are_applied_here(node):
    n = node(history=HISTORY)
    out = _tools()["list_connection_history"].run(exclude_providers="x", since="2026-10-10", status="done")
    assert n.calls[0][2] == "/v1/connections/attempts?limit=200", "a filter reads the node's whole window"
    assert AID in out
    assert "a-x-1" not in out, "X left out"
    assert "a-bsky-1" not in out, "only what was published"
    assert "a-li-1" not in out, "older than since"


def test_only_the_named_providers_are_listed(node):
    node(history=HISTORY)
    out = _tools()["list_connection_history"].run(providers="X, LinkedIn", limit="5")
    assert "a-x-1" in out and "a-li-1" in out and AID not in out


def test_more_matches_than_the_limit_are_counted_not_dropped_silently(node):
    node(history=HISTORY)
    out = _tools()["list_connection_history"].run(providers="mastodon,x,linkedin", limit=1)
    assert AID in out and "a-x-1" not in out
    assert "2 more matched" in out


def test_a_full_window_that_may_not_reach_back_says_so(node):
    full = {"attempts": [_attempt(f"a{i}", "mastodon", "2026-10-15T09:00:00Z") for i in range(ct.NODE_HISTORY_MAX)]}
    node(history=full)
    out = _tools()["list_connection_history"].run(since="2026-01-01", limit=200)
    assert "older ones were not looked at" in out


def test_a_date_that_is_not_one_never_reaches_the_node(node):
    n = node(history=HISTORY)
    out = _tools()["list_connection_history"].run(since="last week")
    assert out.startswith(ct.NOT_DONE) and "2026-10-15" in out
    assert n.calls == []


def test_an_empty_history_names_the_permission_the_owners_items_need(node):
    node(history={"attempts": []})
    out = _tools()["list_connection_history"].run()
    assert "No published item matches." in out
    assert "connections:read-as-owner" in out and "does not mean your owner published nothing" in out


def test_each_item_says_whose_it_is(node):
    owner_item = _attempt("a-owner-1", "linkedin", "2026-10-16T08:00:00.000Z", holder="owner")
    own_item = _attempt("a-own-1", "mastodon", "2026-10-15T08:00:00.000Z", holder="own")
    node(history={"attempts": [owner_item, own_item]})
    out = _tools()["list_connection_history"].run()
    assert "id a-owner-1 | your owner's | linkedin" in out
    assert "id a-own-1 | yours | mastodon" in out
    assert "connections:read-as-owner" not in out, "the owner's items are there, so nothing is missing"


def test_a_history_with_only_own_items_says_what_the_owners_would_take(node):
    node(history=HISTORY)  # every item is the agent's own
    out = _tools()["list_connection_history"].run()
    assert "None of these is your owner's" in out and "connections:read-as-owner" in out


def test_an_owners_item_without_the_permission_is_the_owners_decision(node):
    node(
        replies=_refused(
            "READ_AS_OWNER_REQUIRED", "This item is your owner's; reading it needs connections:read-as-owner.", 403
        )
    )
    out = _tools()["read_connection_replies"].run(attempt_id=AID)
    assert out.startswith(ct.NOT_ALLOWED) and "connections:read-as-owner" in out


def test_a_missing_permission_reaches_the_model_in_the_nodes_words(node):
    node(history=_refused("SCOPE_DENIED", 'Scope "connections:read" required.', 403))
    out = _tools()["list_connection_history"].run()
    assert out.startswith(ct.NOT_ALLOWED) and 'Scope "connections:read" required.' in out


# ── the replies ─────────────────────────────────────────────────────────────────────────────


def test_a_replies_read_hands_back_each_reply_in_one_shape(node):
    n = node(replies=REPLIES)
    out = _tools()["read_connection_replies"].run(attempt_id=AID)
    assert "on mastodon" in out and ": 2." in out
    assert "reply 113 by @ann@social.example" in out and "How much does it cost for a team of five?" in out
    assert "reply 114 by Bob" in out and "answering reply 113" in out
    agent, method, path, body, kw = n.calls[0]
    assert (method, path, body) == ("POST", f"/v1/connections/attempts/{AID}/replies", {"limit": 100})
    assert kw["retries"] == 1, "on X a read is billed: a lost answer is not read twice"


def test_the_limit_is_passed_and_held_to_the_nodes_ceiling(node):
    n = node(replies=REPLIES)
    _tools()["read_connection_replies"].run(attempt_id=AID, limit="25")
    _tools()["read_connection_replies"].run(attempt_id=AID, limit=999)
    assert [c[3] for c in n.calls] == [{"limit": 25}, {"limit": ct.NODE_REPLIES_MAX}]


def test_no_replies_and_the_nodes_note_are_said(node):
    node(replies={**REPLIES, "provider": "x", "replies": [], "note": "X search reaches back seven days"})
    out = _tools()["read_connection_replies"].run(attempt_id=AID)
    assert "Nobody has answered" in out and "seven days" in out


@pytest.mark.parametrize(
    "reason",
    [
        "no way to read replies from tumblr yet",
        "LinkedIn does not let an app read the comments on a member's post; that needs a permission LinkedIn "
        "grants to select partners only",
    ],
)
def test_an_unsupported_provider_is_a_readable_permanent_answer(node, reason):
    node(replies=_refused("NOT_READABLE", reason, 409))
    out = _tools()["read_connection_replies"].run(attempt_id=AID)
    assert out.startswith(ct.NOT_READABLE)
    assert reason in out, "the node's own sentence is in it"
    assert "do not call again for this item" in out and "rather than that nobody answered" in out


def test_a_read_that_may_work_later_is_not_called_permanent(node):
    node(replies=_refused("REPLIES_UNAVAILABLE", "X is rate limiting this app; try later", 502))
    out = _tools()["read_connection_replies"].run(attempt_id=AID)
    assert out.startswith(ct.NOT_NOW) and "rate limiting" in out
    assert ct.NOT_READABLE not in out


def test_no_answer_is_neither_empty_nor_failed(node):
    node(replies=None)
    out = _tools()["read_connection_replies"].run(attempt_id=AID)
    assert out.startswith(ct.NO_ANSWER) and "do not report that there were no replies" in out


def test_a_long_reply_is_cut_and_says_so(node):
    long = {**REPLIES["replies"][0], "text": "a" * (ct.REPLY_TEXT_SHOWN + 40)}
    node(replies={**REPLIES, "replies": [long]})
    out = _tools()["read_connection_replies"].run(attempt_id=AID)
    assert "40 characters more: open the url" in out


def test_an_answer_past_its_size_names_how_many_replies_it_left_out(node, monkeypatch):
    monkeypatch.setattr(ct, "ANSWER_SHOWN", 400)
    many = [{**REPLIES["replies"][0], "id": str(i), "text": "x" * 100} for i in range(10)]
    node(replies={**REPLIES, "replies": many})
    out = _tools()["read_connection_replies"].run(attempt_id=AID)
    assert len(out) < 700 and "older reply(ies) not shown" in out


# ── read-only ───────────────────────────────────────────────────────────────────────────────


def test_the_tool_offers_two_reads_and_nothing_that_writes():
    names = set(_tools())
    assert names == {"list_connection_history", "read_connection_replies"}
    for desc in (t.description for t in _tools().values()):
        assert "once a week" in desc, "X costs money per read; every description says how often"
    assert "never posts, answers or deletes" in _tools()["read_connection_replies"].description


@pytest.mark.parametrize(
    "bad",
    [f"{AID}/../../publish", f"{AID}/metrics", "x?limit=1", "../connections/start", "", " ", "a" * 200],
)
def test_an_id_that_could_reach_another_route_never_reaches_the_node(node, bad):
    n = node(replies=REPLIES)
    out = _tools()["read_connection_replies"].run(attempt_id=bad)
    assert out.startswith(ct.REFUSED_HERE)
    assert n.calls == []


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/v1/connections/publish", {"caption": "hi"}),
        ("POST", f"/v1/connections/attempts/{AID}/metrics", None),
        ("POST", f"/v1/connections/attempts/{AID}/replies", {"limit": 5, "text": "thanks!"}),
        ("DELETE", "/v1/connections/conn-1", None),
        ("GET", "/v1/connections/conn-1/read/messages", None),
        ("POST", "/v1/connections/attempts", None),
    ],
)
def test_the_one_door_to_the_node_refuses_every_request_but_the_two_reads(node, method, path, body):
    n = node(history=HISTORY, replies=REPLIES)
    with pytest.raises(ct.NotARead):
        ct._node_read("a", method, path, body, retries=1)
    assert n.calls == []


# ── the tool id in a crew definition, and the permissions it needs ──────────────────────────


def _doc(tools: list[str]) -> dict:
    return {
        "agent_name": "gtm-vastaukset",
        "agents": [{"role": "Reply reader", "goal": "g", "backstory": "b", "tools": tools}],
        "tasks": [{"id": "run", "agent": "Reply reader", "description": "{{ctx.prompt}}", "expected_output": "x"}],
    }


def test_a_definition_can_name_it_and_the_interpreter_builds_it_without_asking_the_node(node):
    from crewaimeat.crew_def import TOOL_PURPOSES, TOOL_REGISTRY, resolve_tool, validate_crew_doc

    assert "connections" in TOOL_REGISTRY and "connections" in TOOL_PURPOSES
    assert "Read-only" in TOOL_PURPOSES["connections"] and "connections:read-through" in TOOL_PURPOSES["connections"]
    assert validate_crew_doc(_doc(["connections", "decline"])) == []
    n = node(history=HISTORY)
    built = resolve_tool("connections")("gtm-vastaukset#happydude500001@aimeat.io", SimpleNamespace(task={"id": "t"}))
    assert [t.name for t in built] == ["list_connection_history", "read_connection_replies"]
    assert n.calls == []
    built[0].run()
    assert n.calls[0][0] == "gtm-vastaukset#happydude500001@aimeat.io", "the node is asked as the running identity"


def test_the_crew_menu_offers_it(monkeypatch):
    import crewaimeat.crew_invoke as ci

    monkeypatch.setattr("crewaimeat.llm_choice.default_is_node_road", lambda agent_name: False)
    ok, menu = ci.handle("crew.menu", {}, agent_name="concierge")
    assert ok
    row = next(t for t in menu["tools"] if t["id"] == "connections")
    assert "connections:read" in row["purpose"]


def test_the_proposer_asks_for_the_three_read_words_and_never_for_use():
    from crewaimeat import concierge_propose

    assert ct.READ_SCOPES == ("connections:read", "connections:read-through", "connections:read-as-owner")
    assert concierge_propose.PROPOSABLE_TOOLS["connections"] is ct.READ_SCOPES
    asked, left_out = concierge_propose.compute_scopes(["connections"], ["*"])
    assert "connections:use" not in asked, "use publishes; this tool never does"
    asked, left_out = concierge_propose.compute_scopes(["connections"], ["memory:read", "memory:write"])
    assert left_out == list(ct.READ_SCOPES), "capped at what the proposer holds"


def test_read_as_owner_is_asked_for_only_when_the_proposer_holds_it_by_name():
    """No wildcard carries connections:read-as-owner on the node, so a concierge holding `*` does not
    hold it: asking for it would make the proposal wider than the proposer and refused whole."""
    from crewaimeat import concierge_propose

    asked, left_out = concierge_propose.compute_scopes(["connections"], ["*"])
    assert {"connections:read", "connections:read-through"} <= set(asked)
    assert left_out == ["connections:read-as-owner"], "left out and named, for the owner to tick by hand"
    asked, left_out = concierge_propose.compute_scopes(["connections"], ["connections:*", "memory:*"])
    assert "connections:read-as-owner" in left_out, "an area wildcard does not carry it either"
    asked, left_out = concierge_propose.compute_scopes(["connections"], ["*", "connections:read-as-owner"])
    assert "connections:read-as-owner" in asked and left_out == []
    asked, left_out = concierge_propose.compute_scopes(["connections"], None)
    assert "connections:read-as-owner" not in asked and left_out == ["connections:read-as-owner"]
    assert "connections:read" in asked, "an unknown list still leaves the rest to the node's own ceiling"

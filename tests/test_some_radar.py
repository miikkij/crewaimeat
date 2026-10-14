"""The Social Radar after 2026-10-15: the relevance rule, the three sources, and the age-out. Network
and node mocked; every case is one a production radar got wrong before the change."""

from __future__ import annotations

import datetime

import pytest

from crewaimeat import some_analyst, some_listener, some_sources


class _Resp:
    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status

    def json(self):
        return self._body


# ── the relevance rule ───────────────────────────────────────────────────────────────────────────


def test_words_scattered_across_a_post_are_not_the_phrase():
    # The Ledger spy-chip comment: "agent" in one sentence and "memory" in another.
    assert some_sources.judge("", "The agent was compromised. The memory chip was replaced.") is None


def test_hiring_threads_are_out_whatever_they_contain():
    assert (
        some_sources.judge(
            "Ask HN: Who wants to be hired? (October 2026)", "I build agent memory and multi-agent systems"
        )
        is None
    )
    assert some_sources.judge("Ask HN: Who is hiring? (October 2026)", "agent infrastructure") is None


def test_a_phrase_in_the_title_weighs_more_than_one_in_the_body():
    in_title = some_sources.judge("Show HN: agent memory you own", "a store")
    in_body = some_sources.judge("Show HN: a store", "it is agent memory you own")
    assert in_title and in_body and in_title["fit"] > in_body["fit"]


def test_a_question_is_a_better_place_than_an_announcement():
    q = some_sources.judge("", "How do you keep agent memory across restarts?")
    a = some_sources.judge("", "We shipped agent memory today.")
    assert q["fit"] > a["fit"] and q["why"] == "question / help-seeking"


def test_a_hyphen_and_a_space_are_the_same():
    assert some_sources.phrases_in("multi agent systems") == ["multi-agent"]


def test_off_topic_agents_are_noise():
    assert some_sources.judge("", "my real estate agent memory is bad") is None


# ── the sources ──────────────────────────────────────────────────────────────────────────────────


def test_hacker_news_keeps_one_opportunity_per_story_and_drops_hiring_comments():
    hits = [
        {
            "objectID": "11",
            "story_id": 10,
            "_tags": ["comment"],
            "title": None,
            "story_title": "Agent memory in practice",
            "comment_text": "How do you persist agent memory?",
            "author": "a",
        },
        {
            "objectID": "12",
            "story_id": 10,
            "_tags": ["comment"],
            "story_title": "Agent memory in practice",
            "comment_text": "agent memory is hard",
            "author": "b",
        },
        {
            "objectID": "21",
            "story_id": 20,
            "_tags": ["comment"],
            "story_title": "Ask HN: Who wants to be hired? (October 2026)",
            "comment_text": "Skills: agent memory, multi-agent",
            "author": "c",
        },
    ]
    got = some_sources.hn_hits(0, get=lambda *a, **k: _Resp({"hits": hits}))
    assert [h["key"] for h in got] == ["opp-hn-10"]
    # The story keeps its best comment, the question.
    assert got[0]["url"].endswith("id=11")


def test_bluesky_skips_feed_accounts_and_links_the_post():
    posts = [
        {
            "uri": "at://did:plc:a/app.bsky.feed.post/3abc",
            "author": {"handle": "dev.bsky.social"},
            "record": {"text": "Anyone using agent memory with MCP?", "createdAt": "2026-10-15T01:00:00Z"},
            "likeCount": 3,
            "replyCount": 1,
        },
        {
            "uri": "at://did:plc:b/app.bsky.feed.post/3def",
            "author": {"handle": "arxiv-daily-bot.bsky.social"},
            "record": {"text": "agent memory paper"},
        },
    ]
    got = some_sources.bluesky_hits(
        datetime.datetime(2026, 10, 14, tzinfo=datetime.timezone.utc), get=lambda *a, **k: _Resp({"posts": posts})
    )
    assert len(got) == 1
    assert got[0]["url"] == "https://bsky.app/profile/dev.bsky.social/post/3abc" and got[0]["source"] == "bluesky"


def test_mastodon_keeps_recent_human_statuses_only():
    since = datetime.datetime(2026, 10, 14, tzinfo=datetime.timezone.utc)
    statuses = [
        {
            "url": "https://hachyderm.io/@a/1",
            "created_at": "2026-10-14T10:00:00Z",
            "content": "<p>How do you give <b>AI agents</b> memory?</p>",
            "account": {"acct": "a"},
        },
        {
            "url": "https://hachyderm.io/@b/2",
            "created_at": "2026-10-01T10:00:00Z",
            "content": "<p>ai agents</p>",
            "account": {"acct": "b"},
        },
        {
            "url": "https://hachyderm.io/@c/3",
            "created_at": "2026-10-14T10:00:00Z",
            "content": "<p>ai agents</p>",
            "account": {"acct": "c", "bot": True},
        },
        {
            "url": "https://hachyderm.io/@d/4",
            "created_at": "2026-10-14T10:00:00Z",
            "content": "<p>ai agents</p>",
            "account": {"acct": "d"},
            "reblog": {"id": "x"},
        },
    ]
    got = some_sources.mastodon_hits(since, get=lambda *a, **k: _Resp(statuses))
    assert {h["url"] for h in got} == {"https://hachyderm.io/@a/1"}
    assert "<" not in got[0]["snippet"]


def test_a_source_that_does_not_answer_says_so(capsys):
    def boom(*a, **k):
        raise ConnectionError("down")

    assert some_sources.bluesky_hits(datetime.datetime.now(datetime.timezone.utc), get=boom) == []
    err = capsys.readouterr().err
    assert "Bluesky" in err and "NOT READ" in err


def test_a_refused_source_is_reported_not_taken_for_empty(capsys):
    got = some_sources.mastodon_hits(
        datetime.datetime.now(datetime.timezone.utc), get=lambda *a, **k: _Resp({}, status=503)
    )
    assert got == []
    assert "NOT READ" in capsys.readouterr().err


def test_every_source_keeps_a_share_of_the_radar():
    hits = [{"source": "hn", "rank": 40 - i, "key": f"h{i}"} for i in range(20)]
    hits += [{"source": "bluesky", "rank": 20 - i, "key": f"b{i}"} for i in range(5)]
    hits += [{"source": "mastodon", "rank": 15, "key": "m0"}]
    got = some_listener.share_out(hits, 12)
    by = {s: sum(1 for h in got if h["source"] == s) for s in ("hn", "bluesky", "mastodon")}
    assert len(got) == 12 and by["bluesky"] == 4 and by["mastodon"] == 1 and by["hn"] == 7


# ── the age-out ──────────────────────────────────────────────────────────────────────────────────

TODAY = datetime.date(2026, 10, 15)


def _index(rows_opp, rows_draft=()):
    return {"manifest": {"objectTypes": []}, "index": {"opportunity": list(rows_opp), "reply-draft": list(rows_draft)}}


def _row(rid, updated, title="t"):
    return {"id": rid, "title": title, "updated": f"{updated}T05:00:00.000Z", "published": True}


class _Node:
    def __init__(self, records):
        self.records = records
        self.writes: list[tuple[str, dict]] = []

    def read(self, tool, payload):
        return {
            "items": [
                {"id": i, "space": payload["space"], "published": True, "value": self.records[i]}
                for i in payload["ids"]
                if i in self.records
            ]
        }

    def write(self, tool, payload):
        self.writes.append((tool, payload))
        return {"ok": True}


def test_age_out_skips_only_untouched_old_records_and_hiring_threads():
    node = _Node(
        {
            "opp-old": {"id": "opp-old", "status": "new", "title": "agent memory"},
            "opp-triaged": {"id": "opp-triaged", "status": "triaged", "title": "agent memory"},
            "opp-hire": {"id": "opp-hire", "status": "new", "title": "Ask HN: Who wants to be hired? (October 2026)"},
            "draft-opp-old": {"id": "draft-opp-old", "status": "draft"},
            "draft-posted": {"id": "draft-posted", "status": "posted"},
        }
    )
    index = _index(
        [
            _row("opp-old", "2026-09-20"),
            _row("opp-triaged", "2026-09-20"),
            _row("opp-new", "2026-10-14"),
            _row("opp-hire", "2026-10-14", "Ask HN: Who wants to be hired? (October 2026)"),
        ],
        [_row("draft-opp-old", "2026-09-20"), _row("draft-posted", "2026-09-01")],
    )
    out = some_listener.age_out(index, today=TODAY, read=node.read, write=node.write)
    written = {p["id"]: p["value"] for t, p in node.writes if t == "aimeat_workspace_write"}
    assert set(written) == {"opp-old", "opp-hire", "draft-opp-old"}
    assert all(v["status"] == "skipped" for v in written.values())
    assert "hiring thread" in written["opp-hire"]["angle"]
    assert out["aged"] == 2 and out["drafts"] == 1 and out["failed"] == 0
    # Every write is published into its own space's namespace.
    ns = {p["id"]: p["namespace"] for t, p in node.writes if t == "aimeat_workspace_publish"}
    assert ns["opp-old"] == "shared.opportunities" and ns["draft-opp-old"] == "shared.drafts"


def test_age_out_stops_at_its_budget_and_says_what_is_left():
    node = _Node({f"o{i}": {"id": f"o{i}", "status": "new"} for i in range(10)})
    index = _index([_row(f"o{i}", "2026-09-01") for i in range(10)])
    out = some_listener.age_out(index, today=TODAY, limit=4, read=node.read, write=node.write)
    assert out["aged"] == 4 and out["left"] == 6


def test_age_out_changes_nothing_when_the_records_cannot_be_read():
    index = _index([_row("o1", "2026-09-01")])
    node = _Node({})
    out = some_listener.age_out(index, today=TODAY, read=lambda *a: None, write=node.write)
    assert node.writes == [] and out["failed"] == 1


def test_age_out_without_access_does_nothing():
    assert some_listener.age_out(None, today=TODAY)["no_access"] is True


# ── the drafter ──────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("found", "fresh"),
    [("2026-10-15", True), ("2026-10-08", True), ("2026-10-07", False), ("", False)],
)
def test_only_fresh_opportunities_are_drafted(found, fresh):
    assert some_analyst._fresh({"found_date": found}, TODAY) is fresh

"""The `news` tool: today's headlines from the publishers' own feeds, with no search engine in between.

Why it exists (2026-10-12): two node-defined agents were asked for "today's political news" and had
only the web search. The machine's metasearch was blocked by its sources after about a hundred
queries in an hour (Google News and Startpage: CAPTCHA), every query came back empty, the agent ran
to its iteration limit and the scheduled 18:00 task failed. A feed is read once, answers in a
fraction of a second and has no query to block.
"""

from __future__ import annotations

import pytest

from crewaimeat import news_headlines as nh

NOW = 1_800_000_000.0  # one fixed instant; every age below is counted back from it
H = 3600.0


def _item(title, url, hours_ago, summary="s"):
    return {"title": title, "url": url, "summary": summary, "published": NOW - hours_ago * H}


def _reader(by_url):
    """A feed reader that answers from a dict: a list is the feed's items, a string is why it failed."""

    def read(url):
        got = by_url[url]
        if isinstance(got, str):
            raise nh.FeedNotRead(got)
        return got

    return read


@pytest.fixture
def two_feeds(monkeypatch):
    monkeypatch.setattr(nh, "NEWS_FEEDS", {"politiikka": [("Yle", "u:yle"), ("Ilta-Sanomat", "u:is")]})


def test_one_call_gives_the_days_headlines_from_every_feed_of_the_topic(two_feeds):
    read = _reader(
        {
            "u:yle": [
                _item("Budjettivääntö kiihtyy", "https://yle.fi/a/1", 4),
                _item("Vanha", "https://yle.fi/a/0", 40),
            ],
            "u:is": [_item("Bensatuki tulossa", "https://is.fi/2", 1), _item("Orpo kommentoi", "https://is.fi/3", 2)],
        }
    )
    got = nh.headlines("politiikka", now=NOW, read=read)
    assert [i["title"] for i in got.items] == ["Bensatuki tulossa", "Budjettivääntö kiihtyy", "Orpo kommentoi"], (
        "inside the window only, and the sources take turns so one busy publisher does not fill the list"
    )
    assert {i["source"] for i in got.items} == {"Yle", "Ilta-Sanomat"}
    assert got.problems == [] and got.found == 3
    text = nh.render(got, now=NOW)
    assert "1. Bensatuki tulossa" in text and "Source: Ilta-Sanomat" in text and "URL: https://is.fi/2" in text
    assert "1 h ago" in text and "Vanha" not in text


def test_the_same_story_from_one_address_is_listed_once(two_feeds):
    read = _reader(
        {
            "u:yle": [_item("Sama", "https://yle.fi/a/1", 1), _item("Sama", "https://yle.fi/a/1", 1)],
            "u:is": [],
        }
    )
    assert len(nh.headlines("politiikka", now=NOW, read=read).items) == 1


def test_max_items_cuts_the_list_and_the_answer_says_how_many_there_were(two_feeds):
    read = _reader(
        {
            "u:yle": [_item(f"y{n}", f"https://yle.fi/{n}", n + 1) for n in range(5)],
            "u:is": [_item(f"i{n}", f"https://is.fi/{n}", n + 1) for n in range(5)],
        }
    )
    got = nh.headlines("politiikka", max_items=4, now=NOW, read=read)
    assert len(got.items) == 4 and got.found == 10
    assert [i["source"] for i in got.items].count("Yle") == 2, "the cut keeps both sources"
    assert "10 found, 4 shown" in nh.render(got, now=NOW)


def test_a_feed_that_did_not_answer_is_named_and_the_others_are_still_read(two_feeds):
    read = _reader({"u:yle": "HTTP 503", "u:is": [_item("Bensatuki tulossa", "https://is.fi/2", 1)]})
    got = nh.headlines("politiikka", now=NOW, read=read)
    assert [i["title"] for i in got.items] == ["Bensatuki tulossa"]
    assert got.problems == ["Yle (HTTP 503)"]
    assert "Not read: Yle (HTTP 503)" in nh.render(got, now=NOW)


def test_no_feed_answering_is_said_as_a_fault_and_never_as_a_day_without_news(two_feeds):
    """The run of 2026-10-12 reported "no political news today" when the truth was that the search
    engine was blocked. The two endings must not read alike."""
    got = nh.headlines("politiikka", now=NOW, read=_reader({"u:yle": "timeout", "u:is": "HTTP 500"}))
    text = nh.render(got, now=NOW)
    assert got.items == [] and text.startswith(nh.NOT_READ)
    assert "Yle (timeout)" in text and "Ilta-Sanomat (HTTP 500)" in text
    assert "not a day without news" in text


def test_a_quiet_window_shows_the_newest_items_with_their_age(two_feeds):
    """Early in the morning the last hours can be empty. The feed was read, so the answer is its
    newest items and how old they are, decided here and not by a second call."""
    read = _reader(
        {"u:yle": [_item("Eilinen", "https://yle.fi/a/9", 30)], "u:is": [_item("Toissapäivä", "https://is.fi/8", 50)]}
    )
    got = nh.headlines("politiikka", hours=6, now=NOW, read=read)
    assert got.items == [] and [i["title"] for i in got.older] == ["Eilinen", "Toissapäivä"]
    text = nh.render(got, now=NOW)
    assert text.startswith("Nothing was published for 'politiikka' in the last 6 hours")
    assert "1. Eilinen" in text and "30 h ago" in text


def test_an_unknown_topic_names_the_topics_there_are(two_feeds):
    text = nh.render(nh.headlines("jääkiekko", now=NOW, read=_reader({})), now=NOW)
    assert text.startswith("[news] no feed for the topic 'jääkiekko'") and "politiikka" in text
    assert "web search" in text


def test_a_topic_is_matched_without_case_or_accents_mattering(monkeypatch):
    monkeypatch.setattr(nh, "NEWS_FEEDS", {"paauutiset": [("Yle", "u:yle")]})
    read = _reader({"u:yle": [_item("Otsikko", "https://yle.fi/a/1", 1)]})
    for said in ("paauutiset", "Pääuutiset", " PÄÄUUTISET "):
        assert len(nh.headlines(said, now=NOW, read=read).items) == 1, said


def test_a_feed_is_parsed_into_dated_items_and_an_undated_entry_is_left_out():
    xml = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
    <item><title> Hallitus p\xc3\xa4\xc3\xa4t\xc2\xadti </title><link>https://x.fi/1</link>
      <description>&lt;p&gt;Tuki &lt;b&gt;tulee&lt;/b&gt;.&lt;/p&gt;</description>
      <pubDate>Mon, 12 Oct 2026 14:05:00 GMT</pubDate></item>
    <item><title>Ei p\xc3\xa4iv\xc3\xa4\xc3\xa4</title><link>https://x.fi/2</link></item>
    </channel></rss>"""
    items = nh.parse_feed(xml)
    assert len(items) == 1
    assert items[0]["title"] == "Hallitus päätti", "trimmed, and the publisher's soft hyphen is gone"
    assert items[0]["url"] == "https://x.fi/1"
    assert items[0]["summary"] == "Tuki tulee."
    assert items[0]["published"] == 1791813900.0  # 2026-10-12T14:05:00Z


def test_the_reader_has_a_timeout_and_a_refusal_is_a_reason(monkeypatch):
    seen = {}

    class _Answer:
        def __init__(self, status, content=b""):
            self.status_code, self.content = status, content

    def _get(url, **kw):
        seen.update(kw)
        return _Answer(404)

    import requests

    monkeypatch.setattr(requests, "get", _get)
    with pytest.raises(nh.FeedNotRead, match="HTTP 404"):
        nh.read_feed("https://x.fi/rss")
    assert seen["timeout"] == nh.FEED_TIMEOUT_S

    def _boom(url, **kw):
        raise requests.ConnectionError("no route")

    monkeypatch.setattr(requests, "get", _boom)
    with pytest.raises(nh.FeedNotRead, match="ConnectionError"):
        nh.read_feed("https://x.fi/rss")


def test_every_feed_in_the_registry_is_https_and_named():
    assert {"politiikka", "kotimaa", "paauutiset"} <= set(nh.NEWS_FEEDS)
    for topic, feeds in nh.NEWS_FEEDS.items():
        assert feeds, topic
        for source, url in feeds:
            assert source and url.startswith("https://"), (topic, source, url)
    assert len(nh.NEWS_FEEDS["politiikka"]) >= 4, "one publisher down must not empty the day's politics"


def test_news_is_a_tool_id_a_definition_can_name_and_the_menu_says_when():
    from crewaimeat.crew_def import TOOL_PURPOSES, TOOL_REGISTRY, validate_crew_doc

    assert "news" in TOOL_REGISTRY and "news" in TOOL_PURPOSES
    assert "today's news" in TOOL_PURPOSES["news"] and "not `web`" in TOOL_PURPOSES["news"]
    doc = {
        "agent_name": "x",
        "agents": [{"name": "a", "role": "r", "goal": "g", "backstory": "b", "tools": ["news", "article_fetch"]}],
        "tasks": [{"id": "t", "agent": "a", "description": "{{ctx.prompt}}", "expected_output": "o", "context": []}],
    }
    assert validate_crew_doc(doc) == []


def test_the_tool_answers_in_words_and_is_never_cached(monkeypatch):
    monkeypatch.setattr(nh, "NEWS_FEEDS", {"politiikka": [("Yle", "u:yle")]})
    monkeypatch.setattr(nh, "read_feed", _reader({"u:yle": "timeout"}))
    [tool] = nh.make_news_tools()
    assert tool.name == "news_headlines"
    out = tool.run(topic="politiikka", hours="24", max_items="5")
    assert out.startswith(nh.NOT_READ), "a model sends numbers as strings; that is not a failure"
    assert tool.cache_function("a", "b") is False

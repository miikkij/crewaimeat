"""Today's headlines straight from the publishers' own RSS feeds: the `news` tool of a crew definition.

WHY NOT THE WEB SEARCH. "Today's news" was asked of the web search, and that failed twice in one
afternoon (2026-10-12, two node-defined agents). First the model wrote the date into the query, which
matches no headline. With that corrected, the machine's metasearch was blocked by its own sources
after about a hundred queries in an hour (Google News and Startpage: CAPTCHA, Brave: too many
requests): every query came back empty, the agent searched until its iteration limit, and the
scheduled 18:00 task failed. A search engine is the wrong instrument for a question that has no
query in it. A publisher's feed is the list of what it published, newest first: one read per feed,
a fraction of a second each (measured: 0.1 to 0.4 s), nothing to phrase and nothing to block.

WHAT IS CODE HERE AND WHAT IS THE MODEL'S. Which feeds, which hours, which item is a duplicate and
what to show when the last hours are empty are decided here. The model gets the list and does what
only it can: judge which items matter and write about them.

THE TWO EMPTY ENDINGS DO NOT READ ALIKE. "No feed answered" is a fault of the connection or of the
publishers and starts with `NOT_READ`. "The feeds answered and the window is empty" lists the newest
items with their age. The run that prompted this reported "no political news today" when the truth
was a blocked search engine.

The registry holds only feeds measured to answer with dated items (2026-10-12). `feed_sources` is
the news-fetcher's own registry: it rotates by date and leaves out what its past editions used,
which is right for a newspaper and wrong for "what happened today".
"""

from __future__ import annotations

import calendar
import html
import re
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime

# topic -> [(publisher, feed address)], every publisher's own feed for that topic.
NEWS_FEEDS: dict[str, list[tuple[str, str]]] = {
    "politiikka": [
        ("Yle", "https://feeds.yle.fi/uutiset/v1/recent.rss?publisherIds=YLE_UUTISET&concepts=18-220306"),
        ("Helsingin Sanomat", "https://www.hs.fi/rss/politiikka.xml"),
        ("Ilta-Sanomat", "https://www.is.fi/rss/politiikka.xml"),
        ("Iltalehti", "https://www.iltalehti.fi/rss/politiikka.xml"),
    ],
    "kotimaa": [
        ("Yle", "https://feeds.yle.fi/uutiset/v1/recent.rss?publisherIds=YLE_UUTISET&concepts=18-34837"),
        ("Helsingin Sanomat", "https://www.hs.fi/rss/suomi.xml"),
        ("Ilta-Sanomat", "https://www.is.fi/rss/kotimaa.xml"),
        ("Iltalehti", "https://www.iltalehti.fi/rss/kotimaa.xml"),
    ],
    "paauutiset": [
        ("Yle", "https://feeds.yle.fi/uutiset/v1/majorHeadlines/YLE_UUTISET.rss"),
    ],
}

FEED_TIMEOUT_S = 15  # a feed answers in under a second; this only stops one dead publisher holding the run
OLDER_SHOWN = 5  # how many of the newest items are shown when the window itself is empty
NOT_READ = "[news] NOT READ:"
_AGENT = "Mozilla/5.0 (compatible; crewaimeat news reader)"
_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


class FeedNotRead(Exception):
    """One feed did not give its items; the message is why, in a few words."""


@dataclass
class Headlines:
    topic: str
    hours: int
    known: bool = True  # False: no feed for this topic
    items: list[dict] = field(default_factory=list)  # inside the window, cut to max_items
    older: list[dict] = field(default_factory=list)  # the newest ones, only when the window is empty
    found: int = 0  # inside the window before the cut
    feeds: int = 0
    problems: list[str] = field(default_factory=list)  # "Publisher (why)" for each feed not read


def _plain(text: str) -> str:
    """Feed markup as words. The soft hyphen goes too: two publishers put one inside long words
    ("bensa­tuen") for their own line breaks, and it would travel on into what the model writes."""
    return _SPACE.sub(" ", html.unescape(_TAG.sub("", text or "")).replace("­", "")).strip()


def _topic_key(topic: str) -> str:
    """`Pääuutiset` and `paauutiset` are one topic: a model writes the word, not the registry's key."""
    folded = unicodedata.normalize("NFKD", str(topic or "").strip().lower())
    return "".join(c for c in folded if not unicodedata.combining(c))


def parse_feed(content: bytes) -> list[dict]:
    """A feed's items as `{title, url, summary, published}`, `published` in epoch seconds.

    An entry with no date is left out: whether it is from today cannot be known, and "today" is the
    whole point of the tool.
    """
    import feedparser  # type: ignore

    out = []
    for e in feedparser.parse(content).entries or []:
        title, url = _plain(getattr(e, "title", "")), str(getattr(e, "link", "") or "").strip()
        stamp = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
        if not title or not url or not stamp:
            continue
        summary = _plain(getattr(e, "summary", "") or getattr(e, "description", ""))
        out.append({"title": title, "url": url, "summary": summary, "published": float(calendar.timegm(stamp))})
    return out


def read_feed(url: str) -> list[dict]:
    """Fetch one feed and parse it. Raises FeedNotRead with the reason.

    Fetched with `requests` and a timeout, then parsed: `feedparser.parse(url)` has no timeout of its
    own, so one publisher that accepts the connection and says nothing would hold the run.
    """
    import requests

    try:
        r = requests.get(url, headers={"User-Agent": _AGENT}, timeout=FEED_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 — the reason goes to the answer, by its type
        raise FeedNotRead(type(exc).__name__) from exc
    if r.status_code != 200:
        raise FeedNotRead(f"HTTP {r.status_code}")
    return parse_feed(r.content)


def _take_turns(by_source: list[list[dict]]) -> list[dict]:
    """Every source's items newest first, one from each in turn, each round newest first.

    One publisher posts three times as often as another (measured: 13 against 3 in a day). Sorted by
    time alone, a cut list would be that one publisher's.
    """
    queues = [sorted(items, key=lambda i: -i["published"]) for items in by_source]
    out: list[dict] = []
    while any(queues):
        turn = [q.pop(0) for q in queues if q]
        out.extend(sorted(turn, key=lambda i: -i["published"]))
    return out


def headlines(topic: str = "politiikka", hours: int = 24, max_items: int = 20, *, now=None, read=None) -> Headlines:
    """The items each feed of `topic` published in the last `hours`. `read` and `now` are seams."""
    now = time.time() if now is None else now
    read = read or read_feed
    got = Headlines(topic=str(topic or "").strip(), hours=hours)
    feeds = {_topic_key(k): v for k, v in NEWS_FEEDS.items()}.get(_topic_key(topic))
    if not feeds:
        got.known = False
        return got
    got.feeds = len(feeds)
    seen: set[str] = set()
    inside: list[list[dict]] = []
    outside: list[dict] = []
    for source, url in feeds:
        try:
            items = read(url)
        except FeedNotRead as exc:
            got.problems.append(f"{source} ({exc})")
            continue
        fresh: list[dict] = []
        for item in items:
            if item["url"] in seen:
                continue
            seen.add(item["url"])
            item = {**item, "source": source}
            (fresh if now - item["published"] <= hours * 3600 else outside).append(item)
        inside.append(fresh)
    ordered = _take_turns(inside)
    got.found = len(ordered)
    got.items = ordered[:max_items]
    if not ordered:
        got.older = sorted(outside, key=lambda i: -i["published"])[:OLDER_SHOWN]
    return got


def _line(n: int, item: dict, now: float) -> str:
    age = max(0, round((now - item["published"]) / 3600))
    when = datetime.fromtimestamp(item["published"]).astimezone().strftime("%Y-%m-%d %H:%M")
    summary = item["summary"] or "(no summary)"
    return f"{n}. {item['title']}\n   Source: {item['source']}, published {when} ({age} h ago)\n   URL: {item['url']}\n   {summary}"


def render(got: Headlines, *, now=None) -> str:
    """What the model reads. Its first words say which of the four endings this is."""
    now = time.time() if now is None else now
    if not got.known:
        topics = ", ".join(sorted(NEWS_FEEDS))
        return (
            f"[news] no feed for the topic {got.topic!r}. The topics are: {topics}. "
            "For any other subject use the web search."
        )
    not_read = f"Not read: {'; '.join(got.problems)}." if got.problems else ""
    if got.problems and len(got.problems) == got.feeds:
        return (
            f"{NOT_READ} none of the {got.feeds} feed(s) for {got.topic!r} answered: {'; '.join(got.problems)}. "
            "This is a fault of the connection or of the publishers, not a day without news. Say that the "
            "news could not be read; do not report that there was no news."
        )
    if not got.items:
        head = f"Nothing was published for {got.topic!r} in the last {got.hours} hours. "
        if not got.older:
            return (head + "The feeds answered and hold no dated item at all. " + not_read).strip()
        lines = [head + f"The newest {len(got.older)} item(s), with their age:", ""]
        lines += [_line(n, i, now) for n, i in enumerate(got.older, 1)]
        return "\n".join(lines + ([not_read] if not_read else []))
    shown = f"{got.found} found, {len(got.items)} shown" if got.found > len(got.items) else f"{got.found} found"
    read_at = datetime.fromtimestamp(now).astimezone().strftime("%H:%M")
    lines = [
        f"Headlines for {got.topic!r} from the last {got.hours} hours ({shown}), read from the publishers' own "
        f"feeds at {read_at}:",
        "",
    ]
    lines += [_line(n, i, now) for n, i in enumerate(got.items, 1)]
    return "\n".join(lines + ([not_read] if not_read else []))


def make_news_tools() -> list:
    """The crew_def `news` tool: one call, the day's headlines of a topic."""
    from crewai.tools import tool

    @tool("news_headlines")
    def news_headlines(topic: str = "politiikka", hours: int = 24, max_items: int = 20) -> str:
        """The day's news headlines of one topic, read straight from Finnish publishers' own feeds
        (Yle, Helsingin Sanomat, Ilta-Sanomat, Iltalehti). Each item has its source, when it was
        published, its URL and a summary. This is how you find today's news: ONE call gives the list,
        so call it once per topic and do not repeat it. It takes a TOPIC, not search words:
        politiikka (national politics), kotimaa (domestic news), paauutiset (main headlines).
        `hours` is how far back to look (24 = the last day). When the last hours are empty the answer
        lists the newest items with their age. An answer that starts with "[news] NOT READ" means the
        feeds could not be reached: report that, never that there was no news."""
        return render(headlines(topic, int(hours), int(max_items)))

    news_headlines.cache_function = lambda *_a, **_k: False  # live network: never answer from a cache
    return [news_headlines]

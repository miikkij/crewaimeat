"""The Social Radar's sources and its relevance rule (2026-10-15).

WHY THIS EXISTS. Until 2026-10-15 the radar asked Hacker News's search for each keyword and kept
whatever came back. That search matches the WORDS of a query anywhere in a post, not the phrase, so
"agent memory" found a comment about a hardware wallet that said "agent" in one sentence and "memory"
in another, and every "Who wants to be hired?" comment whose CV listed agent work became its own
opportunity. Measured on 2026-10-15: 1,429 opportunities, 757 drafted replies, 0 posted, and the
latest radar led with a hiring thread and a Ledger spy-chip story.

THE RULE NOW. A hit counts only when one of the radar's PHRASES appears in its own text (a story's
title or body, a comment's text, a post's text). A phrase in the title weighs more than one in the
body. Hiring and job threads are not places to talk about a product, so they are out entirely.

THE SOURCES. Hacker News (Algolia, unchanged), Bluesky's public post search (api.bsky.app answers
without a sign-in; public.api.bsky.app refuses search with 403, measured 2026-10-15), and the public
hashtag timelines of two tech-heavy Mastodon servers. Reddit's search refuses an unauthenticated
request (403, measured the same day), so Reddit stays with the Grok scout.

A SOURCE THAT DOES NOT ANSWER SAYS SO. Each fetcher prints `[some-radar] <source> NOT READ: ...` to
stderr and returns nothing; an empty result and a failed read must not look alike in the log.
"""

from __future__ import annotations

import datetime
import hashlib
import html
import re
import sys

import requests

# The phrases a hit must contain in its own text. Lower-case; a hyphen and a space are the same.
PHRASES = [
    "agent memory",
    "memory for agents",
    "memory layer",
    "persistent memory",
    "shared memory",
    "long-term memory",
    "multi-agent",
    "agent orchestration",
    "agent framework",
    "agent infrastructure",
    "agent backend",
    "stateful agents",
    "long-running agents",
    "autonomous agents",
    "ai agents",
    "agentic",
    "crewai",
    "mcp server",
    "model context protocol",
    "agent identity",
    "agent permissions",
    "knowledge ownership",
    "data ownership",
]

# Threads where a reply about a product is out of place, whatever words they contain.
HIRING = re.compile(
    r"who is hiring|who wants to be hired|seeking freelancer|freelancer\?|seeking work|we'?re hiring|job (board|posting)",
    re.I,
)
# Off-topic uses of "agent", kept from the first version.
NOISE = re.compile(r"\b(real estate|insurance|travel|secret|free|booking|support) agent", re.I)
# A question is a place where a genuine answer helps.
QUESTION = re.compile(
    r"\?|how (do|to|can)|what'?s the best|recommend|anyone (using|tried|know)|looking for|struggl|stuck|alternativ",
    re.I,
)
# Accounts that post feeds, not conversations.
BOTLIKE = re.compile(r"bot\b|bot\.|arxiv|daily|digest|feed|rss|news\.", re.I)


def _norm(text: str) -> str:
    return " ".join(text.lower().replace("-", " ").split())


def phrases_in(text: str) -> list[str]:
    """The radar phrases that appear in `text`, in PHRASES order, each once."""
    t = _norm(text)
    return [p for p in PHRASES if _norm(p) in t]


def plain(markup: str) -> str:
    """HTML (an HN comment, a Mastodon status) as plain text on one line."""
    return " ".join(re.sub(r"<[^>]+>", " ", html.unescape(markup or "")).split())


def judge(title: str, body: str, points: int = 0, comments: int = 0) -> dict | None:
    """Score one hit, or None when it is not one.

    fit (0-5) says how well the subject matches; rank orders the radar and adds engagement on top.
    """
    if HIRING.search(title) or HIRING.search(body[:200]):
        return None
    if NOISE.search(title) or NOISE.search(body):
        return None
    in_title = phrases_in(title)
    in_body = [p for p in phrases_in(body) if p not in in_title]
    if not in_title and not in_body:
        return None
    is_q = bool(QUESTION.search(title) or QUESTION.search(body[:400]))
    fit = 1 + (2 if in_title else 0) + min(len(in_body), 1) + (1 if is_q else 0)
    fit = max(0, min(5, fit))
    engagement = min(points, 50) // 10 + min(comments, 50) // 10
    return {
        "fit": fit,
        "rank": fit * 10 + engagement,
        "why": "question / help-seeking" if is_q else "active discussion",
        "matched": in_title + in_body,
    }


def _short_id(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def _not_read(source: str, exc: object) -> list[dict]:
    print(f"[some-radar] {source} NOT READ: {exc}", file=sys.stderr)
    return []


# ── Hacker News ─────────────────────────────────────────────────────────────────────────────────

_HN = "https://hn.algolia.com/api/v1/search_by_date"
HN_QUERIES = [
    "agent memory",
    "multi-agent",
    "agent framework",
    "mcp server",
    "ai agents",
    "agent infrastructure",
    "crewai",
]


def hn_hits(since_ts: int, per_query: int = 30, get=requests.get) -> list[dict]:
    """Hacker News hits, ONE per story: a comment counts for its story, and the story keeps the best."""
    by_story: dict[str, dict] = {}
    for q in HN_QUERIES:
        try:
            r = get(
                _HN,
                params={
                    "query": q,
                    "tags": "(story,comment)",
                    "numericFilters": f"created_at_i>{since_ts}",
                    "hitsPerPage": per_query,
                },
                timeout=20,
            )
            hits = (r.json() or {}).get("hits") or []
        except Exception as exc:  # noqa: BLE001 — a source that did not answer is reported, not fatal
            _not_read(f"Hacker News ({q})", exc)
            continue
        for h in hits:
            is_comment = "comment" in (h.get("_tags") or [])
            title = str(h.get("title") or h.get("story_title") or "")
            body = plain(h.get("comment_text") or h.get("story_text") or "")
            # A comment is judged on its own words; the story title only names where it was said.
            j = judge(
                title if not is_comment else "",
                body if is_comment else body,
                int(h.get("points") or 0),
                int(h.get("num_comments") or 0),
            )
            if is_comment and HIRING.search(title):
                j = None
            if not j:
                continue
            story = str(h.get("story_id") or h.get("objectID") or "")
            oid = str(h.get("objectID") or story)
            if not story:
                continue
            hit = {
                "source": "hn",
                "key": f"opp-hn-{story}",
                "url": f"https://news.ycombinator.com/item?id={oid}",
                "title": title[:140] or "(comment)",
                "snippet": body[:220],
                "author": h.get("author"),
                "created": h.get("created_at"),
                "points": int(h.get("points") or 0),
                "comments": int(h.get("num_comments") or 0),
                **j,
            }
            prev = by_story.get(hit["key"])
            if prev is None or hit["rank"] > prev["rank"]:
                by_story[hit["key"]] = hit
    return list(by_story.values())


# ── Bluesky ─────────────────────────────────────────────────────────────────────────────────────

_BSKY = "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts"
BSKY_QUERIES = ['"agent memory"', '"ai agents" memory', '"mcp server"', '"multi-agent"', "crewai"]


def bluesky_hits(since: datetime.datetime, per_query: int = 25, get=requests.get) -> list[dict]:
    """Bluesky posts that contain a radar phrase, newest first, from accounts that are not feeds."""
    out: dict[str, dict] = {}
    for q in BSKY_QUERIES:
        try:
            r = get(
                _BSKY,
                params={"q": q, "sort": "latest", "limit": per_query, "since": since.strftime("%Y-%m-%dT%H:%M:%SZ")},
                timeout=20,
            )
            if r.status_code != 200:
                _not_read(f"Bluesky ({q})", f"HTTP {r.status_code}")
                continue
            posts = (r.json() or {}).get("posts") or []
        except Exception as exc:  # noqa: BLE001
            _not_read(f"Bluesky ({q})", exc)
            continue
        for p in posts:
            author = p.get("author") or {}
            handle = str(author.get("handle") or "")
            if not handle or BOTLIKE.search(handle):
                continue
            text = str((p.get("record") or {}).get("text") or "")
            j = judge("", text, int(p.get("likeCount") or 0), int(p.get("replyCount") or 0))
            if not j:
                continue
            uri = str(p.get("uri") or "")
            rkey = uri.rsplit("/", 1)[-1]
            url = f"https://bsky.app/profile/{handle}/post/{rkey}"
            out[url] = {
                "source": "bluesky",
                "key": f"opp-bsky-{_short_id(uri)}",
                "url": url,
                "title": text.split("\n", 1)[0][:140],
                "snippet": " ".join(text.split())[:220],
                "author": handle,
                "created": (p.get("record") or {}).get("createdAt"),
                "points": int(p.get("likeCount") or 0),
                "comments": int(p.get("replyCount") or 0),
                **j,
            }
    return list(out.values())


# ── Mastodon ────────────────────────────────────────────────────────────────────────────────────

# Public hashtag timelines; read-only, no account. Tech-heavy servers, and not mastodon.social.
MASTODON_TIMELINES = [
    ("hachyderm.io", "aiagents"),
    ("hachyderm.io", "mcp"),
    ("fosstodon.org", "aiagents"),
    ("fosstodon.org", "llm"),
]


def mastodon_hits(since: datetime.datetime, per_tag: int = 40, get=requests.get) -> list[dict]:
    """Mastodon statuses under the radar's hashtags that contain a radar phrase in their own text."""
    out: dict[str, dict] = {}
    for server, tag in MASTODON_TIMELINES:
        try:
            r = get(f"https://{server}/api/v1/timelines/tag/{tag}", params={"limit": per_tag}, timeout=20)
            if r.status_code != 200:
                _not_read(f"Mastodon {server} #{tag}", f"HTTP {r.status_code}")
                continue
            statuses = r.json() or []
        except Exception as exc:  # noqa: BLE001
            _not_read(f"Mastodon {server} #{tag}", exc)
            continue
        for s in statuses:
            created = str(s.get("created_at") or "")
            try:
                when = datetime.datetime.fromisoformat(created.replace("Z", "+00:00"))
            except ValueError:
                continue
            if when < since or s.get("reblog"):
                continue
            acct = str((s.get("account") or {}).get("acct") or "")
            if (s.get("account") or {}).get("bot") or BOTLIKE.search(acct):
                continue
            text = plain(str(s.get("content") or ""))
            j = judge("", text, int(s.get("favourites_count") or 0), int(s.get("replies_count") or 0))
            if not j:
                continue
            url = str(s.get("url") or s.get("uri") or "")
            if not url:
                continue
            out[url] = {
                "source": "mastodon",
                "key": f"opp-masto-{_short_id(url)}",
                "url": url,
                "title": text[:140],
                "snippet": text[:220],
                "author": acct,
                "created": created,
                "points": int(s.get("favourites_count") or 0),
                "comments": int(s.get("replies_count") or 0),
                **j,
            }
    return list(out.values())

"""Throwaway trafilatura-extraction worker — process isolation for the native crash class.

trafilatura parses arbitrary, often-malformed web HTML through lxml → libxml2 (C). A bad page
can hard-kill the process with a Windows native fast-fail (exit 0xC0000409 /
STATUS_STACK_BUFFER_OVERRUN) that Python CANNOT catch — it took down the long-lived news-fetcher
crew daemon repeatedly. The robust fix (independent of the bundled libxml2 version) is to do every
extraction in a SHORT-LIVED SUBPROCESS: if libxml2 crashes here, only this throwaway process dies;
the caller sees a non-zero exit and skips that one URL, and the crew daemon lives on.

Run as:  python -m crewaimeat._extract_worker --url <url>     (fetch the URL, then extract)
         python -m crewaimeat._extract_worker --html          (extract HTML read from stdin)
Prints a single-line JSON object {"text": "<extracted>"} to stdout (empty text on no content).
"""

from __future__ import annotations

import json
import sys


def main() -> None:
    import trafilatura

    args = sys.argv[1:]
    html = None
    if "--url" in args:
        url = args[args.index("--url") + 1]
        html = trafilatura.fetch_url(url)
    elif "--html" in args:
        try:  # the caller pipes UTF-8; Windows stdin defaults to cp1252 → reconfigure or it mangles ä/ö
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
        html = sys.stdin.read()
    text, date = "", None
    if html:
        text = trafilatura.extract(html, include_comments=False, favor_recall=True) or ""
        # WHEN THE PAGE SAYS IT WAS PUBLISHED. Without this the raw carried only `fetched_at` —
        # when WE looked — and a writer with no publication date cannot tell a story from last
        # night from one from 2024. It did not abstain: it invented "viime torstaina" for an event
        # that was two years old. Extracted here because the metadata comes from the same parsed
        # HTML, and this subprocess is the only place allowed to touch lxml.
        try:
            md = trafilatura.extract_metadata(html)
            date = getattr(md, "date", None) or None
        except Exception:  # noqa: BLE001 — a page with no/odd metadata is undated, not a failure
            date = None
    sys.stdout.write(
        json.dumps({"text": text, "date": date})
    )  # ensure_ascii=True: pure-ASCII stdout survives any (cp1252) Windows pipe encoding


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 — a clean error still returns empty (never crash the caller's parse)
        sys.stdout.write(
            json.dumps({"text": "", "error": str(exc)[:200]})
        )  # ensure_ascii=True: pure-ASCII stdout survives any (cp1252) Windows pipe encoding

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
    text = ""
    if html:
        text = trafilatura.extract(html, include_comments=False, favor_recall=True) or ""
    sys.stdout.write(json.dumps({"text": text}))  # ensure_ascii=True: pure-ASCII stdout survives any (cp1252) Windows pipe encoding


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001 — a clean error still returns empty (never crash the caller's parse)
        sys.stdout.write(json.dumps({"text": "", "error": str(exc)[:200]}))  # ensure_ascii=True: pure-ASCII stdout survives any (cp1252) Windows pipe encoding

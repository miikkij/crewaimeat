"""The Grok run, closed into a loop that keeps its results.

TODAY the morning mail prints a prompt, you run it in Grok, and you REPLY TO THE EMAIL with the
output. postman parses that reply into Social Radar opportunities. It works, but the run itself is
never kept: the prompt, the raw output and what came of it live only in a mailbox thread, so nobody
can ask "what did we scout last week, and did any of it turn into anything?".

This closes the loop without changing what already works:

  app  ->  writes the raw paste to `some.grok.inbox.<ts>`   (one small owner-visible record)
  agent->  drain() parses it with the SAME parser the mail path uses (`_ingest_radar_lines`), so
           there is exactly one definition of the line format, and archives the run
  archive: `some.grok.<date>` keeps the raw text, the parse result and the ingested hits — the run
           becomes a dated, reviewable object instead of a mail you have to find again.

The app never writes to the Social Radar workspace and never parses anything. It drops off raw text;
the structuring is an agent's job, which is also the only place the parser and the workspace
credentials belong.
"""

from __future__ import annotations

import datetime
import sys

from crewaimeat.aimeat_crew import _aimeat_call

AGENT = "postman"
INBOX_PREFIX = "some.grok.inbox."
PROCESSED_TTL_H = 48  # a drained paste is kept briefly for inspection, then the node ages it out


def submit(text: str, by: str = "app", agent: str = AGENT, now=None) -> str | None:
    """Drop a raw Grok output into the inbox. Returns the key, or None if the write failed.

    Used by the app (through the data lib) and available here so the same path can be exercised
    without a browser."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    key = f"{INBOX_PREFIX}{now.strftime('%Y%m%dT%H%M%S')}"
    value = {"pasted_at": now.isoformat(timespec="seconds"), "by": by, "text": text, "status": "pending"}
    # PROVENANCE: this is verbatim output from somebody ELSE's model (Grok), relayed by us. We are
    # not its author and cannot attest to how it was made, so nothing is declared here — the same
    # reasoning fetch_pipeline applies to scraped press text. What we DO record is that it is a
    # relayed paste, in `by`, which is the honest claim.
    if _aimeat_call(agent, "aimeat_memory_write", {"key": key, "value": value, "visibility": "owner"}) is None:
        print(f"[{agent}] grok inbox: write {key} FAILED", file=sys.stderr)
        return None
    return key


def pending(agent: str = AGENT) -> list[dict]:
    """Pastes waiting to be structured."""
    r = _aimeat_call(agent, "aimeat_memory_list", {"owner_scope": True, "prefix": INBOX_PREFIX, "limit": 100})
    items = (r.get("items") or []) if isinstance(r, dict) else []
    out = []
    for it in items:
        v = it.get("value")
        if v is None:
            v = (
                _aimeat_call(agent, "aimeat_memory_read", {"key": it.get("key"), "owner_scope": True}, quiet=True) or {}
            ).get("value")
        if isinstance(v, dict) and v.get("status") == "pending":
            out.append({"key": it.get("key"), **v})
    out.sort(key=lambda x: x.get("pasted_at") or "")
    return out


def drain(agent: str = AGENT) -> dict:
    """Structure every pending paste: parse -> Social Radar -> dated archive. Returns a summary.

    Idempotent by construction: a drained paste is marked `done`, and `_ingest_radar_lines` skips a
    URL that is already an opportunity, so running this twice adds nothing twice."""
    from crewaimeat.mail_contract import _ingest_radar_lines

    items = pending(agent)
    runs = []
    for p in items:
        found_date = (p.get("pasted_at") or "")[:10] or datetime.date.today().isoformat()
        try:
            res = _ingest_radar_lines(p.get("text") or "", found_date)
        except Exception as exc:  # noqa: BLE001 — one bad paste must not block the rest
            print(f"[{agent}] grok drain: {p['key']} FAILED ({exc!r})", file=sys.stderr)
            res = {"error": repr(exc)[:200]}
        run = {
            "key": p["key"],
            "pasted_at": p.get("pasted_at"),
            "by": p.get("by"),
            "result": res,
            "text": (p.get("text") or "")[:8000],  # the raw run is the evidence; keep it, bounded
        }
        runs.append(run)
        _archive(found_date, run, agent)
        # Mark drained with a short ttl: kept long enough to inspect what was parsed, then gone, so
        # the inbox cannot become a second silent backlog like the 297 drafts did.
        _aimeat_call(
            agent,
            "aimeat_memory_write",
            {
                "key": p["key"],
                "value": {**{k: v for k, v in p.items() if k != "key"}, "status": "done", "result": res},
                "visibility": "owner",
                "ttl_hours": PROCESSED_TTL_H,
            },
        )
    if runs:
        print(f"[{agent}] grok drain: {len(runs)} paste(s) structured", file=sys.stderr)
    return {"drained": len(runs), "runs": runs}


def _archive(date: str, run: dict, agent: str = AGENT) -> None:
    """Append one run to `some.grok.<date>` — the dated, reviewable record of what was scouted."""
    key = f"some.grok.{date}"
    prev = (_aimeat_call(agent, "aimeat_memory_read", {"key": key}, quiet=True) or {}).get("value")
    runs = (prev or {}).get("runs") if isinstance(prev, dict) else None
    runs = (runs if isinstance(runs, list) else []) + [run]
    if (
        _aimeat_call(
            agent,
            "aimeat_memory_write",
            {"key": key, "value": {"date": date, "runs": runs}, "visibility": "owner"},
        )
        is None
    ):
        print(f"[{agent}] grok archive: write {key} FAILED", file=sys.stderr)

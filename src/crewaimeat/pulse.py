"""What the organism ACTUALLY did in the last 24 h — measured, not narrated.

WHY THIS EXISTS. The morning report's analysis (`mail_contract._insights_section`) was fed exactly
two things: workspace-object events and the SOME radar. It could not see memory writes, workflow
runs, agent tasks or spend. So on the night the fleet published a 24-article newspaper with every
workflow step green, the briefing opened with "ei syntynyt konkreettista toimintaa — ei koodia,
dokumentaatiota eikä julkaisua". The model was not wrong; it described the empty input it was given.

`collect()` gathers the signals that were missing, as NUMBERS a reader can check:
  · the edition — how many raw categories, articles, whether quiz/editorial/frontpage landed
  · spend — LLM calls, tokens and cost from the node ledger, per agent
  · memory — how many keys each namespace gained in the window
It is written to `mail.pulse.<date>` (+ `.latest`) so the same numbers serve the mail analysis AND
the Aamukatsaus app, and so a day can be compared against the days before it.

Everything degrades to None rather than raising: this is instrumentation, and instrumentation that
takes the morning mail down with it is worse than no instrumentation.
"""

from __future__ import annotations

import datetime
import sys
from typing import Any

from crewaimeat.aimeat_crew import _aimeat_call, _aimeat_rest

AGENT = "postman"  # the morning-report agent; the pulse is written under its own gaii

# The namespaces worth counting. Each is a thing the fleet visibly produces, so a day with zero in
# all of them really was a quiet day — which is exactly the claim the old analysis could not justify.
_WATCHED = ("news.", "some.", "crews.", "eco.", "feedback.", "mail.", "agents.")


def _list(prefix: str, agent: str = AGENT) -> list[dict]:
    # include=meta so `bytes`/`updated_at` come back — without it the listing carries neither, and a
    # size rendered from the missing field reads as "0 kB" next to 24 real articles.
    r = _aimeat_call(
        agent,
        "aimeat_memory_list",
        {"owner_scope": True, "prefix": prefix, "limit": 500, "include": "meta"},
        quiet=True,
    )
    return (r.get("items") or []) if isinstance(r, dict) else []


def _kb(n: int | None) -> str:
    """Size for a human, or nothing at all when the node did not report one. An unknown size must
    never render as `0 kB` — that is a claim, and the wrong one."""
    return f", {round((n or 0) / 1024)} kB" if n else ""


def edition_health(date: str, edition: str = "evening", agent: str = AGENT) -> dict:
    """The newspaper, counted from the keys it actually wrote.

    This is the single loudest piece of evidence the old input was missing: a complete edition is
    ~25 keys and roughly 20 articles, and none of it reached the analysis."""
    items = _list(f"news.{date}.{edition}.", agent)
    keys = [i.get("key", "") for i in items]
    raw = next((i for i in items if i.get("key", "").endswith(".raw")), None)
    cats = None
    if raw is not None:
        # The consolidated raw record holds every category under `categories` (one key since
        # 2026-08-10). Read it only if the listing did not already carry the value.
        val = raw.get("value")
        if val is None:
            val = (
                _aimeat_call(agent, "aimeat_memory_read", {"key": raw["key"], "owner_scope": True}, quiet=True) or {}
            ).get("value")
        if isinstance(val, str):
            try:
                import json

                val = json.loads(val)
            except ValueError:
                val = None
        if isinstance(val, dict) and isinstance(val.get("categories"), dict):
            cats = sum(1 for v in val["categories"].values() if v)
    return {
        "date": date,
        "edition": edition,
        "keys": len(keys),
        "articles": sum(1 for k in keys if ".article." in k),
        "raw_categories": cats,
        "quiz": any(k.endswith(".quiz") for k in keys),
        "editorial": any(k.endswith(".editorial") for k in keys),
        "status": next(
            (
                i.get("value")
                for i in items
                if i.get("key", "").endswith(".status") and isinstance(i.get("value"), dict)
            ),
            None,
        ),
        "bytes": sum(int(i.get("bytes") or 0) for i in items),
    }


def spend(since_date: str, until_date: str, agent: str = AGENT) -> dict | None:
    """LLM calls / tokens / cost from the node ledger, per agent. None when the ledger is unreachable.

    Worth having beside the narrative: "a quiet day" and "a day that cost $0.03 across four agents"
    are the same sentence, but only one of them can be checked."""
    d = _aimeat_rest(agent, "GET", f"/v1/ledger/usage?from={since_date}&to={until_date}&group_by=agent")
    if not isinstance(d, dict):
        return None
    totals = d.get("totals") or {}
    groups = [
        {
            "agent": (g.get("key") or "").split("#")[0],
            "calls": g.get("calls"),
            "tokens": g.get("total_tokens"),
            "cost_usd": g.get("cost_usd"),
        }
        for g in (d.get("groups") or [])
    ]
    groups.sort(key=lambda g: -(g.get("cost_usd") or 0))
    return {"totals": totals, "by_agent": groups}


def memory_activity(since_iso: str, agent: str = AGENT) -> list[dict]:
    """Keys written per watched namespace inside the window — the fleet's visible output."""
    out = []
    for prefix in _WATCHED:
        items = _list(prefix, agent)
        fresh = [i for i in items if (i.get("updated_at") or "") >= since_iso]
        if fresh:
            out.append(
                {
                    "namespace": prefix.rstrip("."),
                    "written": len(fresh),
                    "bytes": sum(int(i.get("bytes") or 0) for i in fresh),
                    "examples": [i.get("key") for i in fresh[:3]],
                }
            )
    out.sort(key=lambda x: -x["written"])
    return out


def collect(now: datetime.datetime | None = None, agent: str = AGENT) -> dict:
    """One measured snapshot of the last 24 h. Never raises — a failed probe becomes None."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    today = now.date()
    since = now - datetime.timedelta(hours=24)
    out: dict[str, Any] = {
        "generated_at": now.isoformat(timespec="seconds"),
        "window_hours": 24,
        "date": today.isoformat(),
    }
    for name, fn in (
        # The edition published in this window carries YESTERDAY's date when the run crosses midnight
        # (the workflow fires 00:17 Helsinki), so both are probed and the fuller one wins.
        (
            "edition",
            lambda: max(
                (edition_health(d.isoformat(), agent=agent) for d in (today, today - datetime.timedelta(days=1))),
                key=lambda e: e["keys"],
            ),
        ),
        ("spend", lambda: spend(since.date().isoformat(), (today + datetime.timedelta(days=1)).isoformat(), agent)),
        ("memory", lambda: memory_activity(since.isoformat(), agent)),
    ):
        try:
            out[name] = fn()
        except Exception as exc:  # noqa: BLE001 — instrumentation never takes the mail down
            print(f"[{agent}] pulse: {name} failed ({exc!r})", file=sys.stderr)
            out[name] = None
    return out


def as_prompt_lines(p: dict) -> str:
    """The pulse rendered for the morning analyst's prompt — the input that was missing.

    Deliberately terse and numeric: the analyst's job is to interpret, and it interprets better from
    counts it cannot argue with than from prose it can echo."""
    if not isinstance(p, dict):
        return "(no pulse data)"
    lines = []
    e = p.get("edition")
    if e:
        state = "complete" if (e.get("articles") or 0) >= 12 and e.get("editorial") else "partial/none"
        lines.append(
            f"- NEWSPAPER {e['date']} {e['edition']}: {state} — {e.get('articles')} articles, "
            f"{e.get('raw_categories')} raw categories, quiz={e.get('quiz')}, editorial={e.get('editorial')}, "
            f"{e.get('keys')} memory keys{_kb(e.get('bytes'))}"
        )
    s = p.get("spend") or {}
    t = s.get("totals") or {}
    if t:
        by = ", ".join(f"{g['agent']} {g['calls']}" for g in (s.get("by_agent") or [])[:5])
        lines.append(
            f"- LLM SPEND: {t.get('calls')} calls, {t.get('total_tokens')} tokens, "
            f"${t.get('cost_usd')} — busiest: {by or 'none'}"
        )
    for m in p.get("memory") or []:
        lines.append(f"- MEMORY {m['namespace']}: {m['written']} keys written{_kb(m.get('bytes'))}")
    return "\n".join(lines) or "(pulse collected, but every signal was empty — a genuinely quiet 24h)"


def publish(p: dict | None = None, agent: str = AGENT) -> dict:
    """Write the snapshot to `mail.pulse.<date>` and `mail.pulse.latest` (owner-visible).

    Per-day AND latest, because the app needs today fast and the trend needs yesterday — the morning
    report itself had only a `.latest` that each morning overwrote, so no day could be compared to
    the one before it."""
    p = p or collect(agent=agent)
    for key in (f"mail.pulse.{p['date']}", "mail.pulse.latest"):
        # No ai_provenance: every field here is a COUNT this code measured, not model-written prose.
        # Declaring a level would attribute authorship to a model that never touched it.
        if _aimeat_call(agent, "aimeat_memory_write", {"key": key, "value": p, "visibility": "owner"}) is None:
            print(f"[{agent}] pulse: write {key} FAILED", file=sys.stderr)
    return p

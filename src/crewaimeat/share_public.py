"""The Aamukatsaus public surface — one explicit, auditable list of what leaves the owner's bubble.

WHY A MIRROR AND NOT A VISIBILITY FLIP. The briefing, the pulse, the review queue and the Grok
archive are produced as `owner`-visible working data. Making colleagues able to see them could be
done by flipping each producer's `visibility` to `public`, but then "what is public?" would be a
property scattered across four modules and answerable only by reading all of them. Here it is ONE
list, in one file, that a person can read in ten seconds and a reviewer can object to.

WHAT `public` ACTUALLY MEANS HERE — measured 2026-08-11, not assumed:
  GET /v1/memory/<gaii>/<key>  returns a public record to an ANONYMOUS caller, no login.
  `owner` and `group` both refuse it (403). A group member on another owner could not read a
  group-visibility key through any path available at the time, which is why this is the mechanism
  the owner chose for now.
So `public` is NOT access control. Anyone who learns a GAII and a key can read it, forever, without
signing in. The owner accepted that trade for this data; the point of the list below is that the
trade stays visible and reversible rather than becoming an accident.

WHY AN INDEX KEY IS REQUIRED. `getPublic()` needs an EXACT key, and there is no public listing —
an app cannot enumerate another principal's keys. Without `aamukatsaus.share.index` the app could
never discover which dates exist, so the archive and the date picker would be impossible.

ONE WRITER. Everything is mirrored under `postman`'s GAII even when another agent produced it, so
the app needs exactly one GAII and no per-key lookup table.
"""

from __future__ import annotations

import datetime
import sys

from crewaimeat.aimeat_crew import _aimeat_call

WRITER = "postman"  # the single GAII the app reads from
INDEX_KEY = "aamukatsaus.share.index"

# Keep this many dated snapshots in the public archive. Older days stay owner-visible only — the
# public surface is a window, not a permanent republication of everything the fleet ever measured.
KEEP_DAYS = 30


def _list(prefix: str, agent: str) -> list[dict]:
    r = _aimeat_call(agent, "aimeat_memory_list", {"owner_scope": True, "prefix": prefix, "limit": 200}, quiet=True)
    return (r.get("items") or []) if isinstance(r, dict) else []


def _read(key: str, agent: str):
    return (_aimeat_call(agent, "aimeat_memory_read", {"key": key, "owner_scope": True}, quiet=True) or {}).get("value")


def _publish(key: str, value) -> bool:
    """Write one public copy. No ai_provenance: a mirror re-publishes a record that already carries
    its own declaration (or is a measured count), and re-declaring here would name US as the author
    of prose a different agent wrote."""
    ok = _aimeat_call(WRITER, "aimeat_memory_write", {"key": key, "value": value, "visibility": "public"})
    if ok is None:
        print(f"[{WRITER}] share: publish {key} FAILED", file=sys.stderr)
    return ok is not None


APP_FILE = "aamukatsaus.html"
FACE_KEY = f"apps.{APP_FILE}.agentface"


def _agent_face(index: dict, pulse: dict | None, queue: dict | None) -> bool:
    """The app's READ surface for agents — the markdown the node serves on the app URL when the
    request prefers text/markdown (`?format=md`).

    Written HERE, on the same pass that refreshes the mirror, because the spec's rule is that the
    face updates on the same writes as the visible view: an agent and a human must never be looking
    at different states. It carries DATA and POINTERS only — no UI chrome, no buttons, and nothing
    that is not already public, since the face is served to anonymous readers. The node appends its
    own "Agent affordances" footer, so none is written here.

    owner_scope: the face must live under the APP OWNER, not under postman — an app-owned record in
    a writing agent's namespace is not the one the node serves."""
    e = (pulse or {}).get("edition") or {}
    t = ((pulse or {}).get("spend") or {}).get("totals") or {}
    b = (queue or {}).get("backlog") or {}
    lines = [
        "# Aamukatsaus",
        "",
        "Daily briefing for the crewaimeat organism: the morning report, measured pulse, the "
        "reply-review queue and the Grok scouting archive. All figures below are public records.",
        "",
        "## Latest pulse",
        f"- Newspaper {e.get('date', '?')} {e.get('edition', '')}: {e.get('articles', '?')} articles, "
        f"{e.get('raw_categories', '?')} raw categories, quiz={e.get('quiz')}, editorial={e.get('editorial')}",
        f"- LLM spend (24 h): {t.get('calls', '?')} calls, {t.get('total_tokens', '?')} tokens, ${t.get('cost_usd', '?')}",
        "",
        "## Review queue",
        f"- {len(((queue or {}).get('items')) or [])} draft(s) awaiting a human, "
        f"{b.get('drafts_expired', '?')} expired, {b.get('posted_total', '?')} ever posted",
        "",
        "## Data",
        f"Public records under `{PUB_PREFIXES}`, read with "
        "`AIMEAT.data.getPublic(<writer gaii>, key)`. Start from the index — `getPublic` needs an "
        "exact key and public keys cannot be listed:",
        f"- `{INDEX_KEY}` — what is shared, and which dated keys exist",
        "- `mail.morning.public.<date>` / `.latest` — the briefing (markdown in `body_md`)",
        "- `mail.pulse.public.<date>` / `.latest` — measured edition, spend and memory counts",
        "- `some.queue.public.latest` — the review queue with thread URLs and draft text",
        "- `some.grok.public.<date>` — archived Grok scouting runs",
        "",
        f"Archive depth: {KEEP_DAYS} days. Refreshed once a day by the morning pass (`crewaimeat.share_public.sync`).",
    ]
    ok = _aimeat_call(
        WRITER,
        "aimeat_memory_write",
        {"key": FACE_KEY, "value": "\n".join(lines), "visibility": "public", "owner_scope": True},
    )
    if ok is None:
        print(f"[{WRITER}] share: agent face {FACE_KEY} FAILED", file=sys.stderr)
    return ok is not None


PUB_PREFIXES = "mail.morning.public.* · mail.pulse.public.* · some.queue.public.* · some.grok.public.*"


HOME_ORG = "b784641b-a4dd-4d69-adb6-9954dc813e1e"
HOME_WS = "ws-mq5vvdgsjwp"  # Internal — where the morning report's mail-request records live
_BODY_CAP = 6000  # mail_contract stores body_md[:6000] in the record; longer briefings are cut


def backfill_briefings(limit: int = 60) -> dict:
    """Recover the briefing archive from the `morning-<date>` mail-request records.

    WHY THIS IS NEEDED. `mail.morning.public.latest` is overwritten every morning, and the dated key
    that preserves each day only started being written on 2026-08-11. Every briefing before that
    exists as prose in a sent email and as a `mail-request` record — but nowhere the app can read.
    58 of them were sitting in the Internal workspace while the app said "the archive starts with
    the next run".

    HONESTY: the record stores `body_md[:6000]`, so most recovered days are TRUNCATED. Each one is
    marked `truncated: true` and carries `recovered_from`, because a briefing that stops mid-sentence
    must not be presented as the whole thing. The full text of an old day exists only in the email
    that was sent.

    Gaps only — a dated key that already exists is never replaced by a truncated recovery."""
    idx_now = {it.get("key") for it in _list("mail.morning.public.", WRITER)}
    d = _aimeat_call(WRITER, "aimeat_workspace_read", {"organism_id": HOME_ORG, "ws": HOME_WS}) or {}
    reqs = [o for o in ((d.get("objects") or {}).get("mail-request") or []) if isinstance(o, dict)]
    mornings = sorted(
        (o for o in reqs if str(o.get("id", "")).startswith("morning-")),
        key=lambda o: o.get("id", ""),
        reverse=True,
    )[:limit]

    written, skipped = [], 0
    for o in mornings:
        date = str(o.get("id", ""))[len("morning-") :]
        key = f"mail.morning.public.{date}"
        if key in idx_now:
            skipped += 1
            continue
        body = o.get("body_md") or ""
        if not body.strip():
            continue
        value = {
            "date": date,
            "subject": o.get("subject") or f"Aamuraportti · {date}",
            "body_md": body,
            "recovered_from": f"mail-request/{o.get('id')}",
            "truncated": len(body) >= _BODY_CAP,
        }
        if _publish(key, value):
            written.append(key)
    print(
        f"[{WRITER}] backfill: {len(written)} briefing(s) recovered, {skipped} already present "
        f"({sum(1 for k in written if k)} of them from truncated records)",
        file=sys.stderr,
    )
    return {"written": written, "skipped": skipped}


def sync(now: datetime.datetime | None = None) -> dict:
    """Mirror the current Aamukatsaus data to public keys and rewrite the index.

    Returns {published:[keys], index}. Best-effort per key: one failure is logged and the rest still
    go, because a half-published surface with an honest index beats no surface at all."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    cutoff = (now.date() - datetime.timedelta(days=KEEP_DAYS)).isoformat()
    published: list[str] = []
    index: dict = {
        "generated_at": now.isoformat(timespec="seconds"),
        "writer_gaii": None,  # filled by the app from the key it already reads; kept for clarity
        "note": "Public mirror of the Aamukatsaus data. Readable by anyone who knows this GAII + key.",
        "briefing": [],
        "pulse": [],
        "queue": None,
        "grok": [],
    }

    # 1) BRIEFING — already written public by mail_contract; the index only has to point at the days.
    for it in _list("mail.morning.public.", WRITER):
        k = it.get("key") or ""
        if k.endswith(".latest") or k[-10:].count("-") != 2:
            continue
        if k[-10:] >= cutoff:
            index["briefing"].append(k)
    index["briefing"].sort(reverse=True)

    # 2) PULSE — produced `owner`; mirrored per day + latest.
    for it in _list("mail.pulse.", WRITER):
        k = it.get("key") or ""
        # Skip our OWN output: the prefix listing also returns `mail.pulse.public.<date>` from the
        # last run, so without this the mirror re-mirrors its own copies and every day appears twice
        # in the index — visible in the app as a duplicated "Viime päivät" row.
        if not k.startswith("mail.pulse.") or k.endswith(".latest") or ".public." in k:
            continue
        day = k.rsplit(".", 1)[-1]
        if day < cutoff or day.count("-") != 2:
            continue
        v = _read(k, WRITER)
        if v is not None and _publish(f"mail.pulse.public.{day}", v):
            published.append(f"mail.pulse.public.{day}")
            index["pulse"].append(f"mail.pulse.public.{day}")
    index["pulse"].sort(reverse=True)
    latest_pulse = _read("mail.pulse.latest", WRITER)
    if latest_pulse is not None and _publish("mail.pulse.public.latest", latest_pulse):
        published.append("mail.pulse.public.latest")

    # 3) REVIEW QUEUE — produced by some-analyst; mirrored under the one writer so the app needs
    #    a single GAII. The drafts are the paper's own unsent replies, nothing third-party.
    q = _read("some.queue.latest", "some-analyst")
    if q is not None and _publish("some.queue.public.latest", q):
        published.append("some.queue.public.latest")
        index["queue"] = "some.queue.public.latest"

    # 4) GROK ARCHIVE — dated scouting runs.
    for it in _list("some.grok.", WRITER):
        k = it.get("key") or ""
        day = k.rsplit(".", 1)[-1]
        # `.public.` for the same self-mirroring reason as the pulse above; `.inbox.` is the raw
        # paste queue, which is working state and never published.
        if ".inbox." in k or ".public." in k or day.count("-") != 2 or day < cutoff:
            continue
        v = _read(k, WRITER)
        if v is not None and _publish(f"some.grok.public.{day}", v):
            published.append(f"some.grok.public.{day}")
            index["grok"].append(f"some.grok.public.{day}")
    index["grok"].sort(reverse=True)

    _publish(INDEX_KEY, index)
    _agent_face(index, latest_pulse, q)
    print(
        f"[{WRITER}] share: {len(published)} public key(s) + index "
        f"({len(index['briefing'])} briefing, {len(index['pulse'])} pulse, {len(index['grok'])} grok)",
        file=sys.stderr,
    )
    return {"published": published, "index": index}

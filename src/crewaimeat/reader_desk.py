"""Lukijoilta-deski — the reader/owner news intake for (L)AIMEAT Sanomat (deterministic half).

Two inflows feed ONE raw key, and the EXISTING deterministic write pipeline does the rest:
  1. INTERVIEW: a daily schedule fires a kickoff -> sanomat-desk DMs the owner a short interview
     ("mitä tänään tapahtui?"); the owner's replies (text + photos) in that thread become tips.
  2. TIPS: any logged-in federation user DMs sanomat-desk a news tip (text + attachments). External
     senders' material passes the legal screen (crewaimeat.legal_screen) BEFORE it becomes raw;
     the owner's own material does not (it is our own production, per the owner's scoping).

A tip is appended to `news.<date>.evening.raw.lukijoilta` in the SAME shape news-fetcher uses
([{title, content, url?, images?}]), so write_pipeline's desk-A loop writes the "lukijoilta" article
with its own persona — no new write path. Attached images are re-published PUBLIC to this agent's
storage so the article can embed them as /v1/pub URLs (a DM attachment itself stays private).

This module is deterministic (config, addressing, key I/O, edition cutoff); the judgement lives in
the crew file (crews/sanomat_desk_crew.py) and in legal_screen/corrections.
"""

from __future__ import annotations

import datetime
import re
import sys
from zoneinfo import ZoneInfo

from crewaimeat import dm, orchestrator, session_store, storage
from crewaimeat.aimeat_crew import _aimeat_call
from crewaimeat.generator_tool import _discover_owner, _token

AGENT_NAME = "sanomat-desk"
_CONFIG_CONV = "_sanomat_desk"  # fixed session_store pseudo-conversation for this agent's own config
_CONFIG_KEY = "config"
KICKOFF_MARKER = "SANOMAT_INTERVIEW_KICKOFF"  # scheduled task_description carries this

# The evening write step runs at 18:00 Europe/Helsinki; raw landing after this cutoff would race the
# desk write, so late tips roll to the NEXT day's edition (the ack tells the sender which one).
EDITION_CUTOFF = (17, 30)
_TZ = ZoneInfo("Europe/Helsinki")


# ── config (durable, local) ──────────────────────────────────────────────────
def get_config(agent: str = AGENT_NAME) -> dict:
    return session_store.session_get(agent, _CONFIG_CONV, _CONFIG_KEY) or {}


def set_config(agent: str, **changes) -> dict:
    cfg = get_config(agent)
    cfg.update({k: v for k, v in changes.items() if v is not None})
    session_store.session_set(agent, _CONFIG_CONV, _CONFIG_KEY, cfg)
    return cfg


# ── owner addressing (same shape as social_briefing — same-owner DM is ungated) ──
def own_gaii(agent: str = AGENT_NAME) -> str | None:
    """This agent's full GAII (name#owner@node) from the live roster."""
    for a in orchestrator.list_node_agents(agent):
        if a.get("name") == agent and a.get("gaii"):
            return a["gaii"]
    return None


def owner_gaii(agent: str = AGENT_NAME) -> str | None:
    g = own_gaii(agent)
    return g.split("#", 1)[1] if g and "#" in g else None


def is_owner_human(agent: str, sender: str | None) -> bool:
    """True when the DM sender is this agent's OWNER as a person (owner@node) — not a sibling agent
    (agent#owner@node) and not a stranger."""
    if not sender:
        return False
    head = sender.split("@")[0]
    return "#" not in head and head == _discover_owner(agent)


# ── edition targeting ────────────────────────────────────────────────────────
def next_evening_edition(now: datetime.datetime | None = None) -> tuple[str, str]:
    """(date, edition) the next tip should land in: today's evening edition until the cutoff,
    tomorrow's after it (the 18:00 desk write must not race a still-landing tip)."""
    now = now or datetime.datetime.now(_TZ)
    date = now.date()
    if (now.hour, now.minute) >= EDITION_CUTOFF:
        date += datetime.timedelta(days=1)
    return date.isoformat(), "evening"


def raw_key(date: str, edition: str = "evening") -> str:
    return f"news.{date}.{edition}.raw.lukijoilta"


# ── the interview kickoff (deterministic — the questions are fixed) ──────────
def build_interview(date_str: str) -> str:
    return (
        f"**🗞️ (L)AIMEAT Sanomat — päivän haastattelu ({date_str})**\n\n"
        "Toimitus kaipaa materiaalia Lukijoilta-osastoon. Kerro omin sanoin tähän ketjuun:\n\n"
        "1. Mitä tänään tapahtui? Mikä oli päivän tärkein juttu?\n"
        "2. Kävikö jotain merkittävää — työ, projektit, koti, maailma?\n"
        "3. Liitä kuvia mukaan, jos on — ne päätyvät juttuun.\n\n"
        "Jokainen vastauksesi tässä ketjussa kirjataan uutismateriaaliksi. Vilma Vinkki kirjoittaa "
        "niistä jutun iltapainokseen. 👇"
    )


def send_interview_kickoff(agent: str, date_str: str) -> bool:
    """DM the owner the daily interview — reply in the standing interview thread if we have one, else
    open it (and remember the conversation id). Returns True if delivered."""
    to = owner_gaii(agent)
    if not to:
        print(f"[{agent}] send_interview_kickoff: could not resolve owner gaii", file=sys.stderr)
        return False
    body = build_interview(date_str)
    conv = get_config(agent).get("interview_conversation_id")
    if conv:
        res = dm.dm_reply(agent, to, body, conversation_id=conv)
    else:
        res = dm.dm_send(agent, to, body, subject="Sanomat — päivän haastattelu")
    conv_id = orchestrator._conv_id(res) if res else None
    if conv_id and conv_id != conv:
        set_config(agent, interview_conversation_id=conv_id)
    return bool(res)


# ── tip images: private DM attachment -> PUBLIC storage the paper can embed ──
def publish_tip_images(agent: str, attachments: list[dict], *, date: str) -> list[str]:
    """Re-publish image attachments PUBLIC under this agent's storage and return their /v1/pub URLs.
    Non-image attachments are skipped (the paper embeds images only). A failed fetch/upload is
    reported loud and skipped — the tip still lands, without that image."""
    from crewaimeat.seedream_gen import _upload_public

    urls: list[str] = []
    gaii = own_gaii(agent)
    _tok, node_url = _token(agent, _discover_owner(agent))
    if not gaii or not node_url:
        print(f"[{agent}] publish_tip_images: no gaii/node url — images skipped", file=sys.stderr)
        return urls
    for i, att in enumerate(attachments):
        mime = str(att.get("mime") or att.get("mime_type") or "").lower()
        skey = att.get("storage_key") or att.get("storageKey")
        if not skey or not mime.startswith("image/"):
            continue
        got = storage.fetch_bytes(agent, str(skey))
        if not got:
            print(f"[{agent}] publish_tip_images: fetch failed for {skey}", file=sys.stderr)
            continue
        data, real_mime = got
        name = re.sub(r"[^a-zA-Z0-9._-]", "_", str(att.get("name") or f"kuva-{i}.jpg"))
        pub_key = f"sanomat/lukijoilta/{date}/{name}"
        if not _upload_public(agent, pub_key, data, real_mime or mime):
            print(f"[{agent}] publish_tip_images: public upload failed for {pub_key}", file=sys.stderr)
            continue
        urls.append(f"{node_url.rstrip('/')}/v1/pub/{gaii}/{pub_key}")
    return urls


# ── the tip write: append to the edition's lukijoilta raw ────────────────────
def add_tip(
    agent: str,
    *,
    text: str,
    source: str,
    images: list[str] | None = None,
    title: str | None = None,
) -> tuple[str, str]:
    """Append one tip to the NEXT evening edition's lukijoilta raw (read-modify-write; tips are rare
    enough that the race window is acceptable). Returns (date, edition) it landed in. Raises on a
    failed write — the caller must tell the sender the tip did NOT land (fail loud, no black hole)."""
    date, edition = next_evening_edition()
    key = raw_key(date, edition)
    existing = _aimeat_call(agent, "aimeat_memory_read", {"key": key})
    items = []
    if isinstance(existing, dict) and isinstance(existing.get("value"), list):
        items = existing["value"]
    entry: dict = {
        "title": (title or text.strip().split("\n", 1)[0])[:120],
        "content": text.strip(),
        "source": source,
    }
    if images:
        entry["images"] = images
    items.append(entry)
    res = _aimeat_call(agent, "aimeat_memory_write", {"key": key, "value": items, "visibility": "owner"})
    if res is None:
        raise RuntimeError(f"tip write failed — {key} not updated (tunnel/transport)")
    return date, edition

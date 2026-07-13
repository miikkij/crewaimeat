"""Oikaisukanava — the formal correction-request channel for (L)AIMEAT Sanomat.

The loop (arbiter = the desk's Lakiosasto, publishing = the existing edition keys):
  1. Anyone (owner or federation user) DMs sanomat-desk a request starting with "oikaisu"/"korjaus".
  2. `new_request` files it into the PUBLIC index `sanomat.oikaisut.index` (status: vastaanotettu) —
     the app's Oikaisut page reads this one key, so the requester can follow the state.
  3. The arbiter (one strict-JSON LLM pass over the request + recent headlines) rules:
     - "aiheeton"  -> final immediately; the pompous justification is published in the index.
     - "oikaistaan"-> status odottaa-hyvaksyntaa + a HITL approval gate to the OWNER (a correction
       changes public content, so a human approves before publication).
  4. On approval, `publish_correction` appends the correction to the NEXT evening edition's
     `news.<date>.evening.article.oikaisut` (public; the front-page index auto-includes any
     article.* key) and flips the index entry to oikaistu. On rejection -> hylatty.

Statuses: vastaanotettu -> aiheeton | odottaa-hyvaksyntaa -> oikaistu | hylatty. All transitions go
through set_status (read-modify-write on the one index key). Deterministic; fail loud everywhere.
"""

from __future__ import annotations

import datetime
import json
import re
from zoneinfo import ZoneInfo

from crewaimeat.aimeat_crew import _aimeat_call
from crewaimeat.llm import get_llm
from crewaimeat.reader_desk import next_evening_edition

INDEX_KEY = "sanomat.oikaisut.index"
_TZ = ZoneInfo("Europe/Helsinki")

STATUSES = ("vastaanotettu", "aiheeton", "odottaa-hyvaksyntaa", "oikaistu", "hylatty")


class CorrectionsUnavailable(RuntimeError):
    """The corrections flow could not complete (transport or arbiter failure). Callers tell the
    requester the request did NOT get filed/ruled — never a silent black hole."""


def _today() -> str:
    return datetime.datetime.now(_TZ).date().isoformat()


# ── the public index (the app's Oikaisut page reads exactly this key) ────────
def read_index(agent: str) -> list[dict]:
    r = _aimeat_call(agent, "aimeat_memory_read", {"key": INDEX_KEY})
    val = r.get("value") if isinstance(r, dict) else None
    items = val.get("items") if isinstance(val, dict) else None
    return items if isinstance(items, list) else []


def _write_index(agent: str, items: list[dict]) -> None:
    value = {"items": items, "updated": _today()}
    res = _aimeat_call(agent, "aimeat_memory_write", {"key": INDEX_KEY, "value": value, "visibility": "public"})
    if res is None:
        raise CorrectionsUnavailable(f"index write failed — {INDEX_KEY} not updated (tunnel/transport)")


def new_request(agent: str, *, sender: str, text: str) -> dict:
    """File a correction request (status: vastaanotettu) into the public index. Returns the entry."""
    items = read_index(agent)
    entry = {
        "id": f"oik-{_today()}-{len(items) + 1}",
        "created": _today(),
        "sender": sender.split("@")[0],  # requester handle only — no node routing in a public key
        "claim": text.strip()[:1000],
        "status": "vastaanotettu",
    }
    _write_index(agent, [entry, *items])
    return entry


def set_status(agent: str, req_id: str, **fields) -> dict:
    """Flip one entry's status (+ extra fields: perustelu, oikaisu, article_key, edition, resolved).
    Raises if the entry is missing or the status is unknown — a typo must not invent a state."""
    status = fields.get("status")
    if status and status not in STATUSES:
        raise ValueError(f"unknown correction status {status!r} (valid: {STATUSES})")
    items = read_index(agent)
    for it in items:
        if it.get("id") == req_id:
            it.update({k: v for k, v in fields.items() if v is not None})
            _write_index(agent, items)
            return it
    raise CorrectionsUnavailable(f"correction {req_id} not found in {INDEX_KEY}")


# ── arbiter context: what did the paper actually print lately ────────────────
def recent_headlines(agent: str, *, days: int = 3) -> list[tuple[str, str]]:
    """[(article_key, first line)] for the last `days` evening editions — enough for the arbiter to
    identify which article a claim is about. Transport failure on a day is skipped (the arbiter can
    still rule on the request text alone)."""
    out: list[tuple[str, str]] = []
    day = datetime.datetime.now(_TZ).date()
    for _ in range(days):
        prefix = f"news.{day.isoformat()}.evening.article."
        lr = _aimeat_call(agent, "aimeat_memory_list", {"owner_scope": True, "prefix": prefix})
        for it in (lr.get("items") or []) if isinstance(lr, dict) else []:
            val = str(it.get("value") or "")
            if val:
                out.append((str(it.get("key")), val.strip().split("\n", 1)[0][:120]))
        day -= datetime.timedelta(days=1)
    return out


# ── the arbiter ruling (strict JSON, fail loud) ──────────────────────────────
def judge_request(agent: str, entry: dict, headlines: list[tuple[str, str]]) -> dict:
    """One ruling: {"verdict": "aiheeton"|"oikaistaan", "perustelu": str, "oikaisu": str|None,
    "article_key": str|None}. Unparseable output RAISES — a broken arbiter never rules by accident."""
    llm = get_llm(for_tool_use=False, temperature=0.2, agent_name=agent)
    heads = "\n".join(f"- {k}: {h}" for k, h in headlines[:60]) or "(ei tuoreita otsikoita saatavilla)"
    prompt = (
        "Olet satiirisen verkkolehden ((L)AIMEAT Sanomat) lakiosaston oikaisuarbiter. Lehti on avoimesti "
        "satiirinen: satiiri, liioittelu ja pilailu EIVÄT ole oikaisun aihe. Oikaisu tehdään vain, jos "
        "juttu esittää TOSIASIAVÄITTEEN, joka on todistettavasti väärä JA jonka lukija voi perustellusti "
        "ymmärtää vakavaksi (esim. väärä nimi, väärä päivämäärä, väärin siteerattu lähde, oikea henkilö "
        "väärässä valossa).\n\n"
        f"OIKAISUPYYNTÖ ({entry.get('sender')}, {entry.get('created')}):\n-----\n{entry.get('claim')}\n-----\n\n"
        f"LEHDEN TUOREET JUTUT (avain: otsikko):\n{heads}\n\n"
        "Vastaa PELKÄLLÄ JSON-objektilla:\n"
        '{"verdict": "aiheeton" TAI "oikaistaan", '
        '"perustelu": "lyhyt, juhlallisen virallinen perustelu suomeksi (2-3 virkettä, lakiosaston ääni)", '
        '"oikaisu": "oikaistaan-tapauksessa valmis oikaisuteksti suomeksi (mitä väitettiin, mikä on oikein), muuten null", '
        '"article_key": "juttua vastaava avain yllä olevasta listasta tai null"}'
    )
    try:
        raw = llm.call([{"role": "user", "content": prompt}])
    except Exception as exc:  # noqa: BLE001
        raise CorrectionsUnavailable(f"arbiter LLM call failed: {exc!r}") from exc
    raw = raw if isinstance(raw, str) else str(raw)
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        raise CorrectionsUnavailable(f"arbiter ruling unparseable: {raw[:200]!r}")
    try:
        data = json.loads(m.group(0))
    except Exception as exc:  # noqa: BLE001
        raise CorrectionsUnavailable(f"arbiter ruling bad JSON: {raw[:200]!r}") from exc
    verdict = str(data.get("verdict") or "").strip().lower()
    if verdict not in ("aiheeton", "oikaistaan"):
        raise CorrectionsUnavailable(f"arbiter verdict invalid: {verdict!r}")
    return {
        "verdict": verdict,
        "perustelu": str(data.get("perustelu") or "").strip(),
        "oikaisu": (str(data["oikaisu"]).strip() if data.get("oikaisu") else None),
        "article_key": (str(data["article_key"]).strip() if data.get("article_key") else None),
    }


# ── publication: the correction becomes edition content ─────────────────────
def publish_correction(agent: str, entry: dict, correction_text: str) -> tuple[str, str]:
    """Append the approved correction to the NEXT evening edition's oikaisut article (public; the
    front-page index auto-includes any article.* key) and flip the index entry to oikaistu.
    Returns (date, edition) it will appear in. Fail loud on any write failure."""
    date, edition = next_evening_edition()
    key = f"news.{date}.{edition}.article.oikaisut"
    existing = _aimeat_call(agent, "aimeat_memory_read", {"key": key})
    body = str(existing.get("value") or "") if isinstance(existing, dict) else ""
    if not body.strip():
        body = "# Oikaisut\n\nLakiosaston vahvistamat oikaisut aiempiin julkaisuihin.\n"
    body += (
        f"\n---\n\n## Oikaisu ({entry['id']})\n\n{correction_text.strip()}\n\n"
        f"*Oikaisupyyntö vastaanotettu {entry.get('created')}. — Lakiosasto*\n"
    )
    res = _aimeat_call(agent, "aimeat_memory_write", {"key": key, "value": body, "visibility": "public"})
    if res is None:
        raise CorrectionsUnavailable(f"correction publish failed — {key} not written (tunnel/transport)")
    set_status(
        agent,
        entry["id"],
        status="oikaistu",
        oikaisu=correction_text.strip()[:2000],
        edition=f"{date} {edition}",
        resolved=_today(),
    )
    return date, edition

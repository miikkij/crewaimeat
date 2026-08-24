"""JULKAISUPÖYTÄ — the desk that decides and the desk that learns.

Two agents live here, and between them they are the difference between a text generator and a desk:

  * **julkaisu-toimittaja** (the editor) reads the public changelog ITSELF, remembers what has
    already been told and how it did, picks the one entry where a reader's own work changes, digs
    out what it REPLACED, and writes ``julkaisu.{ref}.aineisto``. The writers no longer receive a
    pre-written summary — they receive an angle, a before, an after, and a proof, and they only
    choose words.
  * **julkaisu-mittari** (the measurer) runs after publishing, on its own schedule, reads what each
    published piece actually did, and folds that back into ``julkaisu.kerrottu`` — the same record
    the editor reads at step 2. That is the loop: the third run is better than the first, and the
    reason is readable.

Everything except judgement is code. The changelog is fetched with `requests` (verified public:
`GET https://aimeat.io/changelog.json` → 200, `{_format, entries[]}` with `{date, kind, title{en,fi},
body{en,fi}}`), the already-told set is a memory read, the chosen entry's title is copied VERBATIM
from the feed rather than retyped by a model, and the aineisto is checked field by field before it
is stored. What the model decides is which entry matters and what the story actually is.

Nothing here posts anything or contacts anyone.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import sys
from typing import Any

import requests
from aimeat_crewai.provenance import HumanInvolvement, Level, Method, declare, source

from crewaimeat.aimeat_crew import _aimeat_call, _aimeat_rest, record_deliverable_key
from crewaimeat.llm import get_llm, resolved_model, resolved_provider
from crewaimeat.memory_tools import read_owner_key
from crewaimeat.prose_style import FINNISH_NATIVE_STYLE

CHANGELOG_URL = "https://aimeat.io/changelog.json"
LLMS_URL = "https://aimeat.io/llms.txt"
KERROTTU_KEY = "julkaisu.kerrottu"
AINEISTO_KEY = "julkaisu.{ref}.aineisto"
MITTAUS_KEY = "julkaisu.{ref}.mittaus"
PORTTI_KEY = "julkaisu.{ref}.portti"

_MAX_ATTEMPTS = 3
_NEIGHBOURS = 4  # entries either side of the chosen one — "what led here"
_MEASURE_AFTER_H = 24  # a piece is measured once it has had a day to do something


# ── the public feed ──────────────────────────────────────────────────────────────────────────────
def fetch_changelog(url: str = CHANGELOG_URL) -> list[dict]:
    """Every changelog entry, newest first. Raises when the feed cannot be read.

    A server-side fetch, which is the whole point: the browser is blocked cross-origin, the agent is
    not. It RAISES rather than returning [] — an agent that cannot read the changelog must stop, not
    write about a changelog it never saw.
    """
    r = requests.get(url, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"{url} answered HTTP {r.status_code} — the changelog was not read")
    r.encoding = "utf-8"
    doc = r.json()
    entries = doc.get("entries") if isinstance(doc, dict) else doc
    if not isinstance(entries, list) or not entries:
        raise RuntimeError(f"{url} carried no entries — the changelog was not read")
    return sorted(entries, key=lambda e: str((e or {}).get("date") or ""), reverse=True)


def fetch_llms_txt(url: str = LLMS_URL) -> str:
    """The node's own operating text, where a shipped feature actually shows. Best-effort: this is
    corroboration for the dig, not the story, so an unreachable one degrades the dig rather than
    stopping the run — and the agent is told it is missing so it can say so in `varmuus`."""
    try:
        r = requests.get(url, timeout=45)
        if r.status_code == 200:
            r.encoding = "utf-8"
            return r.text
    except Exception as exc:  # noqa: BLE001
        print(f"[julkaisu-desk] {url} unavailable: {exc!r}", file=sys.stderr)
    return ""


def entry_text(entry: dict, field: str, lang: str = "en") -> str:
    """A localized changelog field ({en, fi} or a bare string) as text."""
    v = (entry or {}).get(field)
    if isinstance(v, dict):
        return str(v.get(lang) or v.get("en") or v.get("fi") or "").strip()
    return str(v or "").strip()


def entry_title(entry: dict) -> str:
    """The title used as this entry's identity — English, the stable side of the feed."""
    return entry_text(entry, "title", "en")


def entry_ref(entry: dict) -> str:
    """A stable short ref minted from the entry itself, for a run that was dispatched without one.

    Same entry -> same ref, always: the id is a hash of (date, title), so a re-run of the same story
    writes the same keys instead of scattering half-finished runs across the namespace.
    """
    seed = f"{entry.get('date')}|{entry_title(entry)}".encode()
    return "p" + hashlib.sha256(seed).hexdigest()[:7]


# ── the running memory: what has been told, and how it did ───────────────────────────────────────
def read_kerrottu(agent_name: str) -> list[dict]:
    """`julkaisu.kerrottu` as a list of records (empty when the desk has never run)."""
    value = read_owner_key(agent_name, KERROTTU_KEY)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if isinstance(value, dict):
        value = value.get("kerrottu")
    return [r for r in (value or []) if isinstance(r, dict)]


def _write_kerrottu(agent_name: str, records: list[dict]) -> bool:
    r = _aimeat_call(
        agent_name,
        "aimeat_memory_write",
        {"key": KERROTTU_KEY, "value": {"kerrottu": records[-200:]}, "visibility": "owner", "tags": ["julkaisupoyta"]},
    )
    if r is None:
        print(
            f"[{agent_name}] WRITE FAILED {KERROTTU_KEY} — the next run will not know about this one", file=sys.stderr
        )
        return False
    return True


def remember_told(agent_name: str, ref: str, entry: dict) -> bool:
    """Append the chosen entry to `julkaisu.kerrottu` so the next run does not repeat it.

    Read-modify-write on one owner key. Re-running the SAME ref updates its record in place rather
    than appending a second one, so a repeated run cannot make the ledger disagree with itself.
    """
    records = read_kerrottu(agent_name)
    row = {
        "ref": ref,
        "aihe": entry_title(entry),
        "paiva": str(entry.get("date") or ""),
        "lahde": f"{CHANGELOG_URL}#{entry.get('date')}",
        "valittu_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    kept = [r for r in records if r.get("ref") != ref]
    if len(kept) != len(records):  # a re-run of the same ref keeps whatever was already learned
        prior = next(r for r in records if r.get("ref") == ref)
        row = {**prior, **row}
    return _write_kerrottu(agent_name, [*kept, row])


def remember_measured(agent_name: str, ref: str, patch: dict) -> bool:
    """Fold a measurement into this ref's record in `julkaisu.kerrottu` (the editor reads it next run)."""
    records = read_kerrottu(agent_name)
    hit = next((r for r in records if r.get("ref") == ref), None)
    if hit is None:
        records.append({"ref": ref, **patch})
    else:
        hit.update(patch)
    return _write_kerrottu(agent_name, records)


def untold_entries(entries: list[dict], told: list[dict]) -> list[dict]:
    """The entries this desk has not published yet — matched on (date, title), the pair the ledger
    stores. Titles are compared case-folded and whitespace-collapsed so a cosmetic feed edit does not
    resurrect a story that was already told."""

    def _norm(s: str) -> str:
        return re.sub(r"\s+", " ", (s or "")).strip().casefold()

    seen = {(_norm(str(r.get("paiva"))), _norm(str(r.get("aihe")))) for r in told}
    return [e for e in entries if (_norm(str(e.get("date"))), _norm(entry_title(e))) not in seen]


# ── the editor ───────────────────────────────────────────────────────────────────────────────────
_TAG = "<{t}>(.*?)</{t}>"
_AINEISTO_TAGS = ("KULMA", "ENNEN", "NYT", "KENELLE", "TODISTE", "EI_KERROTA", "VARMUUS")
_GENERIC_AUDIENCE = ("käyttäjät", "kayttajat", "users", "kaikki", "everyone", "ihmiset", "asiakkaat")


def _tagged(raw: str, tag: str) -> str:
    m = re.search(_TAG.format(t=tag), raw or "", re.S | re.I)
    return m.group(1).strip() if m else ""


def parse_aineisto(raw: str) -> dict:
    """The dig's tagged blocks as a dict. A missing block comes back empty so the check catches it —
    the model is never allowed to half-answer into storage."""
    out = {t.lower(): _tagged(raw, t) for t in _AINEISTO_TAGS}
    out["ei_kerrota"] = [
        re.sub(r"^[-*•\d.)\s]+", "", ln).strip() for ln in (out.get("ei_kerrota") or "").splitlines() if ln.strip()
    ]
    return out


def check_aineisto(a: dict) -> list[str]:
    """The editor's own contract, checked in code. Violations in Finnish (the prompt is Finnish)."""
    bad: list[str] = []
    for field, label in (
        ("kulma", "kulma"),
        ("ennen", "ennen"),
        ("nyt", "nyt"),
        ("kenelle", "kenelle"),
        ("todiste", "todiste"),
        ("varmuus", "varmuus"),
    ):
        if not str(a.get(field) or "").strip():
            bad.append(f"kenttä '{label}' on tyhjä — se on osa aineistoa, ei valinnainen.")
    kulma = str(a.get("kulma") or "").strip()
    if kulma and len(kulma) > 220:
        bad.append(f"kulma on {len(kulma)} merkkiä — se on YKSI lause jonka lukija toistaisi, ei tiivistelmä.")
    if kulma and kulma.count(".") > 2:
        bad.append("kulma on useampi lause — kirjoita se yhtenä.")
    kenelle = str(a.get("kenelle") or "").strip().casefold()
    if kenelle and (kenelle in _GENERIC_AUDIENCE or all(w in _GENERIC_AUDIENCE for w in kenelle.split())):
        bad.append(f"kenelle on '{kenelle}' — nimeä kuka tarkalleen, ei 'käyttäjät'.")
    if not a.get("ei_kerrota"):
        bad.append("ei_kerrota on tyhjä — nimeä ainakin yksi asia merkinnässä joka EI ole tämä tarina.")
    return bad


def _candidate_block(entries: list[dict], limit: int = 14) -> str:
    rows = []
    for i, e in enumerate(entries[:limit]):
        body = entry_text(e, "body", "en")
        rows.append(f"[{i}] {e.get('date')} ({e.get('kind')}) {entry_title(e)}\n    {body[:320]}")
    return "\n".join(rows)


def _told_block(told: list[dict], limit: int = 12) -> str:
    if not told:
        return "(mitään ei ole vielä kerrottu — tämä on ensimmäinen ajo)"
    rows = []
    for r in told[-limit:]:
        m = r.get("mittaus") or {}
        how = ", ".join(
            f"{ch}: {v.get('nayttokerrat', '?')} näyttöä / {v.get('klikit', '?')} klikkiä"
            for ch, v in m.items()
            if isinstance(v, dict)
        )
        learned = str(r.get("opittu") or "").strip()
        rows.append(
            f"- {r.get('paiva')} {r.get('aihe')}"
            + (f" — {how}" if how else "")
            + (f" — opittu: {learned}" if learned else "")
        )
    return "\n".join(rows)


def _pick_entry(llm, candidates: list[dict], told: list[dict]) -> tuple[int, str]:
    """Which entry is worth telling. Returns (index into candidates, reason). Judgement, not code."""
    prompt = (
        "Olet AIMEATin julkaisupöydän toimittaja. Valitse TÄSTÄ listasta YKSI merkintä, josta seuraava "
        "julkaisu tehdään.\n\n"
        "VALINTAPERUSTE — tässä järjestyksessä:\n"
        "1. Se merkintä, jonka jälkeen lukijan OMA työ muuttuu. Ei uusin oletuksena.\n"
        "2. Tasatilanteessa: se jonka joku kertoisi eteenpäin kollegalleen.\n\n"
        "JO KERROTTU (älä valitse näitä uudestaan; katso myös miten ne pärjäsivät):\n"
        + _told_block(told)
        + "\n\nEHDOKKAAT:\n"
        + _candidate_block(candidates)
        + "\n\nVastaa täsmälleen näin:\n<VALINTA>numero</VALINTA>\n<PERUSTELU>yksi lause</PERUSTELU>"
    )
    raw = llm.call([{"role": "user", "content": prompt}])
    raw = raw if isinstance(raw, str) else str(raw)
    m = re.search(r"<VALINTA>\s*(\d+)\s*</VALINTA>", raw, re.I)
    idx = int(m.group(1)) if m else 0
    if not 0 <= idx < len(candidates):
        idx = 0
    return idx, _tagged(raw, "PERUSTELU") or "(ei perustelua)"


def _dig_prompt(entry: dict, neighbours: list[dict], llms_txt: str) -> str:
    neighbour_rows = "\n".join(
        f"- {e.get('date')} {entry_title(e)}: {entry_text(e, 'body', 'en')[:220]}" for e in neighbours
    )
    node = llms_txt[:4000] if llms_txt else "(solmun llms.txt ei ollut luettavissa)"
    return (
        "Olet AIMEATin julkaisupöydän toimittaja. Merkintä on VALITTU. Nyt kaivat esiin sen, mitä "
        "merkintä ei sano: mitä tämä KORVASI, kuka oli jumissa ennen, ja mikä on nyt mahdollista.\n\n"
        "VALITTU MERKINTÄ\n"
        f"päivä: {entry.get('date')}  laji: {entry.get('kind')}\n"
        f"otsikko: {entry_title(entry)}\n"
        f"teksti (en):\n{entry_text(entry, 'body', 'en')}\n\n"
        f"teksti (fi):\n{entry_text(entry, 'body', 'fi')}\n\n"
        "NAAPURIMERKINNÄT (mikä johti tähän)\n" + (neighbour_rows or "(ei naapureita)") + "\n\n"
        "SOLMUN OMA TEKSTI (missä ominaisuus näkyy)\n" + node + "\n\n"
        "SÄÄNNÖT\n"
        "- Ennen-tila on tärkein. Jos ET löydä sitä lähteistä, sano se kentässä VARMUUS — älä keksi sitä.\n"
        "- TODISTE on fakta joka tekee asiasta todellisen: luku, nimi, tai työvaihe joka katosi. Se on "
        "oltava lähteissä.\n"
        "- KENELLE nimeää kuka tarkalleen. 'Käyttäjät' ei kelpaa.\n"
        "- KULMA on YKSI lause, se jonka lukija toistaisi. Ei tiivistelmä merkinnästä.\n"
        "- EI_KERROTA listaa ne merkinnän asiat jotka EIVÄT ole tämä tarina (kirjoittajat jättävät ne pois).\n"
        + FINNISH_NATIVE_STYLE
        + "\n\nVASTAUKSEN MUOTO — täsmälleen nämä lohkot, ei mitään muuta:\n"
        "<KULMA>yksi lause</KULMA>\n"
        "<ENNEN>mitä ihmiset tekivät ennen tätä, konkreettisesti</ENNEN>\n"
        "<NYT>mitä he tekevät sen sijaan</NYT>\n"
        "<KENELLE>kuka tarkalleen</KENELLE>\n"
        "<TODISTE>luku, nimi tai kadonnut työvaihe</TODISTE>\n"
        "<EI_KERROTA>\n- asia joka ei ole tämä tarina\n- toinen\n</EI_KERROTA>\n"
        "<VARMUUS>mitä et pystynyt varmistamaan (jos kaikki varmistui, sano sekin)</VARMUUS>"
    )


def valitse_aihe(agent_name: str, ref: str | None = None, task_id: str | None = None) -> str:
    """Fetch the changelog, pick the entry worth telling, dig, and write julkaisu.<ref>.aineisto.

    Returns a report. Every failure writes NOTHING and says why: an unread changelog, nothing left
    untold, or a dig that will not meet the contract. The workflow's success_signal reads the same
    absence, so a silent bad pick cannot look like a good run.
    """
    try:
        entries = fetch_changelog()
    except Exception as exc:  # noqa: BLE001 — the fetch IS the input; a failure is the whole story
        print(f"[{agent_name}] changelog fetch failed: {exc!r}", file=sys.stderr)
        return f"FAILED: {exc}. Nothing was written — this agent does not write about a changelog it did not read."

    told = read_kerrottu(agent_name)
    candidates = untold_entries(entries, told)
    if not candidates:
        return (
            f"FAILED: all {len(entries)} changelog entries are already in {KERROTTU_KEY} — there is "
            "nothing left to tell. Nothing was written."
        )

    llm = get_llm(for_tool_use=False, temperature=0.4, agent_name=agent_name)
    idx, why = _pick_entry(llm, candidates, told)
    entry = candidates[idx]
    ref = (ref or "").strip() or entry_ref(entry)
    key = AINEISTO_KEY.format(ref=ref)
    pos = entries.index(entry)
    neighbours = [e for e in entries[max(0, pos - _NEIGHBOURS) : pos + _NEIGHBOURS + 1] if e is not entry]
    llms_txt = fetch_llms_txt()
    print(f"[{agent_name}] picked '{entry_title(entry)}' ({entry.get('date')}) -> {key}: {why}", file=sys.stderr)

    base = _dig_prompt(entry, neighbours, llms_txt)
    prompt, aineisto, violations = base, {}, ["(no attempt ran)"]
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        raw = llm.call([{"role": "user", "content": prompt}])
        aineisto = parse_aineisto(raw if isinstance(raw, str) else str(raw))
        violations = check_aineisto(aineisto)
        print(
            f"[{agent_name}] dig attempt {attempt}/{_MAX_ATTEMPTS}: "
            + ("OK" if not violations else "; ".join(violations)),
            file=sys.stderr,
        )
        if not violations:
            break
        prompt = (
            base + "\n\nKorjaa nämä ja kirjoita koko vastaus uudestaan:\n" + "\n".join(f"- {v}" for v in violations)
        )
    if violations:
        return (
            f"FAILED: the dig did not meet the contract after {_MAX_ATTEMPTS} attempts — "
            + "; ".join(violations)
            + f". Nothing was written to {key}."
        )

    # `valittu` and `paiva` are copied from the FEED, never retyped by the model: the contract says
    # verbatim, and a model that retypes a title eventually retypes it wrong.
    value = {
        "valittu": entry_title(entry),
        "paiva": str(entry.get("date") or ""),
        "kulma": aineisto["kulma"],
        "ennen": aineisto["ennen"],
        "nyt": aineisto["nyt"],
        "kenelle": aineisto["kenelle"],
        "todiste": aineisto["todiste"],
        "ei_kerrota": aineisto["ei_kerrota"],
        "varmuus": aineisto["varmuus"],
        "lahde": f"{CHANGELOG_URL}#{entry.get('date')}",
    }
    written = _aimeat_call(
        agent_name,
        "aimeat_memory_write",
        {
            "key": key,
            "value": value,
            "visibility": "owner",
            "tags": ["julkaisupoyta", "aineisto", f"ref:{ref}"],
            "ai_provenance": declare(
                Level.SYNTHESIZED,
                method=Method.SYNTHESIZED,
                human_involvement=HumanInvolvement.NONE,
                model=resolved_model(llm),
                provider=resolved_provider(),
                sources=[source(CHANGELOG_URL), source(LLMS_URL)] if llms_txt else [source(CHANGELOG_URL)],
                notes="julkaisupöytä: entry chosen and dug from the public changelog; nothing published.",
            ),
        },
    )
    if written is None:
        return f"FAILED to write '{key}' (tunnel/transport) — the aineisto did not land."
    record_deliverable_key(task_id, key)
    remembered = remember_told(agent_name, ref, entry)
    return (
        f"OK: '{entry_title(entry)}' ({entry.get('date')}) -> {key}. Angle: {value['kulma'][:120]}"
        + (f" | ref '{ref}' was minted from the entry (no ref in the dispatch)." if not (task_id and ref) else "")
        + ("" if remembered else f" WARNING: {KERROTTU_KEY} was NOT updated — the next run may repeat this entry.")
    )


# ── the measurer ─────────────────────────────────────────────────────────────────────────────────
def _iso_now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _hours_since(stamp: str) -> float | None:
    try:
        t = datetime.datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=datetime.UTC)
    return (datetime.datetime.now(datetime.UTC) - t).total_seconds() / 3600.0


def published_refs(agent_name: str) -> list[dict]:
    """Every ref whose gate says it was published, with when the gate was answered.

    The gate record `julkaisu.<ref>.portti` is the human's decision, so it — not a guess about what
    "published" means — is what the measurer keys on.
    """
    r = _aimeat_call(agent_name, "aimeat_memory_list", {"owner_scope": True, "prefix": "julkaisu.", "limit": 500}) or {}
    out = []
    for it in r.get("items") or []:
        m = re.match(r"^julkaisu\.(.+)\.portti$", str((it or {}).get("key") or ""))
        if not m:
            continue
        value = it.get("value")
        if value is None:
            value = read_owner_key(agent_name, it["key"])
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                value = {"paatos": value}
        if not isinstance(value, dict):
            continue
        decision = str(value.get("paatos") or value.get("decision") or "").strip().casefold()
        if decision not in ("hyvaksy", "hyväksy", "approve", "approved", "julkaistu", "published"):
            continue
        out.append(
            {
                "ref": m.group(1),
                "at": str(
                    value.get("at") or value.get("paatettu") or it.get("updated_at") or it.get("updatedAt") or ""
                ),
                "julkaistu": [c for c in (value.get("julkaistu") or value.get("kanavat") or []) if isinstance(c, str)],
            }
        )
    return out


def fetch_attempts(agent_name: str) -> list[dict]:
    """`GET /v1/connections/attempts` — what the node recorded about each publishing attempt.

    Verified reachable with an AGENT token (200, `{attempts: []}` on 2026-08-24), so this is not an
    owner-only route. The record SHAPE is unverified because the list was empty; `attempt_metrics`
    therefore reports what it actually saw instead of assuming field names.
    """
    data = _aimeat_rest(agent_name, "GET", "/v1/connections/attempts") or {}
    attempts = data.get("attempts") if isinstance(data, dict) else data
    return [a for a in (attempts or []) if isinstance(a, dict)]


def attempt_metrics(agent_name: str, attempt_id: str) -> dict | None:
    """`GET /v1/connections/attempts/:id/metrics` for one attempt, or None (logged loud)."""
    data = _aimeat_rest(agent_name, "GET", f"/v1/connections/attempts/{attempt_id}/metrics")
    if data is None:
        print(f"[{agent_name}] metrics for attempt {attempt_id} could not be read", file=sys.stderr)
        return None
    return data.get("metrics") if isinstance(data, dict) and "metrics" in data else data


def _attempt_ref(attempt: dict) -> str | None:
    """The ref an attempt belongs to, found by looking for it — not by assuming a field name.

    The attempts list was empty when this was written, so its shape is unverified. Rather than pick a
    field and be quietly wrong, this searches the record for a `julkaisu.<ref>.*` key or a `ref`
    field; an attempt it cannot place is REPORTED, never guessed onto the nearest run.
    """
    blob = json.dumps(attempt, ensure_ascii=False, default=str)
    m = re.search(r"julkaisu\.([a-z0-9][a-z0-9._-]*?)\.(?:linkedin|x|video|kuvat|aineisto|portti)\b", blob, re.I)
    if m:
        return m.group(1)
    for field in ("ref", "julkaisu_ref", "reference"):
        v = attempt.get(field)
        if isinstance(v, str) and re.match(r"^[a-z0-9][a-z0-9._-]{0,63}$", v):
            return v
    return None


def _channel_of(attempt: dict) -> str:
    for field in ("channel", "kanava", "platform", "target", "type"):
        v = attempt.get(field)
        if isinstance(v, str) and v.strip():
            return v.strip().casefold()
    return "tuntematon"


def _numbers(metrics: Any) -> dict:
    """The two numbers the ledger records, pulled out of whatever the metrics record calls them."""
    m = metrics if isinstance(metrics, dict) else {}

    def _pick(*names):
        for n in names:
            v = m.get(n)
            if isinstance(v, (int, float)):
                return int(v)
        return 0

    return {
        "nayttokerrat": _pick("nayttokerrat", "impressions", "views", "reach", "seen"),
        "klikit": _pick("klikit", "clicks", "link_clicks", "engagements"),
    }


def _learned(llm, ref: str, aihe: str, per_channel: dict, told: list[dict]) -> str:
    prompt = (
        "Olet julkaisupöydän mittari. Kirjoita YKSI lause siitä, mitä tämä mittaus kertoo siitä mikä toimii.\n\n"
        f"AIHE: {aihe}\n"
        f"MITTAUS: {json.dumps(per_channel, ensure_ascii=False)}\n\n"
        "AIEMMAT AJOT:\n" + _told_block(told) + "\n\n"
        "Sääntö: puhu siitä mitä luvut näyttävät, älä lupaa mitään. Jos lukuja on liian vähän "
        "johtopäätökseen, sano se. Yksi lause, ei otsikkoa." + FINNISH_NATIVE_STYLE
    )
    out = llm.call([{"role": "user", "content": prompt}])
    return (out if isinstance(out, str) else str(out)).strip().splitlines()[0][:400]


def mittaa_julkaisut(agent_name: str, task_id: str | None = None) -> str:
    """Measure every piece published more than 24 h ago and not yet measured; fold it into kerrottu.

    A run with nothing to measure is a fine run and says so. What it never does is invent a number:
    an attempt whose record it cannot place, or a metrics route that does not answer, is REPORTED.
    """
    gates = published_refs(agent_name)
    told = read_kerrottu(agent_name)
    measured = {r.get("ref") for r in told if r.get("mittaus")}
    due = [
        g
        for g in gates
        if g["ref"] not in measured and ((_hours_since(g["at"]) or 0) >= _MEASURE_AFTER_H or not g["at"])
    ]
    if not due:
        return f"OK: nothing due — {len(gates)} published run(s), {len(measured)} already measured, none older than {_MEASURE_AFTER_H} h awaiting a number."

    attempts = fetch_attempts(agent_name)
    by_ref: dict[str, list[dict]] = {}
    unplaceable = 0
    for a in attempts:
        ref = _attempt_ref(a)
        if ref is None:
            unplaceable += 1
            continue
        by_ref.setdefault(ref, []).append(a)

    llm = get_llm(for_tool_use=False, temperature=0.3, agent_name=agent_name)
    done, empty = [], []
    for gate in due:
        ref = gate["ref"]
        rows = by_ref.get(ref) or []
        per_channel: dict[str, dict] = {}
        for a in rows:
            metrics = attempt_metrics(agent_name, str(a.get("id") or a.get("attempt_id") or ""))
            if metrics is None:
                continue
            per_channel[_channel_of(a)] = _numbers(metrics)
        if not per_channel:
            empty.append(ref)
            continue
        aihe = next((r.get("aihe") for r in told if r.get("ref") == ref), ref)
        record = {
            "ref": ref,
            "aihe": aihe,
            "julkaistu": gate["julkaistu"] or sorted(per_channel),
            "mittaus": {**per_channel, "haettu": _iso_now()},
        }
        record["opittu"] = _learned(llm, ref, str(aihe), per_channel, told)
        key = MITTAUS_KEY.format(ref=ref)
        w = _aimeat_call(
            agent_name,
            "aimeat_memory_write",
            {"key": key, "value": record, "visibility": "owner", "tags": ["julkaisupoyta", "mittaus", f"ref:{ref}"]},
        )
        if w is None:
            print(f"[{agent_name}] WRITE FAILED {key}", file=sys.stderr)
            continue
        remember_measured(agent_name, ref, {k: record[k] for k in ("aihe", "julkaistu", "mittaus", "opittu")})
        record_deliverable_key(task_id, key)
        done.append(ref)

    parts = [f"OK: measured {len(done)} run(s)" + (f" ({', '.join(done)})" if done else "")]
    if empty:
        parts.append(
            f"{len(empty)} run(s) had no readable metrics yet ({', '.join(empty)}) — left unmeasured "
            "rather than recorded as zero."
        )
    if unplaceable:
        parts.append(f"{unplaceable} attempt record(s) named no ref and were skipped, not guessed onto a run.")
    return " ".join(parts)


# ── crew tools ───────────────────────────────────────────────────────────────────────────────────
def make_toimittaja_tools(agent_name: str, task: dict | None = None, prompt: str | None = None) -> list:
    """The editor's ONE tool. The ref is resolved (or minted) in code; the model never types a key."""
    from crewai.tools import tool

    from crewaimeat.julkaisu_pipeline import resolve_ref

    ref = resolve_ref(task, prompt)
    task_id = (task or {}).get("id")

    @tool("valitse_ja_kaiva")
    def valitse_ja_kaiva_tool() -> str:
        """Fetch the public changelog, pick the ONE entry worth telling (skipping everything already
        told), dig out what it replaced and who was stuck, and store the aineisto for this run. Takes
        no arguments. Call it EXACTLY ONCE and report what it returns, including any FAILED line."""
        return valitse_aihe(agent_name, ref=ref, task_id=task_id)

    valitse_ja_kaiva_tool.cache_function = lambda *_a, **_k: False
    return [valitse_ja_kaiva_tool]


def make_mittari_tools(agent_name: str, task: dict | None = None) -> list:
    """The measurer's ONE tool. Deterministic apart from the single learned sentence."""
    from crewai.tools import tool

    task_id = (task or {}).get("id")

    @tool("mittaa_julkaisut")
    def mittaa_julkaisut_tool() -> str:
        """Read what every published piece older than 24 h actually did, and fold it into the desk's
        running memory so the next pick is better informed. Takes no arguments. Call it EXACTLY ONCE
        and report what it returns verbatim, including runs it could not measure."""
        return mittaa_julkaisut(agent_name, task_id=task_id)

    mittaa_julkaisut_tool.cache_function = lambda *_a, **_k: False
    return [mittaa_julkaisut_tool]

"""KANSI — the order, the research and the angles. The person directs; these two agents work.

The v2 desk had an editor that CHOSE the story. It chose well, and that was the problem: the owner
wants to direct, not delegate. So the chain turns around — a person picks the entries, the director
and the style in the app, and writes ``julkaisu.{ref}.tilaus``. From there:

  * **julkaisu-tutkija** searches the OPEN WEB about what the order names — not the changelog, which
    the person has already read — and writes ``julkaisu.{ref}.tausta``: findings with source URLs,
    named comparable products, why this week, the strongest counter-argument, and what it looked for
    and could not find.
  * **julkaisu-ohjaaja** reads the order, the research and the node's own directors list, and writes
    ``julkaisu.{ref}.kulmat``: as many DIFFERENT ways into the material as the order asked for, each
    with the first line actually written and an honest probability of landing.

Then a person picks one (gate 1, ``julkaisu.{ref}.valinta``) and the four writers work from THAT.

The split is the same as everywhere in this repo: judgement is the model's, the loop is code. The
model decides what is worth searching for and what the angles are; the code runs the searches,
fetches the pages, and REFUSES a finding whose source URL is not one of the pages that were actually
read. That last check is what makes this step worth having — a researcher that can invent a citation
is worse than no researcher at all.

The directors list is READ from ``julkaisu.ohjaajat`` on every run and never copied here: the person
adds directors there, and a hardcoded list would go stale the first time they do.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

import requests
from aimeat_crewai.provenance import HumanInvolvement, Level, Method, declare, source

from crewaimeat.aimeat_crew import _aimeat_call, record_deliverable_key
from crewaimeat.julkaisu_pipeline import (  # the canonical key templates + the address ladder
    KULMAT_KEY,
    LISAA,
    OHJAAJAT_KEY,
    TAUSTA_KEY,
    TILAUS_KEY,
    VALINTA_KEY,
    run_address,
)
from crewaimeat.llm import get_llm, resolved_model, resolved_provider
from crewaimeat.memory_tools import read_owner_key
from crewaimeat.prose_style import FINNISH_NATIVE_STYLE

_MAX_ATTEMPTS = 3
_MAX_QUERIES = 6
_RESULTS_PER_QUERY = 6
_FETCH_ARTICLES = 5
_MIN_LOYDOKSET = 2  # the offer's success_signal floor


# ── the order ────────────────────────────────────────────────────────────────────────────────────
def read_tilaus(agent_name: str, ref: str) -> dict:
    """The person's order. Raises when it is missing — nothing downstream is inventable without it."""
    key = TILAUS_KEY.format(ref=ref)
    value = read_owner_key(agent_name, key)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = None
    if not isinstance(value, dict) or not (value.get("merkinnat") or []):
        raise LookupError(
            f"order '{key}' is missing or names no entries — a person has not placed one yet. "
            "Nothing was written; this agent does not choose the subject."
        )
    return value


def read_ohjaajat(agent_name: str) -> dict:
    """The node's directors + styles list. Raises when it is not there.

    Read every run, never copied into this file: the person adds directors to that key, and a
    hardcoded list would be wrong the first time they did.
    """
    value = read_owner_key(agent_name, OHJAAJAT_KEY)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = None
    if not isinstance(value, dict) or not (value.get("ohjaajat") or []):
        raise LookupError(
            f"directors list '{OHJAAJAT_KEY}' is missing or empty — the order names a director this "
            "agent cannot look up. Nothing was written."
        )
    return value


def _director(doc: dict, director_id: str) -> dict | None:
    for d in doc.get("ohjaajat") or []:
        if isinstance(d, dict) and str(d.get("id", "")).casefold() == str(director_id or "").casefold():
            return d
    return None


def _director_lines(d: dict) -> str:
    """One director as the node describes him. `teksti` and `esimerkki` matter as much as `kuva`:
    they are how he writes PROSE, which is what three of the four writers actually produce."""
    rows = [
        f"  {label}: {d.get(field)}"
        for field, label in (
            ("teksti", "TEKSTI (näin hän kirjoittaa)"),
            ("kuva", "kuva"),
            ("rytmi", "rytmi"),
            ("vari", "väri"),
            ("aani", "ääni"),
            ("sopii", "sopii"),
            ("ei_sovi", "ei sovi"),
        )
        if str(d.get(field) or "").strip()
    ]
    if str(d.get("esimerkki") or "").strip():
        rows.append(f"  esimerkkirivi: {d['esimerkki']}")
    return f"{d.get('nimi')} ({d.get('id')})\n" + "\n".join(rows)


def _as_director_list(ohjaaja: Any) -> list[dict]:
    """The order's directors as a list, whatever shape it arrives in.

    `ohjaajat` is a LIST and that is the point — one to three at once, each with its own reading and
    its own weight. A single object (the older shape, and what a hand-written order may still carry)
    is simply a list of one.
    """
    if isinstance(ohjaaja, list):
        return [o for o in ohjaaja if isinstance(o, dict) and (o.get("id") or o.get("ids"))]
    if isinstance(ohjaaja, dict) and (ohjaaja.get("id") or ohjaaja.get("ids")):
        ids = [i for i in (ohjaaja.get("ids") or []) if i]
        if ids:  # a legacy blend carried its members in `ids`
            return [{"id": i, "kaytto": ohjaaja.get("kaytto"), "paino": ohjaaja.get("paino")} for i in ids]
        return [ohjaaja]
    return []


_KAYTTO_TAIL = {
    "full": "Tee tämä hänen tavallaan kauttaaltaan.",
    "inspired-by": "Ota VAIN henki ja yksi tunnistettava ele. Nimeä se: 'inspired by <nimi>'. Älä matki.",
    "opposite-of": "KÄÄNNÄ hänet ylösalaisin ja nimeä mikä käännettiin.",
    "blend": "Sulauta: rytmi yhdeltä, kuva toiselta, ääni kolmannelta.",
    "free-hand": "Tyyli on lähtökohta, saat poiketa jos jokin toimii paremmin — kerro missä poikkesit.",
}


def director_block(doc: dict, ohjaaja: Any) -> str:
    """The direction for this run, rendered from the NODE's list — one to three directors with weights.

    Raises on a director the list does not carry: an order naming an unknown director is a mistake
    worth surfacing, not something to quietly ignore and write in no style at all.
    """
    ordered = _as_director_list(ohjaaja)
    if not ordered:
        return "OHJAAJA: ei ohjaajaa — kirjoita talon omalla äänellä, tilatussa tyylissä."
    rows, total = [], sum(int(o.get("paino") or 0) for o in ordered)
    for o in sorted(ordered, key=lambda x: -int(x.get("paino") or 0)):
        d = _director(doc, o.get("id"))
        if d is None:
            known = ", ".join(str(x.get("id")) for x in (doc.get("ohjaajat") or [])[:20])
            raise LookupError(
                f"the order names director {o.get('id')!r}, which {OHJAAJAT_KEY} does not carry. Known: {known}"
            )
        kaytto = str(o.get("kaytto") or "inspired-by").strip().casefold()
        how = (doc.get("kaytto") or {}).get(kaytto) or ""
        share = f" — OSUUS {int(o['paino'])}%" if total and o.get("paino") else ""
        rows.append(f"[{kaytto}{share}] {how}\n{_director_lines(d)}\n  → {_KAYTTO_TAIL.get(kaytto, '')}")
    head = "OHJAUS — " + ("yksi ohjaaja" if len(rows) == 1 else f"{len(rows)} ohjaajaa yhtä aikaa") + ":\n\n"
    tail = ""
    if len(rows) > 1:
        tail = (
            "\n\nPAINOT RATKAISEVAT SEKOITUKSEN, ÄLÄ KESKIARVOISTA NIITÄ PUUROKSI. 70/30 luetaan "
            "niin, että toinen kantaa työn ja toinen leikkaa sen poikki — ei niin, että molemmat "
            "ovat puoliteholla. Kerro kentässä 'ohjaaja_ele' KUKA teki mitäkin."
        )
    return head + "\n\n".join(rows) + tail


def style_block(doc: dict, tyylit: Any) -> str:
    """The ordered styles — a LIST, because a piece can be several at once (tight AND numbers-led)."""
    wanted = [tyylit] if isinstance(tyylit, str) else [t for t in (tyylit or []) if isinstance(t, str)]
    wanted = [t for t in wanted if t.strip()] or ["asiallinen"]
    known = {str(s.get("id", "")).casefold(): s for s in (doc.get("tyylit") or []) if isinstance(s, dict)}
    rows = []
    for t in wanted:
        s = known.get(t.casefold())
        rows.append(f"  - {s.get('nimi')}: {s.get('kuvaus')}" if s else f"  - {t}")
    head = "TYYLI:" if len(rows) == 1 else "TYYLIT — pidä KAIKKI näistä yhtä aikaa:"
    return head + "\n" + "\n".join(rows)


# Two styles ask for invention ON PURPOSE. Everything else in this chain refuses an unsourced claim;
# these two want an idea instead of a defensible one, so the rule changes shape rather than lifting:
# mark what you invented, and attach NO source to it. Inventing is allowed when asked for; dressing
# an invention as a finding never is.
INVENTIVE_STYLES = ("villi", "spekulaatio")


def invention_ordered(tyylit: Any) -> list[str]:
    wanted = [tyylit] if isinstance(tyylit, str) else [t for t in (tyylit or []) if isinstance(t, str)]
    return [t for t in wanted if t.strip().casefold() in INVENTIVE_STYLES]


def _invention_note(tyylit: Any) -> str:
    inventive = invention_ordered(tyylit)
    if not inventive:
        return ""
    return (
        f"\n\nTILATTU TYYLI ({'/'.join(inventive)}) PYYTÄÄ MENEMÄÄN LÄHTEIDEN YLI — se on tarkoitus, "
        "ei lipsahdus. Kärjistä, vertaa kaukaa, kuvittele seuraus viiden vuoden päähän.\n"
        "  · MERKITSE jokainen keksitty kohta itse tekstiin, jotta sen erottaa.\n"
        "  · JÄTÄ 'lahteet' TYHJÄKSI. Keksitylle väitteelle ei liitetä lähdettä joka ei tue sitä.\n"
        "Keksiminen on sallittua kun sitä pyydetään; keksityn pukeminen löydökseksi ei ole koskaan."
    )


def entries_block(tilaus: dict, body_chars: int = 2500) -> str:
    rows = []
    for e in tilaus.get("merkinnat") or []:
        if not isinstance(e, dict):
            continue
        rows.append(f"[{e.get('date')}] {e.get('title')}\n{str(e.get('body') or '')[:body_chars]}")
    return "\n\n".join(rows)


# ── the open web ─────────────────────────────────────────────────────────────────────────────────
def web_search(query: str, max_results: int = _RESULTS_PER_QUERY, time_range: str | None = None) -> list[dict]:
    """Structured live-web results [{title, url, snippet, published}] — SearXNG first, then DuckDuckGo.

    Deliberately NOT the crewai search tools: those render text for a model to read, and this step
    needs the URLs as data so the check below can refuse a source that was never actually returned.
    Both backends are keyless, so the researcher has no single point of failure.
    """
    base = (os.getenv("SEARXNG_URL") or "http://localhost:21333").rstrip("/")
    try:
        r = requests.get(
            f"{base}/search",
            params={"q": query, "format": "json", "categories": "general", "language": "all"},
            timeout=20,
        )
        if r.status_code == 200:
            out = []
            for it in (r.json().get("results") or [])[:max_results]:
                if it.get("url") and it.get("title"):
                    out.append(
                        {
                            "title": it["title"],
                            "url": it["url"],
                            "snippet": str(it.get("content") or "")[:400],
                            "published": str(it.get("publishedDate") or "")[:10],
                        }
                    )
            if out:
                return out
    except Exception as exc:  # noqa: BLE001 — a search backend being down is weather, not a crash
        print(f"[julkaisu-tutkija] SearXNG unavailable ({exc!r}) -> DuckDuckGo", file=sys.stderr)
    try:
        from crewaimeat.ddg_search import _ddgs

        with _ddgs()() as ddgs:
            rows = list(ddgs.text(query, max_results=max_results, region="wt-wt", timelimit=time_range))
        return [
            {
                "title": x.get("title") or "",
                "url": x.get("href") or x.get("url") or "",
                "snippet": str(x.get("body") or "")[:400],
                "published": "",
            }
            for x in rows
            if (x.get("href") or x.get("url"))
        ]
    except Exception as exc:  # noqa: BLE001
        print(f"[julkaisu-tutkija] DuckDuckGo failed for {query!r}: {exc!r}", file=sys.stderr)
        return []


def _plan_queries(llm, tilaus: dict) -> list[str]:
    """What to search for — the model's judgement; running them is code's job."""
    prompt = (
        "Olet tutkija. Alla on muutosmerkinnät joista tehdään julkaisu. Keksi 4–6 HAKULAUSETTA joilla "
        "löytyy AVOIMESTA VERKOSTA (ei tästä muutoslokista) taustaa tälle aiheelle:\n"
        "- onko joku kirjoittanut tästä ongelmasta ja mitä sanoi\n"
        "- mitä vertailukelpoiset tuotteet tekevät asialle, nimeltä\n"
        "- miksi juuri nyt: päivämäärä, sääntely, muutos jonka joku muu on jo huomannut\n"
        "- vastaväite: kuka sanoisi ettei tämä ole iso juttu\n\n"
        "Kirjoita hakulauseet ENGLANNIKSI (verkosta löytyy enemmän), yksi per rivi, ei numerointia, "
        "ei mitään muuta.\n\n"
        "MERKINNÄT:\n"
        + entries_block(tilaus, 1200)
        + ("\n\nLISÄOHJE: " + str(tilaus.get("lisaohje") or "") if tilaus.get("lisaohje") else "")
    )
    raw = llm.call([{"role": "user", "content": prompt}])
    lines = [re.sub(r"^[-*\d.)\s]+", "", ln).strip(" \"'") for ln in str(raw).splitlines() if ln.strip()]
    return [ln for ln in lines if 8 <= len(ln) <= 200][:_MAX_QUERIES]


def gather_sources(llm, tilaus: dict) -> tuple[list[dict], str]:
    """(results, corpus) — every result the searches returned, plus the readable text of the top pages."""
    queries = _plan_queries(llm, tilaus)
    if not queries:
        queries = [str(e.get("title") or "") for e in (tilaus.get("merkinnat") or []) if e.get("title")][:3]
    seen, results = set(), []
    for q in queries:
        for hit in web_search(q):
            if hit["url"] not in seen:
                seen.add(hit["url"])
                results.append(hit)
    print(f"[julkaisu-tutkija] {len(queries)} queries -> {len(results)} unique result(s)", file=sys.stderr)
    corpus = ""
    if results:
        try:
            # `fetch_article_text` is a crewai Tool, not a plain function — calling it directly
            # raises TypeError("'Tool' object is not callable"), which the except below swallowed
            # into "working from snippets". Measured on prod 2026-08-25: the researcher never once
            # read a page body, and nothing said so louder than one debug line.
            from crewaimeat.article_extract import fetch_article_text

            corpus = fetch_article_text.run(
                urls_json=json.dumps([r["url"] for r in results]), max_articles=_FETCH_ARTICLES
            )
        except Exception as exc:  # noqa: BLE001 — snippets alone still beat nothing, and we say so
            print(f"[julkaisu-tutkija] article fetch failed ({exc!r}); working from snippets", file=sys.stderr)
    return results, corpus


# ── the research contract, checked in code ───────────────────────────────────────────────────────
_URL_RE = re.compile(r"https?://[^\s\"'<>)]+", re.I)


def _norm_url(u: str) -> str:
    return str(u or "").strip().rstrip("/.,);").casefold()


def check_tausta(doc: dict, allowed: set[str]) -> list[str]:
    """The researcher's contract. Violations in Finnish — the prompt is Finnish.

    The load-bearing rule is the source allowlist: a `lahde` that is not one of the pages the search
    actually returned is an INVENTED citation, and a researcher that can invent one is worse than no
    researcher at all. It is checked, not requested.
    """
    bad: list[str] = []
    # Normalise the allowlist HERE rather than trusting the caller to have done it: a trailing slash
    # is not a different page, and a check that rejects a real source over punctuation would be
    # switched off within a week.
    allowed = {_norm_url(u) for u in (allowed or set())}
    found = doc.get("loydokset")
    if not isinstance(found, list) or len(found) < _MIN_LOYDOKSET:
        bad.append(
            f"löydöksiä on {len(found) if isinstance(found, list) else 0} — niitä pitää olla vähintään {_MIN_LOYDOKSET}."
        )
        found = found if isinstance(found, list) else []
    for i, f in enumerate(found, 1):
        if not isinstance(f, dict):
            bad.append(f"löydös {i} ei ole olio.")
            continue
        for field in ("vaite", "lahde", "merkitys"):
            if not str(f.get(field) or "").strip():
                bad.append(f"löydös {i}: '{field}' on tyhjä.")
        url = _norm_url(f.get("lahde"))
        if url and not _URL_RE.match(str(f.get("lahde") or "").strip()):
            bad.append(f"löydös {i}: 'lahde' ei ole URL ({f.get('lahde')!r}).")
        elif url and allowed and url not in allowed:
            bad.append(
                f"löydös {i}: lähde {f.get('lahde')!r} EI ole niiden sivujen joukossa jotka haku palautti "
                "— käytä vain luettuja lähteitä, älä keksi osoitetta."
            )
    for i, v in enumerate(doc.get("vertailu") or [], 1):
        if not isinstance(v, dict):
            bad.append(f"vertailu {i} ei ole olio.")
            continue
        if not str(v.get("kuka") or "").strip() or not str(v.get("mita_tekee") or "").strip():
            bad.append(f"vertailu {i}: 'kuka' ja 'mita_tekee' ovat pakollisia.")
        url = _norm_url(v.get("lahde"))
        if url and allowed and url not in allowed:
            bad.append(f"vertailu {i}: lähde {v.get('lahde')!r} ei ole luettujen sivujen joukossa.")
    for field in ("ajankohtaisuus", "vastavaite", "ei_loytynyt"):
        if not str(doc.get(field) or "").strip():
            bad.append(
                f"'{field}' on tyhjä — se on osa vastausta."
                + (
                    " Tyhjä haku on tulos, ja sen salaaminen tekee tästä vaiheesta hyödyttömän."
                    if field == "ei_loytynyt"
                    else ""
                )
            )
    return bad


def parse_json_object(raw: str) -> dict | None:
    """The first JSON object in a model reply, fences and preamble tolerated."""
    text = raw if isinstance(raw, str) else str(raw)
    text = re.sub(r"^\s*```(?:json)?|```\s*$", "", text.strip(), flags=re.M)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        out = json.loads(text[start : end + 1])
    except ValueError:
        return None
    return out if isinstance(out, dict) else None


def _tausta_prompt(tilaus: dict, results: list[dict], corpus: str) -> str:
    src = "\n".join(
        f"- {r['title']} | {r['url']}" + (f" | {r['published']}" if r["published"] else "") + f"\n  {r['snippet']}"
        for r in results[:24]
    )
    return (
        "Olet tutkija. Alla on julkaisun aihe ja se mitä AVOIMESTA VERKOSTA löytyi. Kokoa tausta, "
        "jonka pohjalta joku muu keksii kulmat.\n\n"
        "SÄÄNNÖT — nämä ovat koneellisesti tarkistettuja:\n"
        "- Jokainen väite kantaa lähde-URLin, ja URLin on oltava YKSI ALLA OLEVISTA. Älä keksi "
        "osoitetta, älä muistele osoitetta.\n"
        "- Älä esitä päättelyä löydöksenä. Jos et löytänyt jotain, se kuuluu kenttään 'ei_loytynyt'.\n"
        "- 'ei_loytynyt' ei ole valinnainen. Tyhjä haku on tulos.\n"
        "- 'vastavaite' on VAHVIN perustelu sille ettei tämä ole kiinnostavaa. Kirjoita se rehellisesti.\n\n"
        "AIHE:\n" + entries_block(tilaus, 1500) + "\n\n"
        "HAKUTULOKSET (vain nämä URLit ovat sallittuja lähteitä):\n" + (src or "(ei tuloksia)") + "\n\n"
        "LUETTUJEN SIVUJEN TEKSTI:\n"
        + (
            corpus[:12000]
            if corpus
            else "(sivujen tekstiä ei saatu — käytä otsikoita ja katkelmia, ja sano se kentässä ei_loytynyt)"
        )
        + FINNISH_NATIVE_STYLE
        + "\n\nVASTAUKSEN MUOTO — pelkkä JSON-olio, ei mitään sen ympärille:\n"
        '{\n  "loydokset": [{"vaite": "yksi lause", "lahde": "https://…", "julkaistu": "2026-07-02", '
        '"merkitys": "miksi tämä merkitsee meidän tarinallemme"}],\n'
        '  "vertailu": [{"kuka": "nimetty tuote tai yritys", "mita_tekee": "…", "lahde": "https://…"}],\n'
        '  "ajankohtaisuus": "mikä tekee juuri tästä viikosta oikean",\n'
        '  "vastavaite": "vahvin perustelu ettei tämä ole kiinnostavaa",\n'
        '  "ei_loytynyt": "mitä etsit etkä löytänyt"\n}'
    )


def tutki_tausta(agent_name: str, task: dict | None = None, task_id: str | None = None) -> str:
    """Search the open web for what the order names and write julkaisu.<id>.tausta. Returns a report."""
    key, ref, rule = run_address(task, "tausta")
    addr = f" Address: {rule}."
    print(f"[{agent_name}] tausta -> {key} ({rule})", file=sys.stderr)
    try:
        tilaus = read_tilaus(agent_name, ref)
    except LookupError as exc:
        print(f"[{agent_name}] {exc}", file=sys.stderr)
        return f"FAILED: {exc}{addr}"

    llm = get_llm(for_tool_use=False, temperature=0.3, agent_name=agent_name)
    results, corpus = gather_sources(llm, tilaus)
    if not results:
        return (
            f"FAILED: the open web returned nothing for this order — no search backend answered "
            f"(SearXNG at {os.getenv('SEARXNG_URL') or 'http://localhost:21333'} and DuckDuckGo both). "
            f"Nothing was written to {key}; an unsourced 'tausta' is the one output this step must "
            f"never produce.{addr}"
        )
    allowed = {_norm_url(r["url"]) for r in results}

    base = _tausta_prompt(tilaus, results, corpus)
    prompt, doc, violations = base, None, ["(no attempt ran)"]
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        out = llm.call([{"role": "user", "content": prompt}])
        doc = parse_json_object(out)
        violations = ["vastaus ei ollut JSON-olio."] if doc is None else check_tausta(doc, allowed)
        print(
            f"[{agent_name}] tausta attempt {attempt}/{_MAX_ATTEMPTS}: "
            + ("OK" if not violations else "; ".join(violations)),
            file=sys.stderr,
        )
        if not violations:
            break
        prompt = base + "\n\nKorjaa nämä ja kirjoita koko JSON uudestaan:\n" + "\n".join(f"- {v}" for v in violations)
    if violations or doc is None:
        return (
            f"FAILED: the research did not meet the contract after {_MAX_ATTEMPTS} attempts — "
            + "; ".join(violations)
            + f". Nothing was written to {key}.{addr}"
        )

    written = _aimeat_call(
        agent_name,
        "aimeat_memory_write",
        {
            "key": key,
            "value": doc,
            "visibility": "owner",
            "tags": ["julkaisupoyta", "tausta", f"ref:{ref}"],
            "ai_provenance": declare(
                Level.SYNTHESIZED,
                method=Method.SYNTHESIZED,
                human_involvement=HumanInvolvement.NONE,
                model=resolved_model(llm),
                provider=resolved_provider(),
                sources=[source(r["url"]) for r in results[:20]],
                notes="KANSI: open-web research for a person's order; nothing published.",
            ),
        },
    )
    if written is None:
        return f"FAILED to write '{key}' (tunnel/transport) — the research did not land.{addr}"
    record_deliverable_key(task_id, key)
    return (
        f"OK: {len(doc.get('loydokset') or [])} finding(s), {len(doc.get('vertailu') or [])} comparison(s) "
        f"-> {key}, from {len(results)} web result(s). Not published anywhere.{addr}"
    )


# ── the angles ───────────────────────────────────────────────────────────────────────────────────
_ANGLE_FIELDS = ("otsikko", "kulma", "avaus", "miksi_toimii", "kenelle", "nojaa", "perustelu", "ohjaaja_ele", "riski")
_ORDER_TYYLIT = "tyylit"  # the order carries a LIST of styles; `tyyli` was the single-value shape
_SPREAD_MIN = 15  # a row of near-identical probabilities is a tell that nothing was judged


def read_tausta(agent_name: str, ref: str) -> dict:
    key = TAUSTA_KEY.format(ref=ref)
    value = read_owner_key(agent_name, key)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = None
    if not isinstance(value, dict) or not (value.get("loydokset") or []):
        raise LookupError(f"research '{key}' is missing or has no findings — the researcher step has not run.")
    return value


def existing_kulmat(agent_name: str, ref: str) -> list[dict]:
    """Angles already offered for this run. A second batch APPENDS — the person is still looking at
    the first one in the app, and replacing it would delete what they were reading."""
    value = read_owner_key(agent_name, KULMAT_KEY.format(ref=ref))
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if isinstance(value, dict):
        return [k for k in (value.get("kulmat") or []) if isinstance(k, dict)]
    return []


_GROUND_WORD = re.compile(r"[\wåäöÅÄÖ]{5,}")


def _grounding_tokens(tausta: dict) -> set[str]:
    """Every distinctive word the research offers an angle to lean on.

    All of it counts, not just a finding's sentence: the spec says an angle may lead with the
    counter-argument or the comparison, so `vastavaite`, `vertailu` and `ajankohtaisuus` are just as
    real a place to stand as `loydokset`.
    """
    parts: list[str] = [str(tausta.get(f) or "") for f in ("ajankohtaisuus", "vastavaite", "ei_loytynyt")]
    for f in tausta.get("loydokset") or []:
        if isinstance(f, dict):
            parts += [str(f.get("vaite") or ""), str(f.get("lahde") or ""), str(f.get("merkitys") or "")]
    for v in tausta.get("vertailu") or []:
        if isinstance(v, dict):
            parts += [str(v.get("kuka") or ""), str(v.get("mita_tekee") or "")]
    # The names of the research's own sections are valid pointers too — "vastaväite" IS where an
    # angle can stand, and rejecting the word for it would be pedantry, not a check.
    parts += ["vastavaite vastaväite vertailu ajankohtaisuus taustalöydös taustaloydos löydös loydos"]
    return {w.casefold() for p in parts for w in _GROUND_WORD.findall(p)}


def normalise_kulmat(angles: Any) -> list[str]:
    """Tidy what is unambiguous before judging it. Returns notes about what was tidied.

    One thing only: an entry in `lahteet` that is not a URL. A model writing `"lahteet": ["changelog"]`
    has named where the angle stands in the wrong field — the intent is not in doubt, and the correct
    shape for it is an empty list. Dropping the token is normalising, not guessing; an entry that IS
    a URL is left alone so the invented-citation check can still refuse it.
    """
    notes: list[str] = []
    for a in angles if isinstance(angles, list) else []:
        if not isinstance(a, dict) or not isinstance(a.get("lahteet"), list):
            continue
        kept = [s for s in a["lahteet"] if _URL_RE.match(str(s).strip())]
        dropped = [s for s in a["lahteet"] if s not in kept]
        if dropped:
            a["lahteet"] = kept
            notes.append(f"kulma {a.get('nro')}: 'lahteet' sisälsi ei-URLin ({dropped[0]!r}), poistettu")
    return notes


def check_kulmat(angles: list, wanted: int, tausta: dict, tyylit: Any = None) -> list[str]:
    """The director's contract, checked in code."""
    bad: list[str] = []
    if not isinstance(angles, list) or not angles:
        return ["kulmia ei tullut yhtään."]
    if len(angles) > wanted:
        bad.append(f"kulmia on {len(angles)} — tilaus pyysi {wanted}.")
    # What an angle may legitimately lean on: ANY of the research, not just a finding's exact
    # sentence. The spec says an angle may lead with the counter-argument or the comparison, and the
    # first version of this check compared 40-character prefixes — so `nojaa: "NIS2-direktiivi
    # (taustalöydös)"` and `nojaa: "Vastaväite ja changelog"` were both rejected as ungrounded, and
    # a perfectly good angle set failed three attempts twice in a row on prod (2026-08-25). A check
    # that cries wolf gets switched off; this one matches on a shared DISTINCTIVE WORD instead.
    claim_tokens = _grounding_tokens(tausta)
    allowed_sources = {_norm_url(f.get("lahde")) for f in (tausta.get("loydokset") or []) if isinstance(f, dict)}
    allowed_sources |= {_norm_url(v.get("lahde")) for v in (tausta.get("vertailu") or []) if isinstance(v, dict)}
    allowed_sources.discard("")
    inventive = invention_ordered(tyylit)
    probs = []
    for i, a in enumerate(angles, 1):
        if not isinstance(a, dict):
            bad.append(f"kulma {i} ei ole olio.")
            continue
        for field in _ANGLE_FIELDS:
            if not str(a.get(field) or "").strip():
                bad.append(f"kulma {i}: '{field}' on tyhjä.")
        p = a.get("todennakoisyys")
        if not isinstance(p, int) or not 0 <= p <= 100:
            bad.append(f"kulma {i}: 'todennakoisyys' on {p!r} — kokonaisluku 0–100.")
        else:
            probs.append(p)
        nojaa = str(a.get("nojaa") or "").strip()
        on_changelog = "changelog" in nojaa.casefold()
        grounded = on_changelog or bool({w.casefold() for w in _GROUND_WORD.findall(nojaa)} & claim_tokens)
        if nojaa and claim_tokens and not grounded:
            bad.append(
                f"kulma {i}: 'nojaa' ({nojaa[:50]!r}) ei osoita mihinkään taustassa olevaan — "
                "nimeä löydös, vertailu tai vastaväite, tai kirjoita 'changelog'."
            )
        # `lahteet` is what the app puts on the card so a reader can CHECK the claim instead of
        # trusting it. Required whenever the angle rests on research; deliberately empty when it
        # rests on the changelog entry alone, or when an inventive style was ordered — attaching a
        # source that does not support what you wrote is the one thing that is never allowed.
        srcs = a.get("lahteet")
        if not isinstance(srcs, list):
            bad.append(f"kulma {i}: 'lahteet' puuttuu — anna lista (tyhjä lista jos nojaa on 'changelog').")
            continue
        # An INVENTED URL is the danger this check exists for. A bare word like "changelog" is not a
        # citation at all — it is the model naming where the angle stands, in the wrong field — and
        # `normalise_kulmat` has already dropped it. Failing a whole run over that token is what
        # happened on 2026-08-25: three attempts burned, nothing written, and the app reported the
        # director as offline when it had run and produced perfectly good angles.
        stray = [s for s in srcs if _URL_RE.match(str(s).strip()) and _norm_url(s) not in allowed_sources]
        if stray and allowed_sources:
            bad.append(
                f"kulma {i}: lähde {stray[0]!r} ei ole taustan lähteiden joukossa — kopioi tarkka URL "
                "löydöksestä, älä kirjoita omaa."
            )
        if not srcs and not on_changelog and not inventive and allowed_sources:
            bad.append(
                f"kulma {i}: 'lahteet' on tyhjä vaikka kulma nojaa tutkimukseen — kopioi käyttämiesi "
                "löydösten URLit, tai kirjoita 'changelog' kenttään nojaa."
            )
        if srcs and inventive:
            bad.append(
                f"kulma {i}: tyyli {'/'.join(inventive)} keksii tarkoituksella, joten 'lahteet' jätetään "
                "TYHJÄKSI — keksitylle väitteelle ei liitetä lähdettä joka ei tue sitä."
            )
    if len(probs) >= 3 and max(probs) - min(probs) < _SPREAD_MIN:
        bad.append(
            f"todennäköisyydet ovat {sorted(probs)} — hajonta on alle {_SPREAD_MIN}. Arvioi rehellisesti: "
            "jos osa kulmista on heikkoja, sano se numerossa."
        )
    otsikot = [str(a.get("otsikko") or "").casefold() for a in angles if isinstance(a, dict)]
    if len(set(otsikot)) < len(otsikot):
        bad.append("kaksi kulmaa on saman nimisiä — nämä ovat eri tarinoita, eivät saman sanamuotoja.")
    return bad


def _kulmat_prompt(tilaus: dict, tausta: dict, doc: dict, wanted: int, already: list[dict], extra: str) -> str:
    prior = ""
    if already:
        prior = "\n\nJO TARJOTUT KULMAT (älä toista näitä, keksi ERI tarinoita):\n" + "\n".join(
            f"  {a.get('nro')}. {a.get('otsikko')} — {a.get('kulma')}" for a in already
        )
    findings = "\n".join(
        f"- {f.get('vaite')} [{f.get('lahde')}] — {f.get('merkitys')}"
        for f in (tausta.get("loydokset") or [])
        if isinstance(f, dict)
    )
    comps = "\n".join(
        f"- {v.get('kuka')}: {v.get('mita_tekee')}" for v in (tausta.get("vertailu") or []) if isinstance(v, dict)
    )
    return (
        f"Olet ohjaaja. Keksi {wanted} ERILAISTA KULMAA samaan aineistoon — ei {wanted} sanamuotoa "
        "yhdestä ideasta, vaan eri tarinoita jotka voisi kertoa. Yksi voi lähteä tutkimuslöydöksestä, "
        "yksi vastaväitteestä, yksi yhden ihmisen turhautumisesta, yksi vertailusta, yksi luvusta.\n\n"
        "LÄHTEET: 'lahteet' sisältää VAIN URLeja, jotka on kopioitu tarkalleen alla olevista "
        "löydöksistä. Jos kulma nojaa pelkkään muutosmerkintään, kirjoita nojaa: 'changelog' ja "
        "JÄTÄ lahteet TYHJÄKSI listaksi — älä kirjoita sanaa 'changelog' lähteeksi.\n\n"
        "JOKAINEN KULMA KANTAA TODENNÄKÖISYYDEN: kuinka todennäköisesti se uppoaa juuri tähän "
        "kohdeyleisöön, 0–100, ja perustelun. Hajota ne rehellisesti — viisi kahdeksankymppistä on "
        "merkki siitä ettei mitään arvioitu. Jos kulma on heikko, anna sille matala luku ja sano miksi.\n\n"
        + director_block(doc, tilaus.get("ohjaajat") or tilaus.get("ohjaaja"))
        + "\n\n"
        + style_block(doc, tilaus.get("tyylit") or tilaus.get("tyyli"))
        + _invention_note(tilaus.get("tyylit") or tilaus.get("tyyli"))
        + "\n\n"
        "AIHE:\n" + entries_block(tilaus, 1500) + "\n\n"
        "TAUSTA — LÖYDÖKSET:\n" + (findings or "(ei löydöksiä)") + "\n\n"
        "VERTAILU:\n" + (comps or "(ei vertailua)") + "\n"
        f"AJANKOHTAISUUS: {tausta.get('ajankohtaisuus')}\n"
        f"VASTAVÄITE: {tausta.get('vastavaite')}\n"
        f"EI LÖYTYNYT: {tausta.get('ei_loytynyt')}\n"
        + (f"\nLISÄOHJE TILAAJALTA: {extra}" if extra else "")
        + prior
        + FINNISH_NATIVE_STYLE
        + "\n\nVASTAUKSEN MUOTO — pelkkä JSON-olio:\n"
        '{\n  "kulmat": [{"otsikko": "lyhyt nimi", "kulma": "se yksi lause jonka lukija toistaisi", '
        '"avaus": "varsinainen ensimmäinen rivi, kirjoitettuna", "miksi_toimii": "miksi tämä uppoaa, '
        'kohdeyleisön kannalta", "kenelle": "kuka tarkalleen", "nojaa": "mihin taustan löydökseen '
        'tämä nojaa, tai changelog", "lahteet": ["https://tarkka-url-kaytetysta-loydoksesta"], '
        '"todennakoisyys": 72, "perustelu": "miksi juuri se luku eikä '
        'korkeampi", "ohjaaja_ele": "se yksi näkyvä ele joka on otettu ohjaajalta", "riski": "mikä '
        'tässä voi mennä pieleen"}],\n  "notes": "mitä jätit tietoisesti yrittämättä"\n}'
    )


def tee_kulmat(agent_name: str, task: dict | None = None, task_id: str | None = None) -> str:
    """Write (or extend) julkaisu.<id>.kulmat — as many different angles as the order asked for."""
    key, ref, rule = run_address(task, "kulmat")
    addr = f" Address: {rule}."
    print(f"[{agent_name}] kulmat -> {key} ({rule})", file=sys.stderr)
    try:
        tilaus = read_tilaus(agent_name, ref)
        tausta = read_tausta(agent_name, ref)
        ohjaajat = read_ohjaajat(agent_name)
    except LookupError as exc:
        print(f"[{agent_name}] {exc}", file=sys.stderr)
        return f"FAILED: {exc}{addr}"

    # A "lisaa" answer at the angle gate is the app's "Lisää kulmia" button: another batch, with
    # whatever new instruction the person typed, APPENDED to what they are already looking at.
    valinta = read_owner_key(agent_name, VALINTA_KEY.format(ref=ref))
    valinta = valinta if isinstance(valinta, dict) else {}
    more_round = str(valinta.get("vastaus") or "").strip().casefold() == LISAA
    extra = str((valinta.get("lisaohje") if more_round else tilaus.get("lisaohje")) or "").strip()
    already = existing_kulmat(agent_name, ref)
    try:
        wanted = max(1, min(5, int(tilaus.get("kulmia") or 5)))
    except (TypeError, ValueError):
        wanted = 5

    llm = get_llm(for_tool_use=False, temperature=0.7, agent_name=agent_name)
    try:
        base = _kulmat_prompt(tilaus, tausta, ohjaajat, wanted, already, extra)
        tyylit = tilaus.get("tyylit") or tilaus.get("tyyli")
    except LookupError as exc:  # an order naming a director the node does not carry
        return f"FAILED: {exc}{addr}"
    prompt, doc, violations = base, None, ["(no attempt ran)"]
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        out = llm.call([{"role": "user", "content": prompt}])
        doc = parse_json_object(out)
        if doc is None:
            violations = ["vastaus ei ollut JSON-olio."]
        else:
            for note in normalise_kulmat(doc.get("kulmat")):
                print(f"[{agent_name}] {note}", file=sys.stderr)
            violations = check_kulmat(doc.get("kulmat"), wanted, tausta, tyylit)
        print(
            f"[{agent_name}] kulmat attempt {attempt}/{_MAX_ATTEMPTS}: "
            + ("OK" if not violations else "; ".join(violations)),
            file=sys.stderr,
        )
        if not violations:
            break
        prompt = base + "\n\nKorjaa nämä ja kirjoita koko JSON uudestaan:\n" + "\n".join(f"- {v}" for v in violations)
    if violations or doc is None:
        return (
            f"FAILED: the angles did not meet the contract after {_MAX_ATTEMPTS} attempts — "
            + "; ".join(violations)
            + f". Nothing was written to {key}.{addr}"
        )

    # Numbering CONTINUES from the highest existing nro: the person refers to angles by number in the
    # app, so re-using 1..n on a second batch would rename what they already read.
    start = max((int(a.get("nro") or 0) for a in already), default=0)
    fresh = []
    for i, a in enumerate(doc["kulmat"], start=1):
        a["nro"] = start + i
        fresh.append(a)
    ohj = tilaus.get("ohjaaja") or {}
    value = {
        "kulmat": [*already, *fresh],
        "ohjaaja_luettu": f"{'+'.join(ohj.get('ids') or [ohj.get('id') or '-'])}/{ohj.get('kaytto') or 'inspired-by'}",
        "notes": str(doc.get("notes") or "").strip(),
    }
    written = _aimeat_call(
        agent_name,
        "aimeat_memory_write",
        {
            "key": key,
            "value": value,
            "visibility": "owner",
            "tags": ["julkaisupoyta", "kulmat", f"ref:{ref}"],
            "ai_provenance": declare(
                Level.SYNTHESIZED,
                method=Method.SYNTHESIZED,
                human_involvement=HumanInvolvement.NONE,
                model=resolved_model(llm),
                provider=resolved_provider(),
                notes="KANSI: angles offered to a person for choosing; nothing published.",
            ),
        },
    )
    if written is None:
        return f"FAILED to write '{key}' (tunnel/transport) — the angles did not land.{addr}"
    record_deliverable_key(task_id, key)
    probs = ", ".join(f"#{a['nro']}:{a.get('todennakoisyys')}" for a in fresh)
    return (
        f"OK: {len(fresh)} new angle(s) ({probs}) -> {key}"
        + (f", appended to {len(already)} already offered (numbering continues)." if already else ".")
        + f" A person chooses next.{addr}"
    )


# ── crew tools ───────────────────────────────────────────────────────────────────────────────────
def make_tutkija_tools(agent_name: str, task: dict | None = None, prompt: str | None = None) -> list:
    """The researcher's ONE tool. The address is resolved from the dispatch; the model never types a key."""
    from crewai.tools import tool

    task_id = (task or {}).get("id")

    @tool("tutki_tausta")
    def tutki_tausta_tool() -> str:
        """Search the OPEN WEB about this run's order and store the findings, each with the source URL
        it actually came from, at the key THIS RUN WAS GIVEN. Takes no arguments. Call it EXACTLY ONCE
        and report what it returns verbatim, including any FAILED line."""
        return tutki_tausta(agent_name, task=task, task_id=task_id)

    tutki_tausta_tool.cache_function = lambda *_a, **_k: False
    return [tutki_tausta_tool]


def make_ohjaaja_tools(agent_name: str, task: dict | None = None, prompt: str | None = None) -> list:
    """The angle director's ONE tool."""
    from crewai.tools import tool

    task_id = (task or {}).get("id")

    @tool("tee_kulmat")
    def tee_kulmat_tool() -> str:
        """Read this run's order, its research and the node's directors list, and offer the person as
        many DIFFERENT angles as the order asked for — each with its first line written and an honest
        probability. Appends to any angles already offered. Takes no arguments. Call it EXACTLY ONCE
        and report what it returns verbatim."""
        return tee_kulmat(agent_name, task=task, task_id=task_id)

    tee_kulmat_tool.cache_function = lambda *_a, **_k: False
    return [tee_kulmat_tool]

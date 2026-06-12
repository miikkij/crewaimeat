"""market-scan: a PARAMETERIZED competitor/market analysis as a workspace contract.

The generalization of the morning report's competitor watch: point the same machinery at ANY
segment + area ("AI agent platforms, Espoo/Helsinki" today; "parturi-kampaamot, Leppävaara"
tomorrow) and get the same source-cited analysis: who the players are, what they sell and at what
price, how WE could sell against them (positioned against `our_offer`), and where they are visible
(social/channels).

Contract:
  inputs : `market-scan-request` (records) — DUE when status == 'requested' (one-shot) OR
           status == 'active' and now - last_run >= period_hours (recurring, e.g. weekly)
             { id, segment(required), area?, our_offer?, queries?(list overrides the generated
               ones), lang?('fi'|'en', default fi), period_hours?, email?(true -> the finished
               scan is also written as a mail-request record, which postman then sends),
               status, requested_by?, last_run?, result_ref?, error? }
  outputs: `market-scan` (DOCUMENT) — the analysis as a page (one-shot: scan-<id>;
           recurring: scan-<id>-<date>, which doubles as the once-per-period output-dedup)
  lifecycle: requested -> in-progress -> done | failed; an 'active' record stays active with
             last_run bumped (fires again next period)

Deterministic pipeline: build queries from the parameters -> SearXNG (general + news) ->
trafilatura-fetch the top pages -> ONE analyst distill (coding profile) with a parameterized
prompt -> write the document. Served by web-researcher (its second contract besides `research`).
"""

from __future__ import annotations

import datetime
import sys

from crewai.tools import tool

from crewaimeat.aimeat_crew import _aimeat_call, member_workspaces
from crewaimeat.article_extract import _trafilatura_text
from crewaimeat.llm import get_llm

AGENT = "web-researcher"
IN_SPACE, IN_NS = "market-scan-request", "shared.market_scan_requests"
OUT_SPACE, OUT_NS = "market-scan", "shared.market_scans"  # a DOCUMENT space

_PROCESSED: set[str] = set()  # per-run runaway guard (canon rule 5; output-dedup is primary)

CONTRACT = {
    "id": "market-scan",
    "spaces": [
        {"space": IN_SPACE, "namespace": IN_NS, "mode": "records",
         "schema": {"type": "object", "required": ["id", "segment", "status"],
                    "properties": {"id": {"type": "string"}, "segment": {"type": "string"},
                                   "area": {"type": "string"}, "our_offer": {"type": "string"},
                                   "queries": {"type": "array"}, "lang": {"type": "string"},
                                   "period_hours": {"type": "integer"}, "email": {"type": "boolean"},
                                   "last_run": {"type": "string"},
                                   "requested_by": {"type": "string"}, "result_ref": {"type": "string"},
                                   "error": {"type": "string"},
                                   "status": {"type": "string",
                                              "enum": ["requested", "active", "in-progress", "done", "failed"]}}}},
        {"space": OUT_SPACE, "namespace": OUT_NS, "mode": "document"},
    ],
}


def _call(tool_name: str, payload: dict):
    return _aimeat_call(AGENT, tool_name, payload)


def _build_queries(segment: str, area: str) -> list[str]:
    """Default query set from the parameters — overridable per request via `queries`."""
    a = f" {area}" if area else ""
    return [
        f"{segment}{a}",
        f"{segment}{a} hinnat hinnasto pricing",
        f"{segment}{a} yritykset palvelut",
        f"{segment} yritys liikevaihto henkilöstö",   # financials (finder/asiakastieto often rank)
        f"{segment}{a} toimisto yhteystiedot",        # offices + how to reach them = the sales motion
        f"{segment} news launch",
    ]


def _sweep(queries: list[str], lang: str) -> list[str]:
    """SearXNG (general + week-news) -> trafilatura -> up to 8 source docs."""
    from crewaimeat.fetch_pipeline import _searxng_urls
    docs: list[str] = []
    seen: set[str] = set()
    for q in queries:
        for time_range in ("", "month"):
            for u in _searxng_urls(q, lang, time_range, n=4):
                if u in seen or len(docs) >= 12:
                    continue
                seen.add(u)
                try:
                    txt = _trafilatura_text(u)
                except Exception:  # noqa: BLE001
                    txt = ""
                if txt and len(txt) > 400:
                    docs.append(f"[{u}]\n{txt[:2600]}")
            if len(docs) >= 12:
                break
    return docs


def run_market_scan(segment: str, area: str = "", our_offer: str = "",
                    queries: list[str] | None = None, lang: str = "fi") -> tuple[str | None, str]:
    """One parameterized scan -> (markdown analysis | None, error)."""
    qs = [q for q in (queries or []) if isinstance(q, str) and q.strip()] or _build_queries(segment, area)
    docs = _sweep(qs, lang)
    if not docs:
        return None, "no usable sources found for this segment/area (check SEARXNG_URL / queries)"
    offer_block = f"\nMEIDÄN TARJOOMA (positioi tätä vasten):\n{our_offer}\n" if our_offer else ""
    out_lang = "suomeksi" if lang == "fi" else "in English"
    prompt = (
        f"Olet tarkka markkina-analyytikko. Segmentti: {segment}."
        + (f" Alue: {area}." if area else "") + offer_block +
        "\n\nLÄHTEET:\n\n" + "\n\n".join(docs) +
        f"\n\nKirjoita {out_lang} markdown-analyysi TÄSMÄLLEEN näillä osioilla:\n"
        "## Ketkä täällä pelaavat\n(per toimija: kuka, mitä myy, MITEN MYY — inbound-yhteydenotto / "
        "self-serve / myyntitapaamiset / kumppanit, TOIMIPISTEET/sijainti, ja TALOUSLUVUT jos lähteissä "
        "näkyy: liikevaihto, henkilöstö, rahoitus; lähde-URL suluissa)\n\n"
        "## Tuotteet ja hintahaitarit\n(kategorisoi mitä tällä kentällä myydään — tuoteluokat markdown-"
        "taulukkona: | luokka | mitä se on | hintahaitari | kuka myy |. Vain lähteistä ilmenevät hinnat; "
        "merkitse 'ei julkista hintaa' jos ei näy)\n\n"
        "## Mitä ne mainostavat ja missä\n(viestit, kanavat, some-näkyvyys — vain lähteistä ilmenevä)\n\n"
        "## Miten me myydään tätä vasten\n(konkreettinen suositus: mihin aukkoon iskemme, MILLÄ HINNOILLA "
        "kannattaa myydä suhteessa kentän haitareihin, MITÄ tuotteistettua kannattaa kaupata ensin ja "
        "kenelle, ja millä myyntitavalla" + (" — peilaa meidän tarjoomaan ja sen hinnoitteluun" if our_offer else "") + ")\n\n"
        "## Puuttuvat kyvykkyydet\n(lista: jos lähteistä ilmenee ostajien odotuksia tai kilpailijoiden "
        "ominaisuuksia joita meidän tarjoomamme ei kata — | kyvykkyys | mitä se enabloisi bisneksessä | "
        "karkea toteutusarvio: päiviä / viikkoja / kuukausia |. Arvio on sinun harkintaasi — merkitse se arvioksi)\n\n"
        "## Signaalit ja seuraavat askeleet\n(2-4 bullettia: tärkeimmät signaalit + mitä kannattaa tehdä seuraavaksi)\n\n"
        "Käytä VAIN lähteiden faktoja toimijoista ja hinnoista; jokainen toimijaväite saa lähde-URLin. "
        "Älä keksi toimijoita, hintoja tai talouslukuja — sano suoraan jos tieto ei ilmene lähteistä.\n\n"
        "AIVAN LOPUKSI listaa jokainen analyysissä mainittu YRITYS (ei tuote/alusta — vain yritykset) "
        "omalla rivillään täsmälleen muodossa:\nCOMPANY: <yrityksen nimi>"
    )
    llm = get_llm(for_tool_use=False, temperature=0.3, agent_name="research-contract")
    md = (llm.call([{"role": "user", "content": prompt}]) or "").strip()
    return (md or None), ("" if md else "analyst returned empty output")


_COMPANY_LINE = __import__("re").compile(r"^COMPANY:\s*(.+?)\s*$", __import__("re").MULTILINE)


def _split_companies(md: str) -> tuple[str, list[str]]:
    """Strip the machine-readable COMPANY: lines from the analysis -> (clean_md, company names)."""
    companies = [m.strip() for m in _COMPANY_LINE.findall(md) if m.strip()]
    clean = _COMPANY_LINE.sub("", md).rstrip()
    return clean, list(dict.fromkeys(companies))  # dedup, keep order


def _spawn_company_requests(oid: str, wid: str, companies: list[str], scan_rid: str) -> int:
    """Queue a company-research-request for every NEW company a scan named (the chain)."""
    from crewaimeat.company_contract import IN_NS as CO_NS, IN_SPACE as CO_SPACE, slugify
    data = _call("aimeat_workspace_read", {"organism_id": oid, "ws": wid}) or {}
    if not any(t.get("name") == CO_SPACE for t in ((data.get("manifest") or {}).get("objectTypes") or [])):
        return 0  # this workspace hasn't adopted the company-research contract — skip quietly
    existing = {r.get("id") for r in (data.get("objects", {}) or {}).get(CO_SPACE, [])}
    spawned = 0
    for name in companies[:10]:
        rid = f"cr-{slugify(name)}"
        if rid in existing:
            continue
        rec = {"id": rid, "company": name, "requested_by": f"market-scan/{scan_rid}", "status": "requested"}
        if _call("aimeat_workspace_write", {"organism_id": oid, "ws": wid, "space": CO_SPACE, "id": rid, "value": rec}):
            _call("aimeat_workspace_publish", {"organism_id": oid, "ws": wid, "namespace": CO_NS, "id": rid})
            spawned += 1
            existing.add(rid)
    return spawned


def _advance(oid: str, wid: str, req: dict, **changes) -> None:
    rec = {k: v for k, v in {**req, **changes}.items() if not k.startswith("_")}
    if _call("aimeat_workspace_write", {"organism_id": oid, "ws": wid, "space": IN_SPACE, "id": rec["id"], "value": rec}):
        _call("aimeat_workspace_publish", {"organism_id": oid, "ws": wid, "namespace": IN_NS, "id": rec["id"]})


def _mail_out(oid: str, wid: str, out_id: str, title: str, md: str) -> None:
    """Write the finished scan as a mail-request record — postman's contract then sends it."""
    mid = f"mail-{out_id}"
    rec = {"id": mid, "subject": title, "body_md": md[:6000],
           "requested_by": "market-scan", "status": "requested"}
    if _call("aimeat_workspace_write", {"organism_id": oid, "ws": wid, "space": "mail-request",
                                        "id": mid, "value": rec}):
        _call("aimeat_workspace_publish", {"organism_id": oid, "ws": wid,
                                           "namespace": "shared.mail_requests", "id": mid})


def process_market_scans(max_items: int = 2, targets: list[tuple[str, str]] | None = None) -> dict:
    """Fulfil DUE `market-scan-request` records (one-shot 'requested' + recurring 'active')."""
    pairs = targets if targets is not None else member_workspaces(AGENT)
    now = datetime.datetime.now(datetime.timezone.utc)
    today = datetime.date.today().isoformat()
    processed = failed = 0
    for oid, wid in pairs:
        if processed + failed >= max_items:
            break
        data = _call("aimeat_workspace_read", {"organism_id": oid, "ws": wid})
        if not data or data.get("manifest") is None:
            continue
        reqs = (data.get("objects", {}) or {}).get(IN_SPACE) or []
        done_out = {r.get("id") for r in ((data.get("objects", {}) or {}).get(OUT_SPACE) or [])}
        for req in reqs:
            rid, status = req.get("id"), req.get("status")
            if not rid:
                continue
            recurring = status == "active"
            due = status == "requested"
            if recurring:
                last = req.get("last_run")
                period_h = int(req.get("period_hours") or 168)
                if not last:
                    due = True  # active but never run -> first edition now
                else:
                    try:
                        due = (now - datetime.datetime.fromisoformat(last)).total_seconds() >= period_h * 3600
                    except Exception:  # noqa: BLE001
                        due = True
            if due:
                # Durable per-machine guard: a stale/frozen workspace read after a daemon
                # restart made every scan look due again (6 mails in a day). The marker is
                # this machine's own truth about what it already ran.
                from crewaimeat.local_marks import last_local_run, ran_within
                if recurring and ran_within("market_scan", rid, max(1.0, period_h * 0.9)):
                    due = False
                elif not recurring and last_local_run("market_scan", rid) is not None:
                    _PROCESSED.add(rid)
                    continue
            if not due or rid in _PROCESSED:
                continue
            out_id = f"scan-{rid}-{today}" if recurring else f"scan-{rid}"
            if out_id in done_out:  # output-dedup -> settle / skip without re-running
                _PROCESSED.add(rid)
                if not recurring:
                    _advance(oid, wid, req, status="done", result_ref=out_id)
                continue
            if processed + failed >= max_items:
                break
            _PROCESSED.add(rid)
            _advance(oid, wid, req, status="in-progress")
            md, err = run_market_scan(req.get("segment", ""), req.get("area", ""),
                                      req.get("our_offer", ""), req.get("queries"),
                                      req.get("lang") or "fi")
            if not md:
                _advance(oid, wid, req, status="failed" if not recurring else "active",
                         error=err[:300], **({"last_run": now.isoformat()} if recurring else {}))
                failed += 1
                print(f"[{AGENT}] market-scan FAILED for {rid}: {err}", file=sys.stderr)
                continue
            md, companies = _split_companies(md)
            title = f"Market scan · {req.get('segment','')[:50]}" + (f" · {req.get('area','')[:30]}" if req.get("area") else "")
            footer = f"\n\n*Scan: {req.get('segment','')} · {req.get('area','') or '-'} · {today} · sources via SearXNG*"
            wrote = _call("aimeat_workspace_write",
                          {"organism_id": oid, "ws": wid, "space": OUT_SPACE, "id": out_id,
                           "value": {"title": title + (f" · {today}" if recurring else ""), "markdown": md + footer}})
            pub = _call("aimeat_workspace_publish",
                        {"organism_id": oid, "ws": wid, "namespace": OUT_NS, "id": out_id}) if wrote else None
            if wrote and pub:
                if companies:  # the chain: every named company -> a company-research-request
                    n = _spawn_company_requests(oid, wid, companies, rid)
                    if n:
                        print(f"[{AGENT}] scan {rid} queued {n} company-research request(s)", file=sys.stderr)
                if req.get("email"):
                    _mail_out(oid, wid, out_id, title + f" · {today}", md + footer)
                _advance(oid, wid, req,
                         status="active" if recurring else "done",
                         result_ref=out_id, **({"last_run": now.isoformat(), "error": ""} if recurring else {}))
                from crewaimeat.local_marks import mark_local_run
                mark_local_run("market_scan", rid)
                processed += 1
            else:
                _advance(oid, wid, req, status="failed" if not recurring else "active",
                         error="scan write failed", **({"last_run": now.isoformat()} if recurring else {}))
                failed += 1
    return {"processed": processed, "failed": failed}


def make_market_tools(agent_name: str) -> list:
    """The contract-processing tool: fulfil market-scan-requests; never contacts anyone external."""

    @tool("process_market_scans")
    def _process(max_items: int = 2) -> str:
        """Fulfil pending `market-scan-request` records: build queries from the segment/area
        parameters, sweep the web (SearXNG + fetch), distill a source-cited competitor/market
        analysis positioned against our offer, and write a `market-scan` document. Deterministic."""
        res = process_market_scans(max_items=max_items)
        return f"market-scan: processed {res['processed']} request(s), {res['failed']} failed."

    return [_process]

"""company-research: a DETERMINISTIC Finnish-company financials contract.

Digs one company's facts and figures from the open sources — the PRH/YTJ open-data API
(business id, legal form, industry, registration; free JSON, no scraping) plus finder.fi /
asiakastieto.fi-style pages found via SearXNG — and writes a markdown company profile with a
financials table and a unicode revenue-trend chart into a document space.

Contract:
  inputs : `company-research-request` (records) — trigger: status == 'requested'
             { id, company(required), business_id?, status, requested_by?, result_ref?, error? }
  outputs: `company-research` (DOCUMENT) — the profile page (id = co-<request id>)
  lifecycle: requested -> in-progress -> done (+result_ref) | failed (+error)

Chained from market-scan: every company a scan names is auto-queued here as a request
(market_contract._spawn_company_requests), so the library grows by itself. Served by
web-researcher (its third contract). Grounding: figures come ONLY from the sources; the
profile says plainly when a number is not public.
"""

from __future__ import annotations

import datetime
import re
import sys

import requests
from crewai.tools import tool

from crewaimeat.aimeat_crew import _aimeat_call, member_workspaces
from crewaimeat.article_extract import _trafilatura_text
from crewaimeat.llm import get_llm

AGENT = "web-researcher"
IN_SPACE, IN_NS = "company-research-request", "shared.company_research_requests"
OUT_SPACE, OUT_NS = "company-research", "shared.company_research"  # a DOCUMENT space

_PROCESSED: set[str] = set()  # per-run runaway guard (canon rule 5)

CONTRACT = {
    "id": "company-research",
    "spaces": [
        {"space": IN_SPACE, "namespace": IN_NS, "mode": "records",
         "schema": {"type": "object", "required": ["id", "company", "status"],
                    "properties": {"id": {"type": "string"}, "company": {"type": "string"},
                                   "business_id": {"type": "string"}, "requested_by": {"type": "string"},
                                   "result_ref": {"type": "string"}, "error": {"type": "string"},
                                   "status": {"type": "string",
                                              "enum": ["requested", "in-progress", "done", "failed"]}}}},
        {"space": OUT_SPACE, "namespace": OUT_NS, "mode": "document"},
    ],
}


def _call(tool_name: str, payload: dict):
    return _aimeat_call(AGENT, tool_name, payload)


def _ytj_lookup(company: str, business_id: str = "") -> dict | None:
    """PRH/YTJ open-data API: registry facts as a dict (best name match), or None."""
    try:
        params = {"businessId": business_id} if business_id else {"name": company, "maxResults": 10}
        r = requests.get("https://avoindata.prh.fi/opendata-ytj-api/v3/companies",
                         params=params, timeout=20)
        companies = (r.json() or {}).get("companies") or []
        if not companies:
            return None
        want = company.lower()
        best = next((c for c in companies
                     for n in (c.get("names") or [])
                     if want in (n.get("name") or "").lower()), companies[0])
        names = [n.get("name") for n in (best.get("names") or []) if n.get("name")]
        line = next((d.get("description") for d in ((best.get("mainBusinessLine") or {}).get("descriptions") or [])
                     if d.get("languageCode") == "1"), None)
        form = next((d.get("description") for f in (best.get("companyForms") or [])
                     for d in (f.get("descriptions") or []) if d.get("languageCode") == "1"), None)
        addr = ""
        for a in (best.get("addresses") or []):
            city = a.get("postOffices") or []
            city_name = next((p.get("city") for p in city if p.get("languageCode") == "1"), "") if city else ""
            addr = ", ".join(x for x in (a.get("street") or "", a.get("postCode") or "", city_name) if x)
            if addr:
                break
        return {"business_id": (best.get("businessId") or {}).get("value"),
                "registered": (best.get("businessId") or {}).get("registrationDate"),
                "name": names[0] if names else company, "industry": line, "form": form,
                "address": addr}
    except Exception as exc:  # noqa: BLE001
        print(f"[{AGENT}] YTJ lookup failed for {company}: {exc!r}", file=sys.stderr)
        return None


def _xbrl_financials(business_id: str) -> str:
    """PRH's official digital financial statements API (iXBRL filings).

    GET /opendata-xbrl-api/v3/financials?businessId= — profit/loss + balance sheet of DIGITALLY
    filed statements. Coverage is still thin (voluntary iXBRL filing), so this is the first,
    authoritative source when present and silently absent otherwise; finder-style pages remain
    the fallback. Returns a compact JSON string for the analyst, or ''."""
    import json as _json
    try:
        r = requests.get("https://avoindata.prh.fi/opendata-xbrl-api/v3/financials",
                         params={"businessId": business_id}, timeout=20)
        d = r.json() or {}
        if d.get("totalResults"):
            return _json.dumps(d.get("financials") or [], ensure_ascii=False)[:4000]
    except Exception as exc:  # noqa: BLE001
        print(f"[{AGENT}] XBRL lookup failed for {business_id}: {exc!r}", file=sys.stderr)
    return ""


def _finder_vision(company: str) -> str:
    """Read the company's finder.fi page the way a human does: Playwright opens it, takes a
    screenshot, and the vision model (qwen-vl) reads the JS-rendered revenue/profit bar charts
    that text extraction can't see. Returns a '[url — kuvaluenta]\\n<facts>' block, or ''."""
    import os
    import tempfile

    from crewaimeat.browser_tool import _describe_image
    from crewaimeat.fetch_pipeline import _searxng_urls
    urls = [u for u in _searxng_urls(f"site:finder.fi {company}", "fi", "", n=6) if "finder.fi" in u]
    if not urls:
        return ""
    url = urls[0]
    path = os.path.join(tempfile.gettempdir(), f"finder_{slugify(company)}.png")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1400, "height": 2400})
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
            for sel in ("button:has-text('Hyväksy')", "button:has-text('Salli kaikki')",
                        "button:has-text('OK')"):
                try:
                    page.locator(sel).first.click(timeout=2000)
                    break
                except Exception:  # noqa: BLE001 — no consent dialog is fine
                    pass
            page.wait_for_timeout(3000)  # let the chart JS render
            page.screenshot(path=path)
            browser.close()
    except Exception as exc:  # noqa: BLE001 — vision leg is best-effort; sources still work
        print(f"[{AGENT}] finder screenshot failed for {company}: {exc!r}", file=sys.stderr)
        return ""
    desc = _describe_image(path, (
        "Tämä on finder.fi-yrityssivu. Lue ja listaa TARKASTI pelkkinä riveinä:\n"
        "- yrityksen nimi, y-tunnus, toimiala, osoite\n"
        "- 'Liikevaihto'-pylväskaavio (tuhatta euroa): jokainen vuosi ja arvo\n"
        "- 'Tilikauden tulos' -pylväskaavio (tuhatta euroa): jokainen vuosi ja arvo\n"
        "- henkilöstömäärä jos näkyy\n"
        "Jos jokin arvo ei ole luettavissa, sano se suoraan. Älä arvaa lukuja."))
    if desc.startswith("(describe failed"):
        return ""
    return f"[{url} — finder-sivun kuvaluenta (vision)]\n{desc}"


def _financial_sources(company: str) -> list[str]:
    """finder/asiakastieto-style pages for the company via SearXNG + trafilatura (up to 5)."""
    from crewaimeat.fetch_pipeline import _searxng_urls
    docs: list[str] = []
    seen: set[str] = set()
    for q in (f"site:finder.fi {company}", f"{company} liikevaihto finder",
              f"{company} taloustiedot asiakastieto",
              f"{company} liikevaihto tulos henkilöstö"):
        for u in _searxng_urls(q, "fi", "", n=4):
            if u in seen or len(docs) >= 5:
                continue
            seen.add(u)
            try:
                txt = _trafilatura_text(u)
            except Exception:  # noqa: BLE001
                txt = ""
            if txt and len(txt) > 300:
                docs.append(f"[{u}]\n{txt[:2500]}")
        if len(docs) >= 5:
            break
    return docs


def run_company_research(company: str, business_id: str = "") -> tuple[str | None, str]:
    """One company profile -> (markdown | None, error)."""
    ytj = _ytj_lookup(company, business_id)
    docs = _financial_sources(company)
    if not ytj and not docs:
        return None, "neither the YTJ registry nor any financial source found this company"
    facts = ""
    if ytj:
        facts = ("REKISTERIFAKTAT (PRH/YTJ avoin data — nämä ovat varmoja):\n"
                 + "\n".join(f"- {k}: {v}" for k, v in ytj.items() if v) + "\n\n")
        xbrl = _xbrl_financials(ytj.get("business_id") or business_id or "")
        if xbrl:
            facts += ("VIRALLISET DIGITAALISET TILINPÄÄTÖSTIEDOT (PRH XBRL — ensisijainen lähde "
                      "talousluvuille):\n" + xbrl + "\n\n")
    vision = _finder_vision(company)
    if vision:
        facts += "FINDER-SIVU KUVASTA LUETTUNA (vision-malli; chartit joita tekstihaku ei näe):\n" + vision + "\n\n"
    prompt = (
        f"Olet yritystutkija. Kohde: {company}.\n\n" + facts +
        ("LÄHTEET (talousluvut VAIN näistä):\n\n" + "\n\n".join(docs) + "\n\n" if docs else
         "(Talouslähteitä ei löytynyt — kirjoita profiili rekisterifaktoista ja sano se suoraan.)\n\n") +
        "Kirjoita suomeksi markdown-yritysprofiili TÄSMÄLLEEN näillä osioilla:\n"
        "## Perustiedot\n(virallinen nimi, y-tunnus, yhtiömuoto, toimiala, kotipaikka/osoite, perustettu — "
        "rekisterifaktoista + lähteistä)\n\n"
        "## Talousluvut\n(markdown-taulukko vuosittain: | vuosi | liikevaihto | tulos | henkilöstö | — VAIN "
        "lähteissä näkyvät vuodet ja luvut; jos mitään ei näy, kirjoita 'Talouslukuja ei julkisissa lähteissä.')\n\n"
        "## Liikevaihdon kehitys\n(unicode-pylväskaavio koodiblokissa, esim:\n"
        "```\n2022 ███████░░░ 1.2 M€\n2023 ██████████ 1.7 M€\n```\n"
        "— skaalaa pisin pylväs 10 merkkiin; tee VAIN jos taulukossa on vähintään 2 vuotta liikevaihtoa)\n\n"
        "## Mitä se myy ja kenelle\n(2-4 lausetta lähteistä)\n\n"
        "## Lähteet\n(URL-lista)\n\n"
        "Älä keksi yhtään lukua. Jokainen talousluku on oltava jäljitettävissä lähteeseen."
    )
    llm = get_llm(for_tool_use=False, temperature=0.2, agent_name="research-contract")
    md = (llm.call([{"role": "user", "content": prompt}]) or "").strip()
    return (md or None), ("" if md else "analyst returned empty output")


def _advance(oid: str, wid: str, req: dict, **changes) -> None:
    rec = {k: v for k, v in {**req, **changes}.items() if not k.startswith("_")}
    if _call("aimeat_workspace_write", {"organism_id": oid, "ws": wid, "space": IN_SPACE, "id": rec["id"], "value": rec}):
        _call("aimeat_workspace_publish", {"organism_id": oid, "ws": wid, "namespace": IN_NS, "id": rec["id"]})


def process_company_research(max_items: int = 3, targets: list[tuple[str, str]] | None = None) -> dict:
    """Fulfil pending `company-research-request` records across the agent's member workspaces."""
    pairs = targets if targets is not None else member_workspaces(AGENT)
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
            rid = req.get("id")
            if req.get("status") != "requested" or not rid:
                continue
            if rid in _PROCESSED:
                continue
            if f"co-{rid}" in done_out:  # output-dedup -> settle
                _PROCESSED.add(rid)
                _advance(oid, wid, req, status="done", result_ref=f"co-{rid}")
                continue
            if processed + failed >= max_items:
                break
            _PROCESSED.add(rid)
            _advance(oid, wid, req, status="in-progress")
            md, err = run_company_research(req.get("company", ""), req.get("business_id", ""))
            if not md:
                _advance(oid, wid, req, status="failed", error=err[:300])
                failed += 1
                print(f"[{AGENT}] company-research FAILED for {rid}: {err}", file=sys.stderr)
                continue
            out_id = f"co-{rid}"
            footer = f"\n\n*Company research: {req.get('company','')} · {today} · PRH/YTJ open data + web sources*"
            wrote = _call("aimeat_workspace_write",
                          {"organism_id": oid, "ws": wid, "space": OUT_SPACE, "id": out_id,
                           "value": {"title": f"Company · {req.get('company','')[:70]}", "markdown": md + footer}})
            pub = _call("aimeat_workspace_publish",
                        {"organism_id": oid, "ws": wid, "namespace": OUT_NS, "id": out_id}) if wrote else None
            if wrote and pub:
                _advance(oid, wid, req, status="done", result_ref=out_id)
                processed += 1
            else:
                _advance(oid, wid, req, status="failed", error="profile write failed")
                failed += 1
    return {"processed": processed, "failed": failed}


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]


def make_company_tools(agent_name: str) -> list:
    """The contract-processing tool: fulfil company-research-requests."""

    @tool("process_company_research")
    def _process(max_items: int = 3) -> str:
        """Fulfil pending `company-research-request` records: look the company up in the PRH/YTJ
        open-data registry, gather finder/asiakastieto-style financial pages, and write a markdown
        company profile (facts, financials table, revenue trend chart, sources). Deterministic."""
        res = process_company_research(max_items=max_items)
        return f"company-research: processed {res['processed']} request(s), {res['failed']} failed."

    return [_process]

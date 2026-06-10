"""market-scan: a PARAMETERIZED competitor/market analysis as a workspace contract.

The generalization of the morning report's competitor watch: point the same machinery at ANY
segment + area ("AI agent platforms, Espoo/Helsinki" today; "parturi-kampaamot, Leppävaara"
tomorrow) and get the same source-cited analysis: who the players are, what they sell and at what
price, how WE could sell against them (positioned against `our_offer`), and where they are visible
(social/channels).

Contract:
  inputs : `market-scan-request` (records) — trigger: status == 'requested'
             { id, segment(required), area?, our_offer?, queries?(list overrides the generated
               ones), lang?('fi'|'en', default fi), status, requested_by?, result_ref?, error? }
  outputs: `market-scan` (DOCUMENT) — the analysis as a page (id = scan-<request id>)
  lifecycle: requested -> in-progress -> done (+result_ref) | failed (+error)

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
                                   "requested_by": {"type": "string"}, "result_ref": {"type": "string"},
                                   "error": {"type": "string"},
                                   "status": {"type": "string",
                                              "enum": ["requested", "in-progress", "done", "failed"]}}}},
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
        f"{segment}{a} hinnat pricing",
        f"{segment}{a} yritykset palvelut",
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
                if u in seen or len(docs) >= 8:
                    continue
                seen.add(u)
                try:
                    txt = _trafilatura_text(u)
                except Exception:  # noqa: BLE001
                    txt = ""
                if txt and len(txt) > 400:
                    docs.append(f"[{u}]\n{txt[:2800]}")
            if len(docs) >= 8:
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
        "## Ketkä täällä pelaavat\n(per toimija: kuka, mitä myy, hinnoittelu jos näkyvissä; lähde-URL suluissa)\n\n"
        "## Mitä ne mainostavat ja missä\n(viestit, kanavat, some-näkyvyys — vain lähteistä ilmenevä)\n\n"
        "## Miten me myydään tätä vasten\n(3-5 konkreettista myyntikulmaa: mihin aukkoon iskemme, "
        "mitä sanomme eri tavalla" + (", suhteessa meidän tarjoomaan" if our_offer else "") + ")\n\n"
        "## Signaalit ja seuraavat askeleet\n(2-4 bullettia: tärkeimmät signaalit + mitä kannattaa tehdä seuraavaksi)\n\n"
        "Käytä VAIN lähteiden faktoja; jokainen toimijaväite saa lähde-URLin. Älä keksi toimijoita tai hintoja."
    )
    llm = get_llm(for_tool_use=False, temperature=0.3, agent_name="research-contract")
    md = (llm.call([{"role": "user", "content": prompt}]) or "").strip()
    return (md or None), ("" if md else "analyst returned empty output")


def _advance(oid: str, wid: str, req: dict, **changes) -> None:
    rec = {k: v for k, v in {**req, **changes}.items() if not k.startswith("_")}
    if _call("aimeat_workspace_write", {"organism_id": oid, "ws": wid, "space": IN_SPACE, "id": rec["id"], "value": rec}):
        _call("aimeat_workspace_publish", {"organism_id": oid, "ws": wid, "namespace": IN_NS, "id": rec["id"]})


def process_market_scans(max_items: int = 2, targets: list[tuple[str, str]] | None = None) -> dict:
    """Fulfil pending `market-scan-request` records across the agent's member workspaces."""
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
            if f"scan-{rid}" in done_out:  # output-dedup -> settle without re-running
                _PROCESSED.add(rid)
                _advance(oid, wid, req, status="done", result_ref=f"scan-{rid}")
                continue
            if processed + failed >= max_items:
                break
            _PROCESSED.add(rid)
            _advance(oid, wid, req, status="in-progress")
            md, err = run_market_scan(req.get("segment", ""), req.get("area", ""),
                                      req.get("our_offer", ""), req.get("queries"),
                                      req.get("lang") or "fi")
            if not md:
                _advance(oid, wid, req, status="failed", error=err[:300])
                failed += 1
                print(f"[{AGENT}] market-scan FAILED for {rid}: {err}", file=sys.stderr)
                continue
            out_id = f"scan-{rid}"
            title = f"Market scan · {req.get('segment','')[:50]}" + (f" · {req.get('area','')[:30]}" if req.get("area") else "")
            footer = f"\n\n*Scan: {req.get('segment','')} · {req.get('area','') or '-'} · {today} · sources via SearXNG*"
            wrote = _call("aimeat_workspace_write",
                          {"organism_id": oid, "ws": wid, "space": OUT_SPACE, "id": out_id,
                           "value": {"title": title, "markdown": md + footer}})
            pub = _call("aimeat_workspace_publish",
                        {"organism_id": oid, "ws": wid, "namespace": OUT_NS, "id": out_id}) if wrote else None
            if wrote and pub:
                _advance(oid, wid, req, status="done", result_ref=out_id)
                processed += 1
            else:
                _advance(oid, wid, req, status="failed", error="scan write failed")
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

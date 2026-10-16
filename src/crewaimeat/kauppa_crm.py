"""kauppa-crm: a paid sale in the AIMEAT store becomes a CADENCE contact and a won deal, ONCE.

GTM phase 4. The store (store.aimeat.io, aimeat-commercial's ext:shop) is a separate node from
aimeat.io, where CADENCE lives. The store never calls out: this agent PULLS its sales feed
(`POST /v1/ext/shop/crmFeed`, read-only, aimeat-commercial shop/README.md "Sales into CADENCE") and
writes, per sale:

  contact  `kauppa-<email slug>`, tila `asiakas`, lahde `store.aimeat.io` -- unless CADENCE already
           has a contact with that email, which is then referenced and left exactly as it is;
  deal     `kauppa-<sale id>` (the store's order id), tila `won`, vaihe `voitettu`, arvo without VAT.

WRITTEN ONCE. A deal id already published in CADENCE is never written again, so a person may edit
the deal or the contact afterwards and the next pass leaves both alone. A deal that stayed a draft
(a publish that failed) is written and published on the next pass.

ONLY PAID. The feed lists a sale once its payment has arrived ("Paikka on myyty kun maksu on
saapunut", the Founding programme). A TEST sale (Stripe test mode, a TEST- invoice) is left out
unless KAUPPA_CRM_INCLUDE_TEST=1. A sale refunded before it got here is written as a lost deal.

NO ANONYMOUS DOOR. Nothing here listens: CADENCE is written by this agent's own identity on aimeat.io
(organism:read, memory:write, organism:write), and the store is read with two credentials of the
store node, both in this machine's environment and nowhere else:
  KAUPPA_STORE_PAT        a personal access token of a store account made for this (ext:write, ext:invoke)
  KAUPPA_CRM_FEED_TOKEN   the value of ext:shop's secret CRM_FEED_TOKEN
Optional: KAUPPA_STORE_URL (https://store.aimeat.io), KAUPPA_CRM_ORG / KAUPPA_CRM_WS (CADENCE's
organism and workspace on aimeat.io), KAUPPA_CRM_OMISTAJA (whose deals they are).

Deterministic, no model. Every node call goes through `workspace_tools` (`_aimeat_call`).
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request

from crewaimeat import workspace_tools

AGENT = "kauppa-crm"
STORE_URL = "https://store.aimeat.io"
# CADENCE on aimeat.io: organism "CADENCE" (b9c552d1-...), workspace "CRM".
CADENCE_ORG = "b9c552d1-6a73-40f7-9d2c-91172c7f1332"
CADENCE_WS = "ws-mrkj9sbei68"
OMISTAJA = "happydude500001@aimeat-finland-001-genesis"
SOURCE = "store.aimeat.io"
TIMEOUT = 30


class FeedError(RuntimeError):
    """The store did not hand over its sales. The message is the store's own words."""


# ── pure: a sale as CADENCE records ─────────────────────────────────────────────────────────────
def email_slug(email: str) -> str:
    """`Kaisa.Koe@Acme.fi` -> `kaisa-koe-acme-fi`: the contact id's tail, stable for one address."""
    return re.sub(r"[^a-z0-9]+", "-", str(email or "").strip().lower()).strip("-")[:80]


def contact_id_for(email: str) -> str:
    return "kauppa-" + email_slug(email)


def deal_id_for(sale: dict) -> str:
    sid = str(sale.get("saleId") or sale.get("order") or "")
    return "kauppa-" + re.sub(r"[^A-Za-z0-9_-]+", "-", sid).strip("-")


def _day(iso: str) -> str:
    return str(iso or "")[:10]


def _company(sale: dict) -> str:
    return str((sale.get("buyer") or {}).get("company") or "").strip()


def contact_record(sale: dict, contact_id: str, omistaja: str) -> dict:
    """A new customer as CADENCE's locked contact schema takes it (only its keys, no empty ones)."""
    buyer = sale.get("buyer") or {}
    first, _, last = str(buyer.get("name") or "").strip().partition(" ")
    tags = ["kauppa", "founding" if sale.get("founding") else str(sale.get("tier") or sale.get("kind") or "")]
    note = f"Osti kaupasta {sale.get('product') or 'paikan'} {_day(sale.get('paidAt'))}"
    if sale.get("order"):
        note += f" (tilaus {sale['order']})"
    rec = {
        "id": contact_id,
        "etunimi": first,
        "sukunimi": last.strip(),
        "email": str(buyer.get("email") or "").strip().lower(),
        "yritys_ref": _company(sale),
        "omistaja": omistaja,
        "lahde": SOURCE,
        "tila": "asiakas",
        "tagit": [t for t in tags if t],
        "muistiinpano": note + ".",
        "viimeisin_kosketus": str(sale.get("paidAt") or ""),
        "luotu": str(sale.get("paidAt") or ""),
    }
    return {k: v for k, v in rec.items() if v not in ("", None, [])}


def deal_record(sale: dict, kontakti_ref: str | None, omistaja: str) -> dict:
    """The sale as a closed deal: won, or lost when it was refunded before it got here."""
    buyer = sale.get("buyer") or {}
    who = _company(sale) or str(buyer.get("name") or "").strip()
    title = f"Kauppa: {sale.get('product') or 'AIMEAT'}"
    if sale.get("foundingNumber"):
        title += f" #{sale['foundingNumber']}"
    if sale.get("period"):
        title += f", {sale['period']}"
    if who:
        title += f", {who}"
    lost = bool(sale.get("refunded"))
    rec = {
        "id": deal_id_for(sale),
        "otsikko": title,
        "arvo": sale.get("amountEur") if isinstance(sale.get("amountEur"), (int, float)) else None,
        "valuutta": str(sale.get("currency") or "EUR"),
        "vaihe": "hävitty" if lost else "voitettu",
        "tila": "lost" if lost else "won",
        "todennakoisyys": 0 if lost else 100,
        "kontakti_ref": kontakti_ref,
        "yritys_ref": _company(sale),
        "odotettu_klousaus": _day(sale.get("paidAt")),
        "omistaja": omistaja,
        "luotu": str(sale.get("paidAt") or ""),
    }
    return {k: v for k, v in rec.items() if v not in ("", None)}


def plan_sales(sales, published_deals, contacts_by_email, include_test=False, omistaja=OMISTAJA):
    """What to write: [{"contact": record|None, "kontakti_ref": id|None, "deal": record}], and the
    sales left out by reason. Pure, so the whole decision is testable without a node."""
    actions, skipped = [], {}
    by_email = dict(contacts_by_email)

    def skip(why):
        skipped[why] = skipped.get(why, 0) + 1

    for sale in sales or []:
        if not isinstance(sale, dict) or not (sale.get("saleId") or sale.get("order")):
            skip("not a sale")
            continue
        if sale.get("test") and not include_test:
            skip("test sale")
            continue
        if deal_id_for(sale) in published_deals:
            skip("already in CADENCE")
            continue
        email = str((sale.get("buyer") or {}).get("email") or "").strip().lower()
        contact, ref = None, None
        if email:
            ref = by_email.get(email)
            if ref is None:
                ref = contact_id_for(email)
                contact = contact_record(sale, ref, omistaja)
                by_email[email] = ref
        actions.append({"contact": contact, "kontakti_ref": ref, "deal": deal_record(sale, ref, omistaja)})
    return actions, skipped


# ── the store ──────────────────────────────────────────────────────────────────────────────────
def fetch_sales(store_url: str, pat: str, token: str, since: str | None = None) -> list[dict]:
    """The store's paid sales. Raises FeedError with the store's words when it refuses."""
    body = {"token": token}
    if since:
        body["since"] = since
    req = urllib.request.Request(
        store_url.rstrip("/") + "/v1/ext/shop/crmFeed",
        method="POST",
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json", "Authorization": "Bearer " + pat},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as res:
            parsed = json.loads(res.read().decode() or "{}")
    except urllib.error.HTTPError as err:
        raise FeedError(f"the store answered HTTP {err.code}: {err.read().decode(errors='replace')[:200]}") from None
    except (urllib.error.URLError, TimeoutError, ValueError) as err:
        raise FeedError(f"the store could not be read: {err}") from None
    data = parsed.get("data", parsed) if isinstance(parsed, dict) else {}
    if not isinstance(data, dict) or data.get("error") or not isinstance(data.get("sales"), list):
        raise FeedError(f"the store refused the feed: {(data or {}).get('error') or str(data)[:200]}")
    return data["sales"]


# ── one pass ───────────────────────────────────────────────────────────────────────────────────
def sync_store_sales(agent_name: str = AGENT, *, env=None, fetch=None) -> dict:
    """Read the store's sales and CADENCE, write what is missing. Returns a report; never raises
    for a refusal, because a scheduled run has nobody to catch it: the report says what happened."""
    env = os.environ if env is None else env
    pat, token = env.get("KAUPPA_STORE_PAT", ""), env.get("KAUPPA_CRM_FEED_TOKEN", "")
    missing = [k for k, v in (("KAUPPA_STORE_PAT", pat), ("KAUPPA_CRM_FEED_TOKEN", token)) if not v]
    if missing:
        return {"error": "not set up: " + ", ".join(missing) + " missing from this machine's environment"}
    org, ws = env.get("KAUPPA_CRM_ORG") or CADENCE_ORG, env.get("KAUPPA_CRM_WS") or CADENCE_WS
    omistaja = env.get("KAUPPA_CRM_OMISTAJA") or OMISTAJA
    include_test = env.get("KAUPPA_CRM_INCLUDE_TEST") == "1"

    try:
        sales = (fetch or fetch_sales)(env.get("KAUPPA_STORE_URL") or STORE_URL, pat, token)
    except FeedError as err:
        return {"error": str(err)}

    def read(tool, payload):
        return workspace_tools._aimeat_call(agent_name, tool, payload, return_error=True)

    data = workspace_tools.read_workspace_objects(read, org, ws, spaces=["contact"])
    if not isinstance(data, dict) or not isinstance(data.get("objects"), dict):
        why = (data or {}).get("error") if isinstance(data, dict) else None
        return {"error": f"CADENCE could not be read ({why or 'no answer'}); nothing was written", "sales": len(sales)}
    index = data.get("index") or {}
    published_deals = {str(r.get("id")) for r in index.get("deal") or [] if isinstance(r, dict) and r.get("published")}
    contacts_by_email = {}
    for c in data["objects"].get("contact") or []:
        email = str(c.get("email") or "").strip().lower()
        if email and email not in contacts_by_email:
            contacts_by_email[email] = str(c.get("id"))

    actions, skipped = plan_sales(sales, published_deals, contacts_by_email, include_test, omistaja)
    report = {"sales": len(sales), "contacts": [], "deals": [], "skipped": skipped, "failed": []}
    for act in actions:
        try:
            if act["contact"]:
                workspace_tools.write_record(agent_name, org, ws, "contact", act["contact"], act["contact"]["id"])
                report["contacts"].append(act["contact"]["id"])
            workspace_tools.write_record(agent_name, org, ws, "deal", act["deal"], act["deal"]["id"])
            report["deals"].append(act["deal"]["id"])
        except workspace_tools.WorkspaceWriteError as err:
            # One sale that does not land does not stop the others; it is written on the next pass.
            report["failed"].append({"deal": act["deal"]["id"], "error": str(err)[:300]})
            print(f"[{agent_name}] {act['deal']['id']} not written: {err}", file=sys.stderr)
    return report


def describe(report: dict) -> str:
    """The report in one line, for the task the schedule made."""
    if report.get("error"):
        return f"kauppa-crm: {report['error']}"
    skipped = ", ".join(f"{n} {why}" for why, n in sorted(report["skipped"].items())) or "none"
    line = (
        f"kauppa-crm: {report['sales']} sale(s) in the store; wrote {len(report['deals'])} deal(s) and "
        f"{len(report['contacts'])} new contact(s) into CADENCE; left out: {skipped}"
    )
    if report["failed"]:
        line += f"; NOT written: {', '.join(f['deal'] for f in report['failed'])} (tried again next pass)"
    return line

"""kauppa-crm: a paid sale in the AIMEAT store becomes a CADENCE contact and a won deal, ONCE.

The node double answers the workspace tools as the connector does (index without ids, `items` with
ids, write = draft, publish = live) for two spaces, contact and deal, under CADENCE's locked schemas.
The feed double answers what the store's crmFeed answers (aimeat-commercial shop/test/crm-feed.test.mjs
makes that answer from real orders).
"""

from __future__ import annotations

import json

import pytest

from crewaimeat import kauppa_crm, workspace_tools

# CADENCE's locked schemas for the two spaces this agent writes (aimeat.io, ws-mrkj9sbei68, 2026-10-16).
CONTACT_KEYS = {
    "id", "tila", "email", "lahde", "luotu", "tagit", "etunimi", "puhelin", "omistaja", "sukunimi",
    "yritys_ref", "muistiinpano", "viimeisin_kosketus",
}  # fmt: skip
DEAL_KEYS = {
    "id", "arvo", "tila", "luotu", "vaihe", "otsikko", "omistaja", "valuutta", "yritys_ref", "kontakti_ref",
    "todennakoisyys", "odotettu_klousaus",
}  # fmt: skip
REQUIRED = {
    "contact": {"id", "omistaja", "tila", "luotu"},
    "deal": {"id", "otsikko", "omistaja", "vaihe", "tila", "luotu"},
}
KEYS = {"contact": CONTACT_KEYS, "deal": DEAL_KEYS}
MANIFEST = {
    "objectTypes": [
        {"mode": "records", "name": "contact", "backing": "memory", "namespace": "crm.contacts"},
        {"mode": "records", "name": "deal", "backing": "memory", "namespace": "crm.deals"},
        {"name": "mailmessage", "backing": "rows", "namespace": "crm.mail"},
    ]
}
NS = {"crm.contacts": "contact", "crm.deals": "deal"}


class Cadence:
    """Two record spaces, {space: {id: {"value": published, "draft": draft}}}, validated like the node."""

    def __init__(self, records=None):
        self.spaces = {"contact": {}, "deal": {}}
        for space, recs in (records or {}).items():
            for rid, value in recs.items():
                self.spaces[space][rid] = {"value": value}
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, agent_name, tool, payload, **kw):
        self.calls.append((tool, payload))
        if tool == "aimeat_workspace_read" and "ids" in payload:
            items, missing = [], []
            for rid in payload["ids"]:
                hit = False
                for space, recs in self.spaces.items():
                    if payload.get("space") not in (None, space) or rid not in recs:
                        continue
                    r = recs[rid]
                    item = {"space": space, "id": rid, "published": "value" in r}
                    item["value"] = r.get("value", r.get("draft"))
                    if "draft" in r:
                        item["draft"] = r["draft"]
                    items.append(item)
                    hit = True
                if not hit:
                    missing.append(rid)
            return {"mode": "content", "items": items, "missing": missing}
        if tool == "aimeat_workspace_read":
            index = {
                s: [
                    {"id": i, "title": i, "published": "value" in r, "has_draft": "draft" in r} for i, r in recs.items()
                ]
                for s, recs in self.spaces.items()
            }
            return {"manifest": MANIFEST, "mode": "index", "index": index}
        if tool == "aimeat_workspace_write":
            space, value = payload["space"], payload["value"]
            extra = set(value) - KEYS[space]
            lacking = REQUIRED[space] - set(value)
            if extra or lacking or value.get("id") != payload["id"]:
                return {"ok": False, "error": {"code": "SCHEMA", "message": f"extra {extra} missing {lacking}"}}
            if space == "deal" and value["tila"] not in ("open", "won", "lost"):
                return {"ok": False, "error": {"code": "SCHEMA", "message": "tila"}}
            if space == "contact" and value["tila"] not in (
                "uusi",
                "kontaktoitu",
                "qualifioitu",
                "asiakas",
                "menetetty",
            ):
                return {"ok": False, "error": {"code": "SCHEMA", "message": "tila"}}
            self.spaces[space].setdefault(payload["id"], {})["draft"] = json.loads(json.dumps(value))
            return {"written": "k", "id": payload["id"], "space": space, "mode": "records"}
        if tool == "aimeat_workspace_publish":
            r = self.spaces[NS[payload["namespace"]]][payload["id"]]
            r["value"] = r.pop("draft")
            return {"published": True}
        return None

    def live(self, space):
        return {i: r["value"] for i, r in self.spaces[space].items() if "value" in r}

    def writes(self):
        return [p for t, p in self.calls if t == "aimeat_workspace_write"]


def sale(order="ord-abc1", email="Kaisa.Koe@Acme.fi", **over):
    s = {
        "saleId": order,
        "order": order,
        "kind": "place",
        "founding": True,
        "foundingNumber": 3,
        "product": "Founder place (Team-size)",
        "tier": "team5",
        "period": "yearly",
        "amountEur": 396,
        "paidCents": 49104,
        "currency": "EUR",
        "paidAt": "2026-10-16T08:00:05.000Z",
        "method": "card",
        "invoice": "OS-2026-0007",
        "test": False,
        "refunded": False,
        "address": "acme",
        "affiliate": None,
        "buyer": {
            "name": "Kaisa Koe",
            "company": "Acme Oy",
            "email": email.lower(),
            "country": "Finland",
            "vatId": "FI12345678",
            "account": "kaisa",
        },
    }
    s.update(over)
    return s


ENV = {"KAUPPA_STORE_PAT": "pat", "KAUPPA_CRM_FEED_TOKEN": "tok"}
OMISTAJA = "happydude500001@aimeat-finland-001-genesis"


@pytest.fixture
def node(monkeypatch):
    def make(records=None):
        n = Cadence(records)
        monkeypatch.setattr(workspace_tools, "_aimeat_call", n)
        return n

    return make


def run(sales, env=None, **kw):
    return kauppa_crm.sync_store_sales(env=dict(ENV, **(env or {})), fetch=lambda *a: sales, **kw)


def test_a_paid_founder_place_becomes_a_customer_and_a_won_deal(node):
    n = node()
    report = run([sale()])
    contacts, deals = n.live("contact"), n.live("deal")
    assert list(contacts) == ["kauppa-kaisa-koe-acme-fi"]
    assert list(deals) == ["kauppa-ord-abc1"]
    c = contacts["kauppa-kaisa-koe-acme-fi"]
    assert c["tila"] == "asiakas"
    assert c["lahde"] == "store.aimeat.io"
    assert c["email"] == "kaisa.koe@acme.fi"
    assert (c["etunimi"], c["sukunimi"]) == ("Kaisa", "Koe")
    assert c["yritys_ref"] == "Acme Oy"
    assert c["omistaja"] == OMISTAJA
    assert "founding" in c["tagit"] and "kauppa" in c["tagit"]
    d = deals["kauppa-ord-abc1"]
    assert d["tila"] == "won" and d["vaihe"] == "voitettu" and d["todennakoisyys"] == 100
    assert (d["arvo"], d["valuutta"]) == (396, "EUR")
    assert d["kontakti_ref"] == "kauppa-kaisa-koe-acme-fi"
    assert d["yritys_ref"] == "Acme Oy"
    assert d["odotettu_klousaus"] == "2026-10-16"
    assert "#3" in d["otsikko"] and "Acme Oy" in d["otsikko"]
    assert d["omistaja"] == OMISTAJA
    assert report["deals"] == ["kauppa-ord-abc1"] and report["contacts"] == ["kauppa-kaisa-koe-acme-fi"]


def test_written_once_a_second_pass_writes_nothing(node):
    n = node()
    run([sale()])
    before = len(n.writes())
    report = run([sale()])
    assert len(n.writes()) == before, "a sale already in CADENCE is not written again"
    assert report["deals"] == [] and report["skipped"] == {"already in CADENCE": 1}


def test_a_contact_with_that_email_is_used_and_left_as_it_is(node):
    mine = {
        "id": "c-1",
        "omistaja": OMISTAJA,
        "tila": "qualifioitu",
        "luotu": "2026-09-01",
        "email": "Kaisa.Koe@ACME.fi",
    }
    n = node({"contact": {"c-1": mine}})
    run([sale()])
    assert n.live("contact") == {"c-1": mine}, "the person's own contact is not touched"
    assert n.live("deal")["kauppa-ord-abc1"]["kontakti_ref"] == "c-1"


def test_two_sales_to_one_buyer_make_one_contact(node):
    n = node()
    run(
        [
            sale("ord-1"),
            sale("ord-2", kind="box", founding=False, foundingNumber=None, product="AIMEAT on your own server"),
        ]
    )
    assert list(n.live("contact")) == ["kauppa-kaisa-koe-acme-fi"]
    assert sorted(n.live("deal")) == ["kauppa-ord-1", "kauppa-ord-2"]


def test_test_sales_are_left_out_unless_asked_for(node):
    n = node()
    report = run([sale(test=True)])
    assert n.live("deal") == {} and report["skipped"] == {"test sale": 1}
    run([sale(test=True)], env={"KAUPPA_CRM_INCLUDE_TEST": "1"})
    assert list(n.live("deal")) == ["kauppa-ord-abc1"]


def test_a_founding_payment_with_no_order_is_a_deal_without_a_contact(node):
    n = node()
    loose = sale(
        None,
        saleId="founding-4-20261016090000",
        kind="founding",
        foundingNumber=4,
        product="Founder place",
        method="invoice",
        buyer={"name": "", "company": "Beta Oy", "email": "", "country": "", "vatId": "", "account": ""},
    )
    run([loose])
    assert n.live("contact") == {}
    d = n.live("deal")["kauppa-founding-4-20261016090000"]
    assert "kontakti_ref" not in d and d["yritys_ref"] == "Beta Oy" and d["tila"] == "won"


def test_a_sale_refunded_before_it_reached_cadence_is_a_lost_deal(node):
    n = node()
    run([sale(refunded=True)])
    d = n.live("deal")["kauppa-ord-abc1"]
    assert (d["tila"], d["vaihe"], d["todennakoisyys"]) == ("lost", "hävitty", 0)


def test_a_deal_left_as_a_draft_is_published_on_the_next_pass(node):
    n = node()
    n.spaces["deal"]["kauppa-ord-abc1"] = {"draft": {"id": "kauppa-ord-abc1", "otsikko": "x", "omistaja": OMISTAJA,
                                                      "vaihe": "voitettu", "tila": "won", "luotu": "2026-10-16"}}  # fmt: skip
    run([sale()])
    assert "kauppa-ord-abc1" in n.live("deal")


def test_nothing_is_written_when_cadence_cannot_be_read(node, monkeypatch):
    n = node()
    monkeypatch.setattr(
        workspace_tools,
        "_aimeat_call",
        lambda *a, **k: {"ok": False, "error": {"code": "SCOPE_DENIED", "message": "x"}},
    )
    report = run([sale()])
    assert report["error"].startswith("CADENCE could not be read")
    assert n.writes() == []


def test_without_the_store_credentials_it_says_so_and_reads_nothing(node):
    n = node()
    report = kauppa_crm.sync_store_sales(env={}, fetch=lambda *a: pytest.fail("fetched without credentials"))
    assert "KAUPPA_STORE_PAT" in report["error"] and "KAUPPA_CRM_FEED_TOKEN" in report["error"]
    assert n.calls == []


def test_the_feed_is_asked_with_the_pat_and_the_token(monkeypatch):
    seen = {}

    class Res:
        status = 200

        def read(self):
            return json.dumps({"ok": True, "data": {"sales": [sale()], "testMode": False}}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(req, timeout=None):
        seen["url"], seen["auth"], seen["body"] = req.full_url, req.get_header("Authorization"), json.loads(req.data)
        return Res()

    monkeypatch.setattr(kauppa_crm.urllib.request, "urlopen", urlopen)
    got = kauppa_crm.fetch_sales("https://store.example/", "pat-1", "tok-1")
    assert got[0]["order"] == "ord-abc1"
    assert seen == {
        "url": "https://store.example/v1/ext/shop/crmFeed",
        "auth": "Bearer pat-1",
        "body": {"token": "tok-1"},
    }


def test_a_refused_feed_raises_with_the_stores_words(monkeypatch):
    class Res:
        status = 200

        def read(self):
            return json.dumps({"ok": True, "data": {"error": "unauthorized"}}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(kauppa_crm.urllib.request, "urlopen", lambda req, timeout=None: Res())
    with pytest.raises(kauppa_crm.FeedError, match="unauthorized"):
        kauppa_crm.fetch_sales("https://store.example", "p", "t")

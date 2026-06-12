"""Offers derivation floor — deterministic, no network (samples off), mirrors spec §4."""

import pytest

from crewaimeat.offers import _OFFER_META, _contracts, offer_from_contract, offers_doc

SPEC_FIELDS = {"id", "title", "ask", "example", "tags", "cost", "latency", "repeatability",
               "verification", "dataHandling", "availability", "requirements", "consequences",
               "deliverable"}
COSTS = {"free", "cheap", "expensive"}
LATENCIES = {"seconds", "minutes", "long-running"}
VERIFICATIONS = {"deterministic", "gated", "ungated"}
DATA_HANDLING = {"local-only", "llm-provider", "third-party"}
FORMATS = {"document", "record", "board-post", "file", "app"}
CONSEQUENCE_TYPES = {"creates-agent", "creates-schedule", "publishes-public", "external-send",
                     "mutates-live-app", "delegates-to-agent", "mutates-host"}


def test_every_contract_has_authored_metadata():
    ids = {c["id"] for c in _contracts()}
    assert ids == set(_OFFER_META), "every CONTRACT needs its authored offer constants (and vice versa)"


@pytest.mark.parametrize("contract", _contracts(), ids=lambda c: c["id"])
def test_offer_shape_matches_spec(contract):
    o = offer_from_contract(contract, with_sample=False)
    assert set(o) == SPEC_FIELDS
    assert o["cost"] in COSTS and o["latency"] in LATENCIES
    assert o["repeatability"] == "idempotent" and o["verification"] in VERIFICATIONS
    assert o["dataHandling"] in DATA_HANDLING
    assert len(o["ask"]) <= 500
    assert any(m in o["ask"] for m in ("don't", "refuse", "no ", " only")), \
        "ask must carry negative scope (hard rule 3)"
    assert o["deliverable"]["format"] in FORMATS
    assert o["deliverable"]["location"]["space"], "location must be machine-readable"
    assert o["deliverable"]["sample"] == "untested"  # no-network mode: never invented
    for cq in o["consequences"]:
        assert cq["type"] in CONSEQUENCE_TYPES
    assert any(r["fix"] == "adopt-contract" for r in o["requirements"])


def test_multi_contract_agent_gets_all_offers():
    doc = offers_doc("web-researcher", with_samples=False)
    assert {o["id"] for o in doc["offers"]} == {"research", "market-scan", "company-research"}
    assert doc["version"] == 1 and doc["updatedAt"]
    assert len(doc["offers"]) <= 40  # node-side cap


def test_single_contract_agents():
    for agent, expected in (("image-scout", {"moodboard"}), ("postman", {"mail"}),
                            ("activity-reporter", {"activity-report"})):
        assert {o["id"] for o in offers_doc(agent, with_samples=False)["offers"]} == expected


def test_crew_offers_match_spec_shape():
    from crewaimeat.offers import _CREW_OFFERS, crew_offer, offers_doc_any
    for agent, metas in _CREW_OFFERS.items():
        for meta in metas:
            o = crew_offer(agent, meta, with_sample=False)
            assert set(o) == SPEC_FIELDS
            assert o["cost"] in COSTS and o["latency"] in LATENCIES
            assert o["repeatability"] in {"idempotent", "accumulative", "destructive"}
            assert o["verification"] in VERIFICATIONS and o["dataHandling"] in DATA_HANDLING
            assert any(m in o["ask"] for m in ("don't", "refuse", "not ", " only")), \
                f"{agent}/{meta['id']}: ask must carry negative scope"
            for cq in o["consequences"]:
                assert cq["type"] in CONSEQUENCE_TYPES
            assert o["deliverable"]["location"]["space"].startswith(f"crews.{agent}")
    # crew-forge's build offer must carry the approval-blocking consequence
    forge = offers_doc_any("crew-forge", with_samples=False)["offers"]
    build = next(o for o in forge if o["id"] == "build-crew")
    assert any(c.get("requiresApproval") for c in build["consequences"])
    assert build["repeatability"] == "accumulative"

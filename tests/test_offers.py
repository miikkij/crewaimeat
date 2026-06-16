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
    # base spec fields always; workflow-compat fields are OPTIONAL (a document-output contract derives
    # its signals from the request→result spaces and so becomes workflow-compatible).
    assert SPEC_FIELDS <= set(o)
    assert set(o) - SPEC_FIELDS <= {"required_to_function", "success_signal"}
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


def test_md_excerpt_preserves_markdown_line_structure():
    from crewaimeat.offers import _md_excerpt
    md = "# Otsikko\n\nTeksti kappale.\n\n| a | b |\n|---|---|\n" + "\n".join(
        f"| rivi{i} | arvo{i} |" for i in range(40))
    out = _md_excerpt(md, max_chars=200)
    assert out.startswith("# Otsikko\n\nTeksti"), "heading must stay on its own line"
    assert "|---|---|" in out, "table separator row must survive"
    assert out.endswith("…") and "\n| rivi" in out, "cut at a line boundary, never mid-row"
    assert " | rivi0 | arvo0 | | rivi1" not in out, "rows must not be flattened to one line"
    short = "# A\n\nlyhyt."
    assert _md_excerpt(short, max_chars=200) == short


def test_crew_offers_match_spec_shape():
    from crewaimeat.offers import _CREW_OFFERS, crew_offer, offers_doc_any
    for agent, metas in _CREW_OFFERS.items():
        for meta in metas:
            o = crew_offer(agent, meta, with_sample=False)
            # base spec fields always; workflow-compat fields are OPTIONAL (only agents whose offer
            # declares its signals get them — that's what makes them workflow-compatible).
            assert SPEC_FIELDS <= set(o)
            assert set(o) - SPEC_FIELDS <= {"required_to_function", "success_signal", "dependsOn"}
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


# ── Golden samples (task 1): every published offer carries a real example, never "untested" ──
import json as _json  # noqa: E402

import crewaimeat.offers as _off  # noqa: E402


def _no_live_samples(monkeypatch):
    """Simulate an agent that has never run: no live sample, so authored examples must fill in."""
    monkeypatch.setattr(_off, "fetch_crew_sample", lambda agent: "untested")
    monkeypatch.setattr(_off, "fetch_sample", lambda agent, out: "untested")


def test_golden_sample_never_untested(monkeypatch):
    _no_live_samples(monkeypatch)
    for agent in _off.PILOT_AGENTS + _off.CREW_AGENTS:
        for o in _off.offers_doc_any(agent, with_samples=True)["offers"]:
            s = o["deliverable"]["sample"]
            assert s != "untested", f"{agent}/{o['id']}: authored golden sample missing"
            assert isinstance(s, (str, dict)), f"{agent}/{o['id']}: sample must be markdown or a JSON object"
            size = len(s) if isinstance(s, str) else len(_json.dumps(s))
            assert size <= 8000, f"{agent}/{o['id']}: sample exceeds the 8000-char contract cap"


def test_live_sample_wins_over_authored(monkeypatch):
    monkeypatch.setattr(_off, "fetch_crew_sample", lambda agent: "LIVE EXCERPT")
    doc = _off.offers_doc_any("joker", with_samples=True)
    assert doc["offers"][0]["deliverable"]["sample"] == "LIVE EXCERPT"


# ── JSON-shaped output (task 2): structured offers publish an object sample + a valid format ──
def test_json_shaped_offers_have_object_sample(monkeypatch):
    _no_live_samples(monkeypatch)
    for agent, oid in (("idea-feasibility-rater", "rate-feasibility"),
                       ("probability-creator", "estimate-spectrum"),
                       ("daily-features-writer", "evening-features")):
        o = next(x for x in _off.offers_doc_any(agent, with_samples=True)["offers"] if x["id"] == oid)
        assert isinstance(o["deliverable"]["sample"], dict), f"{oid}: structured offer needs an object sample"
        # format stays "document" until the node enum adds "json" (JSON_FORMAT_SUPPORTED flip)
        assert o["deliverable"]["format"] in (FORMATS | {"json"})


# ── dependsOn (task 4): pipeline offers advertise their upstream, derived from the workflow ──
def test_depends_on_derives_from_workflow():
    from crewaimeat.offers import offers_doc_any as _doc
    ed = next(o for o in _doc("editorial-writer")["offers"] if o["id"] == "evening-editorial")
    assert "dependsOn" in ed
    up = {d["offer"] for d in ed["dependsOn"]}
    assert {"evening-write-a", "evening-write-b", "space-weather"} <= up
    for d in ed["dependsOn"]:
        assert {"offer", "agent", "workflow"} <= set(d)
    # an offer with no hard upstream omits dependsOn entirely
    joker = _doc("joker")["offers"][0]
    assert "dependsOn" not in joker


# ── Per-offer run tagging (task 3): the deliverable write carries both task:<id> and offer:<id> ──
def test_offer_tag_emitted_on_publish(monkeypatch):
    import crewaimeat.aimeat_crew as ac
    calls: list = []
    monkeypatch.setattr(ac, "_aimeat_call",
                        lambda agent, tool, payload: (calls.append((tool, payload)) or {"ok": True}))
    cb = ac._make_publish_cb("joker", "crews.joker.task-1", task_id="task-1-abc", offer_id="tell-jokes")

    class _Out:
        raw = "a joke"

    cb(_Out())
    writes = [p for (t, p) in calls if t == "aimeat_memory_write" and p["key"] == "crews.joker.task-1"]
    assert writes, "the deliverable must be written"
    tags = writes[0]["tags"]
    assert "task:task-1-abc" in tags and "offer:tell-jokes" in tags


def test_no_offer_tag_when_not_an_offer_task(monkeypatch):
    import crewaimeat.aimeat_crew as ac
    calls: list = []
    monkeypatch.setattr(ac, "_aimeat_call",
                        lambda agent, tool, payload: (calls.append((tool, payload)) or {"ok": True}))
    cb = ac._make_publish_cb("joker", "crews.joker.task-1", task_id="task-1-abc")  # no offer_id

    class _Out:
        raw = "a joke"

    cb(_Out())
    tags = next(p for (t, p) in calls if p["key"] == "crews.joker.task-1")["tags"]
    assert tags == ["task:task-1-abc"]

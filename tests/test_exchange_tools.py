"""EXCHANGE tools — the deterministic negotiator logic + the forge_catalog / crew_def wiring.

Network-free: the REST wrappers are not exercised against a node here (that is the live-run deliverable);
these lock the PORTED band + match logic (which must be verbatim-correct — it decides real money) and the
two resolution surfaces (forge_catalog capability + crew_def TOOL_REGISTRY) that make the crew-defs run.
"""

from __future__ import annotations

import json

from crewaimeat import exchange_tools as ex
from crewaimeat import forge_catalog


# ── autonomy band gate (enforced at selection AND at every incoming proposal) ────────────────────────
def _band(**over):
    b = {
        "autonomy": "auto",
        "max_price": 100,
        "provider_whitelist": ["happydude500001"],
        "budget_cap": 1000,
        "min_match": 0.5,
    }
    b.update(over)
    return b


def test_band_auto_acts_only_within_price_and_whitelist():
    d = ex.band_decision(80, "happydude500001", _band())
    assert d["decision"] == "auto"


def test_band_price_over_max_forces_propose():
    d = ex.band_decision(120, "happydude500001", _band())
    assert d["decision"] == "propose"
    assert any("max_price" in r for r in d["reasons"])


def test_band_provider_not_whitelisted_forces_propose():
    d = ex.band_decision(50, "stranger", _band())
    assert d["decision"] == "propose"
    assert any("whitelist" in r for r in d["reasons"])


def test_band_supervised_never_acts_even_within_price():
    d = ex.band_decision(10, "happydude500001", _band(autonomy="supervised"))
    assert d["decision"] == "propose"
    assert any("supervised" in r for r in d["reasons"])


def test_band_boundary_price_equals_max_is_auto():
    # price <= max_price (inclusive) is within band
    assert ex.band_decision(100, "happydude500001", _band())["decision"] == "auto"


# ── machine I/O match (recursive property-name intersection over need output) ─────────────────────────
def test_schema_property_names_recurses_nested_and_arrays():
    schema = {
        "type": "object",
        "properties": {
            "price": {"type": "number"},
            "rows": {
                "type": "array",
                "items": {"type": "object", "properties": {"ticker": {"type": "string"}, "close": {"type": "number"}}},
            },
        },
    }
    assert ex.schema_property_names(schema) == {"price", "rows", "ticker", "close"}


def test_schema_property_names_walks_composition_and_defs():
    schema = {
        "allOf": [{"properties": {"a": {}}}, {"$ref": "#/$defs/b"}],
        "$defs": {"b": {"properties": {"c": {}}}},
    }
    assert ex.schema_property_names(schema) == {"a", "c"}


def test_match_score_is_intersection_over_need():
    need = {"properties": {"price": {}, "volume": {}}}  # need must PRODUCE price+volume
    off_full = {"properties": {"price": {}, "volume": {}, "extra": {}}}
    off_half = {"properties": {"price": {}, "unrelated": {}}}
    assert ex.match_score(need, off_full) == 1.0
    assert ex.match_score(need, off_half) == 0.5
    assert ex.match_score(need, {"properties": {}}) == 0.0


def test_match_score_empty_need_is_zero_not_crash():
    assert ex.match_score({}, {"properties": {"x": {}}}) == 0.0


# ── tool factory: all 13 tools present with the node-contract names ──────────────────────────────────
def test_factory_exposes_every_named_tool():
    tools = ex.make_exchange_tools("some-agent", owner="happydude500001")
    names = {getattr(t, "name", "") for t in tools}
    expected = {
        "exchange_browse",
        "exchange_detail",
        "exchange_accept",
        "exchange_run",
        "exchange_post_need",
        "exchange_bid",
        "exchange_proposals",
        "exchange_proposal_decide",
        "exchange_work_list",
        "exchange_work_start",
        "exchange_work_deliver",
        "exchange_match_score",
        "exchange_band_decide",
    }
    assert expected <= names, f"missing {expected - names}"


def test_deterministic_tools_run_without_network():
    tools = {getattr(t, "name", ""): t for t in ex.make_exchange_tools("a", owner="o")}
    band_out = json.loads(
        tools["exchange_band_decide"].run(
            price=10, provider_owner="o", band_json=json.dumps(_band(provider_whitelist=["o"]))
        )
    )
    assert band_out["decision"] == "auto"
    match_out = json.loads(
        tools["exchange_match_score"].run(
            need_output_schema_json='{"properties":{"x":{}}}',
            offering_output_schema_json='{"properties":{"x":{},"y":{}}}',
            min_match=0.5,
        )
    )
    assert match_out["score"] == 1.0 and match_out["meets_min"] is True


def test_rest_tool_without_token_fails_soft_not_crash():
    # No token file for this agent → the wrapper returns an ERROR string, never raises.
    tools = {getattr(t, "name", ""): t for t in ex.make_exchange_tools("no-such-agent", owner="nobody")}
    out = tools["exchange_browse"].run(q="weather")
    assert out.startswith("ERROR:")


# ── resolution surfaces: forge_catalog capability + crew_def TOOL_REGISTRY ────────────────────────────
def test_forge_catalog_registers_exchange_and_preflight_passes():
    cap = forge_catalog.get("exchange")
    assert cap is not None
    ok, _reason = forge_catalog.preflight(cap)  # no env/deps required → usable everywhere
    assert ok is True
    assert "exchange" in {c.id for c in forge_catalog.available_capabilities()}


def test_forge_catalog_emits_exchange_tools_block():
    src, usable, dropped = forge_catalog.emit_tools_function(["exchange"])
    assert "exchange" in usable and not dropped
    assert "make_exchange_tools(AGENT_NAME)" in src


def test_crew_def_registry_resolves_exchange_bundle_and_each_name():
    from crewaimeat import crew_def
    from crewaimeat.exchange_tools import EXCHANGE_TOOL_NAMES

    # the whole bundle
    assert "exchange" in crew_def.TOOL_REGISTRY
    built = crew_def.TOOL_REGISTRY["exchange"]("agent-x", None)
    assert any(getattr(t, "name", "") == "exchange_browse" for t in built)
    # AND every individual tool name (a crew-def may list them one by one)
    for name in EXCHANGE_TOOL_NAMES:
        assert name in crew_def.TOOL_REGISTRY, f"{name} not resolvable"
        one = crew_def.TOOL_REGISTRY[name]("agent-x", None)
        assert [getattr(t, "name", "") for t in one] == [name]


def test_validate_crew_doc_accepts_individual_exchange_tools():
    from crewaimeat import crew_def

    doc = {
        "agent_name": "exchange-buyer",
        "agents": [
            {
                "name": "buyer",
                "role": "Exchange buyer",
                "goal": "source and run data contracts within the autonomy band",
                "backstory": "A closed negotiation agent on the owner's own fleet.",
                "tools": [
                    "exchange_browse",
                    "exchange_detail",
                    "exchange_match_score",
                    "exchange_band_decide",
                    "exchange_accept",
                    "exchange_run",
                    "exchange_proposals",
                    "exchange_proposal_decide",
                ],
            }
        ],
        "tasks": [
            {
                "description": "Negotiate + fulfil a data need.",
                "expected_output": "a delivered result",
                "agent": "buyer",
            }
        ],
    }
    errors = crew_def.validate_crew_doc(doc)
    tool_errors = [e for e in errors if "unknown tool" in e]
    assert not tool_errors, tool_errors

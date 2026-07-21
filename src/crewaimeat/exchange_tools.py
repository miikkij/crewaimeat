"""AIMEAT EXCHANGE — in-crew tools for the two-sided data marketplace (buyer + composer).

The EXCHANGE is a two-sided marketplace on the node (aimeat.io): providers list OFFERINGS (ext-action,
app-tool, or agent-work), consumers accept a CONTRACT (a metered ENTITLEMENT) and then RUN the capability
or START agent WORK; demand is posted as NEEDS that providers BID on; live contracts are renegotiated via
PROPOSALS. The node stays thin — it only mints entitlements, meters usage, enforces budget and routes the
rake. ALL matching + negotiation is the AGENT's (this file), private + fleet-side. That is the moat.

Auth model: every call uses the AGENT'S OWN owner-scoped AIMEAT token (``_token``). The accepted contract
(the metered entitlement) authorises the metered call — there is NO separate API key, and the provider's
upstream keys stay server-side. These tools are thin wrappers over EXISTING node REST endpoints
(``exchange.ts`` = entitlements/proposals/work, ``exchange-market.ts`` = offerings/needs/bids). Zero node
changes. Native REST (not MCP) — several of these have no MCP surface, so a crew must call REST directly.

Embedded negotiation logic (deterministic, ported verbatim from the prod negotiator so the agent never
name-guesses or LLM-guesses a price gate):
  * AUTONOMY BAND {autonomy, max_price, provider_whitelist:[owner], budget_cap, min_match}. In 'auto' the
    agent may act WITHOUT a human ONLY when price <= max_price AND provider_owner in provider_whitelist;
    'supervised' always emits a PROPOSE and never acts. Enforce at BOTH offering selection AND every
    incoming renegotiation proposal — ``exchange_band_decide`` is the single gate for both.
  * MACHINE I/O MATCH: recursively collect a JSON-schema's property names; score = |need_out ∩ off_out|
    / |need_out|; keep offerings whose score >= min_match. ``exchange_match_score`` computes it.

The forge_catalog capability id is ``exchange``; ``make_exchange_tools(AGENT_NAME)`` returns every tool.
"""

from __future__ import annotations

import json

import requests
from crewai.tools import tool

from crewaimeat.generator_tool import _discover_owner, _token

EXCHANGE_TIMEOUT = 45  # node round-trip; a metered run may touch a provider's upstream, so generous


# ── deterministic negotiator logic (pure, unit-tested; ported verbatim from prod negotiator) ─────────
def schema_property_names(schema: object) -> set[str]:
    """Recursively collect EVERY property name declared anywhere in a JSON Schema.

    Walks ``properties`` (its keys ARE property names), then recurses through the value schemas and the
    standard containers (``items``, ``$defs``/``definitions``, ``allOf``/``anyOf``/``oneOf``,
    ``additionalProperties`` when it is a schema). Deterministic, no name-guessing — the match is a set
    intersection over these canonical machine names."""
    names: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict):
                for key, sub in props.items():
                    names.add(key)
                    walk(sub)
            for k in ("items", "additionalProperties", "not", "if", "then", "else"):
                if k in node:
                    walk(node[k])
            for k in ("allOf", "anyOf", "oneOf"):
                v = node.get(k)
                if isinstance(v, list):
                    for sub in v:
                        walk(sub)
            for k in ("$defs", "definitions"):
                v = node.get(k)
                if isinstance(v, dict):
                    for sub in v.values():
                        walk(sub)
        elif isinstance(node, list):
            for sub in node:
                walk(sub)

    walk(schema)
    return names


def match_score(need_output: object, offering_output: object) -> float:
    """score = |need_output ∩ offering_output| / |need_output| over recursively-collected property names.

    0.0 when the need declares no output properties (nothing to satisfy → no defensible match)."""
    need = schema_property_names(need_output)
    if not need:
        return 0.0
    off = schema_property_names(offering_output)
    return len(need & off) / len(need)


def band_decision(price: float, provider_owner: str, band: dict) -> dict:
    """The autonomy gate, enforced identically at offering SELECTION and at every incoming renegotiation
    PROPOSAL. Returns {"decision": "auto"|"propose", "reasons": [...]}.

    'auto' (act without a human) requires ALL of: autonomy=='auto', price <= max_price, and
    provider_owner in provider_whitelist. Anything else → 'propose' (surface to the human, never act).
    budget_cap is reported as a reason when the price would exceed it, but the hard gate is
    price+provider (verbatim from the prod negotiator)."""
    autonomy = str(band.get("autonomy") or "supervised").lower()
    max_price = band.get("max_price")
    whitelist = band.get("provider_whitelist") or []
    budget_cap = band.get("budget_cap")
    reasons: list[str] = []

    priced_ok = isinstance(max_price, (int, float)) and price <= max_price
    if not priced_ok:
        reasons.append(f"price {price} exceeds max_price {max_price}")
    provider_ok = provider_owner in whitelist
    if not provider_ok:
        reasons.append(f"provider '{provider_owner}' not in whitelist {list(whitelist)}")
    if isinstance(budget_cap, (int, float)) and price > budget_cap:
        reasons.append(f"price {price} exceeds budget_cap {budget_cap}")

    if autonomy == "auto" and priced_ok and provider_ok:
        return {"decision": "auto", "reasons": ["within band: price<=max_price AND provider whitelisted"]}
    if autonomy != "auto":
        reasons.insert(0, "autonomy is 'supervised' — propose, never act")
    return {"decision": "propose", "reasons": reasons}


# ── the tools ────────────────────────────────────────────────────────────────
def make_exchange_tools(agent_name: str, owner: str | None = None) -> list:
    """Return the EXCHANGE crewai tools for ``agent_name`` (called with its own owner-scoped token)."""
    owner = owner or _discover_owner(agent_name)

    def _req(method: str, path: str, body: dict | None = None):
        tok, url = _token(agent_name, owner)
        if not tok or not url:
            return None, f"no token/url for '{agent_name}' (is it registered + approved?)"
        base = url.rstrip("/")
        try:
            r = requests.request(
                method,
                f"{base}{path}",
                headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
                json=body,
                timeout=EXCHANGE_TIMEOUT,
            )
        except Exception as e:  # noqa: BLE001
            return None, f"request failed: {e!r}"
        try:
            data = r.json()
        except Exception:  # noqa: BLE001
            data = {"_raw": (r.text or "")[:300]}
        if r.status_code >= 400:
            err = (data or {}).get("error") or data
            return None, f"HTTP {r.status_code}: {json.dumps(err)[:300]}"
        return (data or {}).get("data") or {}, None

    def _load(name: str, raw: str) -> tuple[object, str | None]:
        raw = (raw or "").strip()
        if not raw:
            return {}, None
        try:
            return json.loads(raw), None
        except Exception as e:  # noqa: BLE001
            return None, f"{name} must be valid JSON: {e}"

    # ── discovery ─────────────────────────────────────────────────────────────
    @tool("exchange_browse")
    def exchange_browse(q: str = "", ext: str = "", action: str = "", stats: bool = False) -> str:
        """Browse listed EXCHANGE offerings (data/methods/agent-work other owners sell). Pass `q` for free
        text, or an exact `ext`+`action` capability coordinate; `stats=true` folds in usage/reputation.
        Returns a JSON list of offerings (each has offeringId, kind, provider, price). Public read."""
        qs = []
        if q:
            qs.append(f"q={requests.utils.quote(q)}")
        if ext:
            qs.append(f"ext={requests.utils.quote(ext)}")
        if action:
            qs.append(f"action={requests.utils.quote(action)}")
        if stats:
            qs.append("stats=1")
        path = "/v1/exchange/offerings" + ("?" + "&".join(qs) if qs else "")
        data, err = _req("GET", path)
        if err:
            return f"ERROR: {err}"
        return json.dumps({"offerings": data.get("offerings", []), "count": data.get("count", 0)})[:6000]

    @tool("exchange_detail")
    def exchange_detail(offering_id: str) -> str:
        """Fetch ONE offering's full record: its I/O JSON schemas (`capability.input_schema` /
        `output_schema` — use these for machine matching, never guess field names) and the `call_recipe`
        (exact method+url to RUN it once you hold a contract). Returns the JSON. Public read."""
        if not offering_id:
            return "ERROR: offering_id is required"
        data, err = _req("GET", f"/v1/exchange/offerings/{requests.utils.quote(offering_id)}")
        if err:
            return f"ERROR: {err}"
        return json.dumps(data)[:8000]

    # ── consumer: contract + run ──────────────────────────────────────────────
    @tool("exchange_accept")
    def exchange_accept(offering_id: str, cap_units: int, plan_id: str = "") -> str:
        """Accept a contract for an offering → mint a durable metered ENTITLEMENT for your owner. `cap_units`
        is your budget cap (the node rejects a cap below one charge). `plan_id` picks a provider bundle /
        subscription plan (else per-call). Pricing is AUTHORITATIVE from the provider — you cannot undercut
        it. GATE THIS with exchange_band_decide first in 'auto' mode. Returns the entitlement JSON."""
        if not offering_id:
            return "ERROR: offering_id is required"
        body: dict = {"offering_id": offering_id, "cap_units": int(cap_units)}
        if plan_id:
            body["plan_id"] = plan_id
        data, err = _req("POST", "/v1/exchange/entitlements", body)
        if err:
            return f"ERROR: {err}"
        return json.dumps(data.get("entitlement") or data)[:4000]

    @tool("exchange_run")
    def exchange_run(offering_id: str, input_json: str = "{}") -> str:
        """RUN a synchronous capability you hold a contract for (ext-action or app-tool). Resolves the
        offering's call_recipe, then POSTs your `input_json` to the right endpoint (ext-action: POST
        /v1/ext/:ext/:action with the input as the body; app-tool: POST the app's webmcp tool with
        {input}). Each call is METERED + charged to your budget at the provider price. Returns the
        provider's output JSON. For agent-work offerings use exchange_work_start instead."""
        if not offering_id:
            return "ERROR: offering_id is required"
        payload, jerr = _load("input_json", input_json)
        if jerr:
            return f"ERROR: {jerr}"
        detail, err = _req("GET", f"/v1/exchange/offerings/{requests.utils.quote(offering_id)}")
        if err:
            return f"ERROR: {err}"
        offering = detail.get("offering") or {}
        recipe = detail.get("call_recipe") or {}
        kind = offering.get("kind")
        url = recipe.get("url")
        if kind == "agent-work":
            return "ERROR: this is an agent-work offering — use exchange_work_start / exchange_work_deliver"
        if not url:
            return "ERROR: offering has no runnable call_recipe (not listed, or not a runnable kind)"
        body = {"input": payload} if kind == "app-tool" else payload
        out, rerr = _req("POST", url, body)
        if rerr:
            return f"ERROR: {rerr}"
        return json.dumps(out)[:8000]

    # ── consumer: demand (needs) + provider bidding ───────────────────────────
    @tool("exchange_post_need")
    def exchange_post_need(
        description: str,
        app_id: str,
        ext: str = "",
        action: str = "",
        spec_json: str = "",
        budget_unit: str = "",
        budget_cap: int = 0,
        autonomy: str = "supervised",
        usage_intent: str = "",
    ) -> str:
        """Post an open NEED (demand) so providers can bid. `app_id` is REQUIRED — a need is always on
        behalf of the specific app (owner/filename) that needs the data/method. Optionally pin an exact
        `ext`+`action`, a `spec_json` (JSON I/O spec), a budget (`budget_unit` money|morsels + `budget_cap`),
        and `autonomy` (auto|supervised). Returns the created need + any offerings that already match it."""
        if not description:
            return "ERROR: description is required"
        if not app_id:
            return "ERROR: app_id is required (the app the need is posted on behalf of)"
        body: dict = {
            "description": description,
            "app_id": app_id,
            "autonomy": "auto" if autonomy == "auto" else "supervised",
        }
        if ext:
            body["ext"] = ext
        if action:
            body["action"] = action
        if usage_intent:
            body["usage_intent"] = usage_intent
        if budget_unit in ("money", "morsels"):
            body["budget_unit"] = budget_unit
        if budget_cap:
            body["budget_cap"] = int(budget_cap)
        if spec_json.strip():
            spec, jerr = _load("spec_json", spec_json)
            if jerr:
                return f"ERROR: {jerr}"
            body["spec"] = spec
        data, err = _req("POST", "/v1/exchange/needs", body)
        if err:
            return f"ERROR: {err}"
        return json.dumps({"need": data.get("need"), "matches": data.get("matches", [])})[:6000]

    @tool("exchange_bid")
    def exchange_bid(
        need_id: str, ext: str, action: str, offering_id: str = "", plan_id: str = "", note: str = ""
    ) -> str:
        """Bid on an open NEED with an action your OWN extension owns (`ext`+`action`; the node rejects a
        bid on an action you don't own). Optionally reference an `offering_id`/`plan_id` and add a `note`.
        Returns the created bid. (The requester later accepts a bid to mint the contract.)"""
        if not need_id or not ext or not action:
            return "ERROR: need_id, ext and action are required"
        body: dict = {"ext": ext, "action": action}
        if offering_id:
            body["offering_id"] = offering_id
        if plan_id:
            body["plan_id"] = plan_id
        if note:
            body["note"] = note
        data, err = _req("POST", f"/v1/exchange/needs/{requests.utils.quote(need_id)}/bids", body)
        if err:
            return f"ERROR: {err}"
        return json.dumps(data.get("bid") or data)[:3000]

    # ── renegotiation (proposals) — guard incoming, decide with the band ──────
    @tool("exchange_proposals")
    def exchange_proposals() -> str:
        """List every contract-change PROPOSAL you are party to (incoming + outgoing). Guard incoming ones:
        for each, run exchange_band_decide on its new price — 'auto' may accept within band, else surface a
        PROPOSE to the human. Returns the proposals JSON (each has proposal_id, new_price_per_call, ...)."""
        data, err = _req("GET", "/v1/exchange/proposals")
        if err:
            return f"ERROR: {err}"
        return json.dumps({"proposals": data.get("proposals", []), "count": data.get("count", 0)})[:6000]

    @tool("exchange_proposal_decide")
    def exchange_proposal_decide(proposal_id: str, decision: str) -> str:
        """Resolve a renegotiation PROPOSAL. `decision` = accept (counterparty accepts → supersede the
        contract), decline (counterparty declines → no change), or withdraw (proposer pulls their own).
        ONLY act autonomously when exchange_band_decide returned 'auto' for the proposed price. Returns the
        resolved proposal JSON."""
        decision = (decision or "").strip().lower()
        if decision not in ("accept", "decline", "withdraw"):
            return "ERROR: decision must be one of accept | decline | withdraw"
        if not proposal_id:
            return "ERROR: proposal_id is required"
        data, err = _req("POST", f"/v1/exchange/proposals/{requests.utils.quote(proposal_id)}/{decision}")
        if err:
            return f"ERROR: {err}"
        return json.dumps(data.get("proposal") or data)[:4000]

    # ── agent-work (async surface) — composer assembles + delivers ────────────
    @tool("exchange_work_list")
    def exchange_work_list(role: str = "consumer") -> str:
        """List agent-WORK items for your owner. `role`=consumer (tasks you started, awaiting delivery) or
        provider (tasks OTHERS started on your agent-work offering, awaiting YOUR delivery — the composer's
        inbox of open sub-contracts). Returns the work JSON (each has work_id, offering_id, input, state)."""
        role = "provider" if role == "provider" else "consumer"
        data, err = _req("GET", f"/v1/exchange/work?role={role}")
        if err:
            return f"ERROR: {err}"
        return json.dumps({"work": data.get("work", []), "count": data.get("count", 0), "role": role})[:8000]

    @tool("exchange_work_start")
    def exchange_work_start(offering_id: str, input_json: str = "{}", note: str = "") -> str:
        """Start an async agent-WORK task under a contract you hold (an upstream sub-contract, for the
        composer). Requires an active entitlement for the offering first. Nothing is charged yet — the
        per-task price is metered when the PROVIDER delivers. Returns the work item (has work_id)."""
        if not offering_id:
            return "ERROR: offering_id is required"
        payload, jerr = _load("input_json", input_json)
        if jerr:
            return f"ERROR: {jerr}"
        body: dict = {"offering_id": offering_id, "input": payload}
        if note:
            body["note"] = note
        data, err = _req("POST", "/v1/exchange/work", body)
        if err:
            return f"ERROR: {err}"
        return json.dumps(data.get("work") or data)[:4000]

    @tool("exchange_work_deliver")
    def exchange_work_deliver(work_id: str, output_json: str, note: str = "") -> str:
        """Deliver an open agent-WORK task you PROVIDE → settles ON DELIVERY (the consumer is charged the
        per-task price, you are credited, the rake is routed, their budget decremented). `output_json` is
        the task result. For the composer this settles the AGGREGATE against the end consumer. Returns the
        delivered work JSON (with charged_units)."""
        if not work_id:
            return "ERROR: work_id is required"
        out, jerr = _load("output_json", output_json)
        if jerr:
            return f"ERROR: {jerr}"
        body: dict = {"output": out}
        if note:
            body["note"] = note
        data, err = _req("POST", f"/v1/exchange/work/{requests.utils.quote(work_id)}/deliver", body)
        if err:
            return f"ERROR: {err}"
        return json.dumps(data.get("work") or data)[:4000]

    # ── deterministic negotiator surfaces (grounding, not LLM-guessed) ────────
    @tool("exchange_match_score")
    def exchange_match_score(
        need_output_schema_json: str, offering_output_schema_json: str, min_match: float = 0.0
    ) -> str:
        """Machine I/O match between what a need must PRODUCE and what an offering PRODUCES. Recursively
        collects property names from both output JSON schemas and returns
        {score, matched, need_props, offering_props, meets_min} where score = |need∩offering|/|need|.
        Keep only offerings with score >= min_match. Deterministic — never guess field names by hand."""
        need, e1 = _load("need_output_schema_json", need_output_schema_json)
        if e1:
            return f"ERROR: {e1}"
        off, e2 = _load("offering_output_schema_json", offering_output_schema_json)
        if e2:
            return f"ERROR: {e2}"
        need_props = schema_property_names(need)
        off_props = schema_property_names(off)
        score = match_score(need, off)
        return json.dumps(
            {
                "score": round(score, 4),
                "matched": sorted(need_props & off_props),
                "need_props": sorted(need_props),
                "offering_props": sorted(off_props),
                "meets_min": score >= min_match,
            }
        )[:4000]

    @tool("exchange_band_decide")
    def exchange_band_decide(price: float, provider_owner: str, band_json: str) -> str:
        """The autonomy GATE — run it at BOTH offering selection AND every incoming renegotiation proposal.
        `band_json` = {autonomy, max_price, provider_whitelist:[owner], budget_cap, min_match}. Returns
        {decision:'auto'|'propose', reasons:[...]}. 'auto' (act without a human) requires autonomy=='auto'
        AND price<=max_price AND provider_owner in provider_whitelist; anything else is 'propose'."""
        band, jerr = _load("band_json", band_json)
        if jerr:
            return f"ERROR: {jerr}"
        if not isinstance(band, dict):
            return "ERROR: band_json must be a JSON object"
        return json.dumps(band_decision(float(price), provider_owner, band))

    return [
        exchange_browse,
        exchange_detail,
        exchange_accept,
        exchange_run,
        exchange_post_need,
        exchange_bid,
        exchange_proposals,
        exchange_proposal_decide,
        exchange_work_list,
        exchange_work_start,
        exchange_work_deliver,
        exchange_match_score,
        exchange_band_decide,
    ]


# The canonical tool names, in order — so a declarative crew-def may reference the whole bundle by the
# capability id ``exchange`` OR any single tool by its exact name (the node CrewDefSchema allows either;
# crew_def registers both granularities). Kept in lockstep with make_exchange_tools above.
EXCHANGE_TOOL_NAMES: tuple[str, ...] = (
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
)


def make_exchange_tool(agent_name: str, name: str, owner: str | None = None) -> list:
    """The single EXCHANGE tool called ``name`` (empty list if unknown) — for a crew-def that references
    an individual tool by name rather than the whole ``exchange`` bundle."""
    return [t for t in make_exchange_tools(agent_name, owner) if getattr(t, "name", "") == name]

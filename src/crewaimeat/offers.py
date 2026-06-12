"""Agent Offers v1 — derive offers docs DETERMINISTICALLY from workspace CONTRACTs.

Spec: docs/internal/2026-06-12-agent-offers-surface.md §4 (the single source both sides
build to). This module emits EXACTLY that shape so the node's Zod validation stays green:
structure (requirements, consequences, deliverable.location/format, repeatability,
verification, availability) derives from each contract module's CONTRACT dict; the
human-facing constants (title/ask/example/cost/latency) are authored literals below —
still zero-LLM, code-reviewed, no drift. Hard rules honoured: every ask carries negative
scope; sample is a REAL excerpt fetched from the agent's latest published deliverable
(visibility inherited) or the literal "untested" — never invented.

Publish target: memory key `agents.<name>.offers`, owner visibility (the same key the
node's PUT /v1/agents/:name/offers route will own once it ships).
"""

from __future__ import annotations

import datetime
import sys
from zoneinfo import ZoneInfo

from crewaimeat.aimeat_crew import _aimeat_call

# Where these contracts are adopted today — used only to fetch a REAL deliverable sample.
_SAMPLE_ORG = "b784641b-a4dd-4d69-adb6-9954dc813e1e"
_SAMPLE_WS = "ws-mq5vvdgsjwp"
_SAMPLE_CHARS = 300

_BASE_REQUIREMENTS = [
    {"need": "organism membership", "fix": "join"},
    {"need": "adopted contract (input+output spaces in the workspace)", "fix": "adopt-contract"},
]

# Authored constants per CONTRACT id (title/ask/example/cost/latency/consequences).
# ask MUST include negative scope (hard rule 3).
_OFFER_META: dict[str, dict] = {
    "research": {
        "agent": "web-researcher",
        "title": "Research a topic from the live web",
        "ask": ("Write a research-request record (topic + optional focus) and I return a cited "
                "research note: searched, fetched, distilled, with source links. I don't do "
                "real-time prices, paywalled sources, or opinions presented as facts."),
        "example": "topic: 'EU AI Act obligations for small SaaS companies', focus: 'what applies before 2027'",
        "cost": "cheap", "latency": "minutes", "consequences": [],
    },
    "market-scan": {
        "agent": "web-researcher",
        "title": "Scan a market / competitor landscape",
        "ask": ("Write a market-scan-request (segment + region) and I return a structured scan: "
                "who plays, what they advertise and where, and how to sell against them. Built "
                "from public web sources only — I don't access private databases or paid reports."),
        "example": "segment: 'AI agent platforms and orchestration consulting', region: 'Helsinki metro'",
        "cost": "expensive", "latency": "long-running", "consequences": [],
    },
    "company-research": {
        "agent": "web-researcher",
        "title": "Research a Finnish company",
        "ask": ("Write a company-research request (name or business id) and I return a company "
                "profile: official registry data (PRH/YTJ), financials where published, web "
                "presence. Public sources only — no credit data, no people's personal details."),
        "example": "company: 'Validera Ab', focus: 'product, pricing, funding'",
        "cost": "expensive", "latency": "long-running", "consequences": [],
    },
    "activity-report": {
        "agent": "activity-reporter",
        "title": "Digest what happened in a workspace",
        "ask": ("Write an activity-tracking record (workspace + period) and I keep producing "
                "periodic digests: who did what, what shipped, narrated readably. I report what "
                "the activity feed shows — I don't audit content quality or verify claims."),
        "example": "ws: '*', period_hours: 168, narrator: 'dry, precise chief of staff'",
        "cost": "cheap", "latency": "minutes", "consequences": [],
    },
    "moodboard": {
        "agent": "image-scout",
        "title": "Curate a moodboard from an image brief",
        "ask": ("Write a moodboard-request (brief + image count) and I search the open web, "
                "vision-curate the candidates and deliver a gallery with metadata and source "
                "links. Internal reference use only — I don't generate images or clear licenses."),
        "example": "brief: 'retro-futuristic finnish newsroom, neon, crt monitors', n_images: 4",
        "cost": "cheap", "latency": "minutes",
        "consequences": [
            {"type": "publishes-public",
             "note": "curated images are stored under public storage keys so every workspace viewer can render them"},
        ],
    },
    "mail": {
        "agent": "postman",
        "title": "Send a mail from a workspace record",
        "ask": ("Write a mail-request record (subject + markdown body) and I send it over SMTP. "
                "Recipients are restricted to the owner-configured allowlist — I refuse any "
                "address outside it, and I don't fetch content or compose on my own."),
        "example": "subject: 'Weekly status', body_md: '## Done this week …'",
        "cost": "free", "latency": "seconds",
        "consequences": [
            {"type": "external-send",
             "note": "sends real email over SMTP; the AIMEAT_MAIL_TO allowlist is enforced on every send"},
        ],
    },
}


def _contracts():
    """All CONTRACT dicts on the OSS side, imported lazily (keeps import cost off the crews)."""
    from crewaimeat import (activity_contract, company_contract, image_contract,
                            mail_contract, market_contract, research_contract)
    return [m.CONTRACT for m in (research_contract, market_contract, company_contract,
                                 activity_contract, image_contract, mail_contract)]


def _spaces(contract: dict) -> tuple[dict | None, dict | None]:
    """(input records space, output space) — input = first records space WITH a schema,
    output = first document space, else the last records space."""
    spaces = contract.get("spaces") or []
    inp = next((s for s in spaces if s.get("mode") == "records" and s.get("schema")), None)
    out = next((s for s in spaces if s.get("mode") == "document"), None)
    if out is None:
        rec = [s for s in spaces if s.get("mode") == "records"]
        out = rec[-1] if rec else None
    return inp, out


def fetch_sample(agent: str, out_space: dict | None) -> str:
    """A REAL excerpt from the latest published deliverable in the adopted workspace,
    or the literal 'untested'. Never invented (hard rule 1)."""
    if not out_space:
        return "untested"
    try:
        data = _aimeat_call(agent, "aimeat_workspace_read",
                            {"organism_id": _SAMPLE_ORG, "ws": _SAMPLE_WS}) or {}
        items = (data.get("objects", {}) or {}).get(out_space["space"]) or []
        if not items:
            return "untested"
        last = items[-1]
        text = (last.get("markdown") or last.get("body_md")
                or str({k: v for k, v in last.items() if k != "markdown"}))
        excerpt = " ".join(str(text).split())[:_SAMPLE_CHARS]
        return excerpt + ("…" if len(str(text)) > _SAMPLE_CHARS else "")
    except Exception as exc:  # noqa: BLE001
        print(f"[offers] sample fetch failed for {agent}: {exc!r}", file=sys.stderr)
        return "untested"


def offer_from_contract(contract: dict, with_sample: bool = False) -> dict:
    """One §4-shaped offer, derived from the CONTRACT + the authored constants."""
    meta = _OFFER_META.get(contract.get("id") or "")
    if meta is None:
        raise KeyError(f"no offer metadata authored for contract id {contract.get('id')!r}")
    inp, out = _spaces(contract)
    deliverable_format = "document" if (out or {}).get("mode") == "document" else "record"
    offer = {
        "id": contract["id"],
        "title": meta["title"],
        "ask": meta["ask"],
        "example": meta["example"],
        "tags": ["role.workspace-contract"]
                + ([f"contract.{inp['space']}"] if inp else []),
        "cost": meta["cost"],
        "latency": meta["latency"],
        "repeatability": "idempotent",   # output-existence dedup is the contract convention
        "verification": "gated",          # records are schema-validated at the boundary
        "availability": {"boundToLastSeen": True, "scheduleBorn": None},
        "requirements": list(_BASE_REQUIREMENTS),
        "consequences": list(meta["consequences"]),
        "deliverable": {
            "format": deliverable_format,
            "location": {"space": (out or {}).get("namespace", ""), "visibility": "workspace"},
            "sample": fetch_sample(meta["agent"], out) if with_sample else "untested",
        },
    }
    return offer


def offers_doc(agent: str, with_samples: bool = False) -> dict:
    """The agents.<agent>.offers document for one agent (multi-contract agents get several offers)."""
    now = datetime.datetime.now(ZoneInfo("Europe/Helsinki")).isoformat()
    offers = [offer_from_contract(c, with_sample=with_samples)
              for c in _contracts() if _OFFER_META.get(c.get("id") or "", {}).get("agent") == agent]
    return {"version": 1, "updatedAt": now, "offers": offers}


def publish_offers(agent: str, with_samples: bool = True) -> bool:
    """Publish the derived offers to agents.<agent>.offers (owner visibility) — the same key
    the node's offers route will own; until it ships this direct write IS the seed."""
    doc = offers_doc(agent, with_samples=with_samples)
    if not doc["offers"]:
        print(f"[offers] {agent}: no contracts -> nothing to publish", file=sys.stderr)
        return False
    ok = bool(_aimeat_call(agent, "aimeat_memory_write",
                           {"key": f"agents.{agent}.offers", "visibility": "owner", "value": doc}))
    print(f"[offers] {agent}: {len(doc['offers'])} offer(s) {'published' if ok else 'PUBLISH FAILED'}")
    return ok


PILOT_AGENTS = ("web-researcher", "activity-reporter", "image-scout", "postman")


def publish_all(with_samples: bool = True) -> dict:
    return {agent: publish_offers(agent, with_samples=with_samples) for agent in PILOT_AGENTS}

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
import re
import sys
from zoneinfo import ZoneInfo

from aimeat_crewai.workflow_spec import NONE, Sig, is_workflow_compatible  # published grammar + compat check

from crewaimeat.aimeat_crew import _aimeat_call

# Where these contracts are adopted today — used only to fetch a REAL deliverable sample.
_SAMPLE_ORG = "b784641b-a4dd-4d69-adb6-9954dc813e1e"
_SAMPLE_WS = "ws-mq5vvdgsjwp"
_SAMPLE_CHARS = 700


def _md_excerpt(text: str, max_chars: int = _SAMPLE_CHARS) -> str:
    """A sample excerpt that PRESERVES Markdown line structure. Flattening newlines made the
    leading '#' swallow the whole sample as one giant heading and broke every table — headings,
    table rows and list items all need their own lines. Cut at a line boundary (never mid-row),
    normalize newlines, and mark the cut with an ellipsis line."""
    norm = str(text).replace("\r\n", "\n").replace("\r", "\n").strip()
    norm = re.sub(r"\n{3,}", "\n\n", norm)
    if len(norm) <= max_chars:
        return norm
    cut = norm[:max_chars]
    nl = cut.rfind("\n")
    if nl > max_chars // 2:
        cut = cut[:nl]
    return cut.rstrip() + "\n\n…"

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
        "dataHandling": "third-party",  # the record content leaves the platform as email
        "cost": "free", "latency": "seconds",
        "consequences": [
            {"type": "external-send",
             "note": "sends real email over SMTP; the AIMEAT_MAIL_TO allowlist is enforced on every send"},
        ],
    },
    "image-request": {
        "agent": "image-maker",
        "title": "Generate an image from a request record",
        "ask": ("Write an image-request record (prompt + optional size 0.5K-4K / aspect_ratio) and I "
                "generate the image with Seedream 4.5 and write it into an image-gallery document. "
                "Adopt the contract + write a record — no task or chat. I make NEW images; I don't edit "
                "existing files or post anywhere."),
        "example": "prompt: 'A retro-futuristic Finnish newsroom, neon, CRT monitors', size: '2K', aspect_ratio: '16:9'",
        "cost": "cheap", "latency": "minutes",
        "consequences": [
            {"type": "publishes-public",
             "note": "the generated image is stored at a public storage URL (~$0.04/image)"},
        ],
    },
}


def _contracts():
    """All CONTRACT dicts on the OSS side, imported lazily (keeps import cost off the crews)."""
    from crewaimeat import (activity_contract, company_contract, image_contract,
                            image_request_contract, mail_contract, market_contract, research_contract)
    return [m.CONTRACT for m in (research_contract, market_contract, company_contract,
                                 activity_contract, image_contract, image_request_contract, mail_contract)]


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
        return _md_excerpt(text)
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
        "dataHandling": meta.get("dataHandling", "llm-provider"),
        "availability": {"boundToLastSeen": True, "scheduleBorn": None},
        "requirements": list(_BASE_REQUIREMENTS),
        "consequences": list(meta["consequences"]),
        "deliverable": {
            "format": deliverable_format,
            "location": {"space": (out or {}).get("namespace", ""), "visibility": "workspace"},
            "sample": fetch_sample(meta["agent"], out) if with_sample else "untested",
        },
    }
    # Workflow-compatibility: derive the three signals from the contract's request→result spaces,
    # so a workflow step can name this offer. These contracts are request/event-driven (the output
    # is a workspace doc keyed by the request id), so the keys template {org}/{ws}/{request} per run
    # rather than {date}. v1 defaults — a workflow may override/strengthen per step. Only contracts
    # with a real DOCUMENT output get them (mail = external send, no deliverable key → stays a sink).
    in_ns = (inp or {}).get("namespace")
    out_ns = (out or {}).get("namespace")
    if in_ns and out_ns and (out or {}).get("mode") == "document" and in_ns != out_ns:
        wsk = "organism.{org}.w.{ws}."
        offer["required_to_function"] = Sig.exists(key_glob=wsk + in_ns + ".{request}.*")
        offer["success_signal"] = Sig.exists(key_glob=wsk + out_ns + ".{request}.*")
        offer["deliverable"]["location"]["key"] = wsk + out_ns + ".{request}.latest"
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


# ──────────────────────────────────────────────────────────────────────────────
# Task-runner crew offers — authored constants for OUR OWN crews (we know exactly
# what each does, so no LLM generation needed; owner-gated by being code-reviewed).
# These crews take a TASK (the node's Run flow) and publish the deliverable to the
# memory prefix crews.<agent>. — so the sample is fetched from the latest real one.
# Most are `accumulative`: every ask produces a NEW deliverable (no output-dedup).
# ──────────────────────────────────────────────────────────────────────────────

_CREW_OFFERS: dict[str, list[dict]] = {
    "crew-forge": [
        {"id": "build-crew", "title": "Build a new agent from a description",
         "ask": ("Send '/build <description>' as a task and I design the crew, write and validate its "
                 "build_domain, register the agent and launch it under the watchdog. You approve one "
                 "device code. I don't build AIMEAT apps or extensions — that's aimeat-crew-forge."),
         "example": "/build a crew that summarizes RSS feeds into a weekly digest",
         "cost": "expensive", "latency": "long-running", "repeatability": "accumulative",
         "verification": "gated",  # build_domain is validated + registration must succeed
         "consequences": [
             {"type": "creates-agent", "persistent": True, "requiresApproval": True,
              "note": "registers a NEW persistent agent; blocks on a device-code approval"},
             {"type": "mutates-host", "note": "launches a watchdog + daemon process on the operator machine"},
         ]},
        {"id": "fleet-status", "title": "Show which crews are running",
         "ask": ("Send '/list' (or '/status') and I report your crews and which are running. "
                 "Read-only — I don't start or stop anything for this offer."),
         "example": "/list",
         "cost": "free", "latency": "seconds", "repeatability": "idempotent",
         "verification": "gated", "consequences": []},
    ],
    "workflow-manager": [
        {"id": "orchestrate-goal", "title": "Fan a goal out to the fleet and synthesize",
         "ask": ("Give me a goal and I decompose it, delegate the parts to the best-rated crews, gather "
                 "the results and synthesize one deliverable. I pick delegates at runtime by reputation — "
                 "I don't execute domain work myself."),
         "example": "Compare three approaches for monetizing the newspaper showcase and recommend one",
         "cost": "expensive", "latency": "long-running", "repeatability": "accumulative",
         "verification": "ungated",
         "consequences": [
             {"type": "delegates-to-agent", "dynamic": True,
              "note": "creates tasks for other crews and RATES their work afterwards (verify-grounded)"},
         ]},
    ],
    "joker": [
        {"id": "tell-jokes", "title": "Four comedians riff on your topic",
         "ask": ("Give me a topic and four comedian personas each riff on it; a host presents the set. "
                 "Humor only — I don't write marketing copy or serious prose."),
         "example": "aihe: etätyöpalaverit", "cost": "cheap", "latency": "minutes",
         "repeatability": "accumulative", "verification": "ungated", "consequences": []},
    ],
    "joker-v2": [
        {"id": "tell-jokes-v2", "title": "Comedians draft many, keep the best (evolved variant)",
         "ask": ("Same job as joker, evolved: each comedian drafts several jokes and only the best "
                 "survive. Part of a live A/B pair — humor only, nothing serious."),
         "example": "aihe: tekoälyagenttien kokouskäytännöt", "cost": "cheap", "latency": "minutes",
         "repeatability": "accumulative", "verification": "ungated", "consequences": []},
    ],
    "sanity-checker": [
        {"id": "stress-test-idea", "title": "Stress-test an idea from multiple angles",
         "ask": ("Give me an idea or plan and I attack it from several angles (feasibility, risks, "
                 "blind spots), then advise. I challenge — I don't rubber-stamp or implement."),
         "example": "Idea: sell organism exports as onboarding accelerators — what breaks?",
         "cost": "cheap", "latency": "minutes", "repeatability": "accumulative",
         "verification": "ungated", "consequences": []},
    ],
    "idea-feasibility-rater": [
        {"id": "rate-feasibility", "title": "Rate an idea's feasibility",
         "ask": ("Give me an idea and I return a structured feasibility rating with reasoning. "
                 "A judgment, not a build plan — I don't implement anything."),
         "example": "Idea: per-customer private AIMEAT nodes with a managed-hosting tier",
         "cost": "cheap", "latency": "minutes", "repeatability": "accumulative",
         "verification": "ungated", "consequences": []},
    ],
    "probability-creator": [
        {"id": "estimate-spectrum", "title": "Turn one question into an estimate spectrum",
         "ask": ("Ask one estimation question and I return a spectrum of answers with probabilities "
                 "and assumptions made explicit. Estimates, not guarantees — no financial advice."),
         "example": "How many Finnish SMEs adopt an AI 'digital employee' service by 2028?",
         "cost": "cheap", "latency": "minutes", "repeatability": "accumulative",
         "verification": "ungated", "consequences": []},
    ],
    "jingle-writer": [
        {"id": "write-jingle", "title": "Write a jingle or short creative copy",
         "ask": ("Give me a product or theme and I write a jingle / short creative copy. "
                 "Short-form creative only — I don't write long articles or technical docs."),
         "example": "Jingle for a morning report that arrives before you wake up",
         "cost": "cheap", "latency": "minutes", "repeatability": "accumulative",
         "verification": "ungated", "consequences": []},
    ],
    "web-tester": [
        {"id": "test-web-flow", "title": "Drive a real browser through a web flow",
         "ask": ("Give me a URL and a flow (login, form, navigation) and I drive a real browser "
                 "through it and report what happened with evidence. I interact with the page — "
                 "point me at test data, not production-critical state."),
         "example": "Test that the public newspaper page renders and the quiz accepts answers",
         "cost": "cheap", "latency": "minutes", "repeatability": "accumulative",
         "verification": "gated",
         "consequences": [
             {"type": "mutates-live-app",
              "note": "clicks and types against the target; interactions can change app state"},
         ]},
    ],
    "librarian": [
        {"id": "map-knowledge", "title": "Map the fleet's deliverables and reuse",
         "ask": ("Ask me what the fleet knows about a theme and I scan every same-owner deliverable "
                 "and return an index with reuse pointers and freshness. I read and map — "
                 "I don't produce new domain content."),
         "example": "What do we already have about onboarding flows?",
         "cost": "cheap", "latency": "minutes", "repeatability": "accumulative",
         "verification": "ungated", "consequences": []},
    ],
    "aimeat-app-conductor": [
        {"id": "build-or-fix-app", "title": "Route an app idea to the right SDLC specialist",
         "ask": ("Describe what you want built, edited or fixed on AIMEAT and I route it to the right "
                 "specialist (builder / editor / cortex-fixer / realtime) and report the outcome. "
                 "I coordinate — I don't write the app code myself."),
         "example": "Build a simple notes app with login for my profile",
         "cost": "expensive", "latency": "long-running", "repeatability": "accumulative",
         "verification": "gated",  # builds complete only through the verify gates
         "consequences": [
             {"type": "delegates-to-agent", "dynamic": True, "note": "routes to the SDLC specialist crews"},
             {"type": "mutates-live-app", "note": "the routed specialist publishes/edits a live app"},
         ]},
    ],
    "aimeat-app-builder": [
        {"id": "build-app", "title": "Build a working AIMEAT app from a description",
         "ask": ("Describe the app and I author it directly into a live, working AIMEAT app on the "
                 "starter template, verified with the render gate. I don't design visual themes "
                 "(aimeat-app-designer) or build realtime channels (aimeat-realtime-builder)."),
         "example": "A checklist app that saves items per user and works for anonymous viewers read-only",
         "cost": "expensive", "latency": "long-running", "repeatability": "accumulative",
         "verification": "gated",
         "consequences": [{"type": "mutates-live-app", "note": "publishes a new app/version under your profile"}]},
    ],
    "aimeat-app-editor": [
        {"id": "edit-app", "title": "Make a surgical edit to an existing app",
         "ask": ("Name the app and the change and I read its live stack, apply an in-place edit and "
                 "verify the render. Small targeted changes only — full rebuilds go to aimeat-app-builder."),
         "example": "Add a 'Reset scores' button to tictactoe.html",
         "cost": "cheap", "latency": "minutes", "repeatability": "accumulative",
         "verification": "gated",
         "consequences": [{"type": "mutates-live-app", "note": "publishes a new version of the existing app"}]},
    ],
    "daily-briefing-crew": [
        {"id": "daily-briefing", "title": "Compose a briefing on demand",
         "ask": ("Ask for a briefing (topic or general) and I compose one from available sources. "
                 "A summary for humans — I don't make decisions or take actions."),
         "example": "Brief me on this week's fleet activity and open questions",
         "cost": "cheap", "latency": "minutes", "repeatability": "accumulative",
         "verification": "ungated", "consequences": []},
    ],
    "finnish-corporate-researcher": [
        {"id": "research-fi-company", "title": "Profile a Finnish company (registry-grounded)",
         "ask": ("Give me a Finnish company name or business id and I build a profile from official "
                 "registries and public web. Public sources only — no credit ratings, no personal data."),
         "example": "Profile: Supercell Oy — ownership, financials trend, public footprint",
         "cost": "expensive", "latency": "long-running", "repeatability": "accumulative",
         "verification": "ungated", "consequences": []},
    ],
    "space-weather-writer": [
        {"id": "space-weather", "title": "Space-weather article from NOAA/NASA data",
         "ask": ("I write the day's space-weather article from official NOAA/NASA feeds. Runs on the "
                 "evening schedule by itself — ask only for an extra/edition re-run. I don't forecast "
                 "beyond what the source data says."),
         "example": "Re-run today's space weather article (evening edition)",
         "cost": "cheap", "latency": "minutes", "repeatability": "accumulative",
         "verification": "ungated", "scheduleBorn": "daily 17:00 Europe/Helsinki — runs automatically",
         "consequences": [{"type": "publishes-public", "note": "the article is public newspaper content"}]},
    ],
    "tagline-translator": [
        {"id": "tagline-or-translation", "title": "Write a tagline or translate a short text",
         "ask": ("Give me a product/theme for a tagline, or a short text + target language. "
                 "Short-form only — no long documents, no legal translation."),
         "example": "Translate to English, keep the tone: 'Muisti joka ei vanhene'",
         "cost": "free", "latency": "seconds", "repeatability": "accumulative",
         "verification": "ungated", "consequences": []},
    ],
    "image-maker": [
        {"id": "generate-image", "title": "Generate an image from a description",
         "ask": ("Describe an image (subject, style, mood, composition) and I generate one with "
                 "ByteDance Seedream 4.5 and return a public URL. I make NEW images — I don't edit "
                 "your existing files, lay out multi-page designs, or post anywhere."),
         "example": "A serene Finnish lakeside summer cottage at golden hour, soft watercolor style",
         "cost": "cheap", "latency": "minutes", "repeatability": "accumulative",
         "verification": "deterministic",  # generation + presigned upload are code; output is a real stored image
         "consequences": [{"type": "publishes-public",
                           "note": "the generated image is stored at a public storage URL (~$0.04/image)"}]},
    ],
    "editorial-writer": [
        {"id": "evening-editorial", "title": "The gonzo S.J. editorial + front-page index",
         "ask": ("I write the savage daily editorial from the day's articles and rebuild the public "
                 "front-page index. Runs on the 18:00 schedule with a self-heal guard — ask only to "
                 "re-run a date/edition. I don't write polite corporate prose."),
         "example": "Re-run the editorial + index for 2026-06-11 evening",
         "cost": "cheap", "latency": "minutes", "repeatability": "accumulative",
         "verification": "deterministic",  # index build + verbatim store are code; self-heal checks output existence
         "scheduleBorn": "daily 18:00 Europe/Helsinki — runs automatically (self-healing at 18:15)",
         "consequences": [{"type": "publishes-public", "note": "editorial + front-page index are public newspaper content"}]},
    ],
    "news-fetcher": [
        {"id": "fetch-edition-raw", "title": "Fetch the day's raw news per category",
         "ask": ("I pull curated feeds + search and ALWAYS run full-text extraction per category into "
                 "the edition raw. Runs on the 17:00 schedule — ask only for a re-fetch. "
                 "I fetch and extract; I don't write articles."),
         "example": "Re-fetch the evening raw for today",
         "cost": "cheap", "latency": "minutes", "repeatability": "accumulative",
         "verification": "deterministic",  # the loop and extraction are code; no LLM in the fetch
         "scheduleBorn": "daily 17:00 Europe/Helsinki — runs automatically",
         "consequences": []},
    ],
    "news-writer": [
        {"id": "evening-write-a", "title": "Write the Desk A news articles",
         "ask": ("I write a full Finnish article for every Desk A category that has raw (talous, "
                 "politiikka, urheilu, kulttuuri, tiede, terveys, …) from the day's scraped material. "
                 "Runs on the evening schedule — ask only for a re-run. I write from the raw; "
                 "I don't fetch sources or build the front page."),
         "example": "Re-write today's Desk A articles (evening edition)",
         "cost": "cheap", "latency": "minutes", "repeatability": "accumulative",
         "verification": "deterministic",  # the category loop is code; grok writes each article
         "scheduleBorn": "daily ~17:25 Europe/Helsinki — runs automatically",
         "consequences": [{"type": "publishes-public", "note": "the articles are public newspaper content"}]},
    ],
    "news-writer-b": [
        {"id": "evening-write-b", "title": "Write the Desk B news articles",
         "ask": ("I write a full Finnish article for every Desk B category that has raw (tekoäly, "
                 "pelit, pelidevaus, startup, ruoka, luonto, mieli, filosofia, …) from the day's "
                 "scraped material. Runs on the evening schedule — ask only for a re-run. I write "
                 "from the raw; I don't fetch sources or build the front page."),
         "example": "Re-write today's Desk B articles (evening edition)",
         "cost": "cheap", "latency": "minutes", "repeatability": "accumulative",
         "verification": "deterministic",
         "scheduleBorn": "daily ~17:25 Europe/Helsinki — runs automatically",
         "consequences": [{"type": "publishes-public", "note": "the articles are public newspaper content"}]},
    ],
    "feedback-wisdom": [
        {"id": "feedback-wisdom", "title": "Turn feedback stats into support guidance",
         "ask": ("I read the Feedback Desk's published statistics (`feedback-stats@1`) and write "
                 "operational advisories (`support-advisory@1`) — rising tag, slow resolution, poor "
                 "tagging, slow per-tag, VIP pressure — each citing the exact stat movement, to the "
                 "AIMEAT advisory outbox for owner-gated delivery. I reason over aggregates; I don't "
                 "read raw feedback or deliver to the app directly."),
         "example": "Produce today's support advisories from the latest published feedback stats",
         "cost": "cheap", "latency": "minutes", "repeatability": "idempotent",
         "verification": "deterministic",  # rules + outbox writes are code; stable ids make re-runs idempotent
         "consequences": [
             {"type": "mutates-live-app",
              "note": "advisories are written to the AIMEAT outbox; after OWNER-GATED AIMEAT delivery "
                      "(deliver-advisory) they appear in the app's Guidance tab — indirect, never a direct write"},
         ]},
    ],
    "daily-features-writer": [
        {"id": "evening-features", "title": "Evening features + the validated news quiz",
         "ask": ("I write the koodaus/prompt/matikka features and build the news quiz from the day's "
                 "articles (validated; skipped rather than fabricated when articles are missing). "
                 "Runs on the 17:45 schedule — ask only for a re-run."),
         "example": "Rebuild today's quiz (evening edition)",
         "cost": "cheap", "latency": "minutes", "repeatability": "accumulative",
         "verification": "gated",  # quiz JSON is structurally validated; placeholder output rejects
         "scheduleBorn": "daily 17:45 Europe/Helsinki — runs automatically (quiz self-heal at 18:00)",
         "consequences": [{"type": "publishes-public", "note": "features + quiz are public newspaper content"}]},
    ],
}


def fetch_crew_sample(agent: str) -> str:
    """Latest real deliverable excerpt from the crew's memory prefix crews.<agent>. —
    or 'untested'. Same hard rule as contracts: never invented."""
    try:
        r = _aimeat_call(agent, "aimeat_memory_list",
                         {"owner_scope": True, "prefix": f"crews.{agent}.", "limit": 50}) or {}
        items = r.get("items") or []
        if not items:
            return "untested"
        last = items[-1]
        v = last.get("value")
        if not v:
            v = (_aimeat_call(agent, "aimeat_memory_read", {"key": last.get("key")}) or {}).get("value")
        if not v:
            return "untested"
        text = v if isinstance(v, str) else str(v)
        return _md_excerpt(text)
    except Exception as exc:  # noqa: BLE001
        print(f"[offers] crew sample fetch failed for {agent}: {exc!r}", file=sys.stderr)
        return "untested"


# Prose task-runner offers to make workflow-compatible with a GENERIC signal (output exists under
# crews.<agent>.). They produce free prose, not a run-keyed deliverable, so the signal is weak — it
# only makes the agent selectable as a workflow step; a real workflow overrides it. Orchestrators
# (crew-forge, workflow-manager), read-only offers (fleet-status), and app-SDLC crews are excluded.
_GENERIC_WORKFLOW_OFFERS = {
    "tell-jokes", "tell-jokes-v2", "write-jingle", "stress-test-idea", "rate-feasibility",
    "estimate-spectrum", "tagline-or-translation", "daily-briefing", "map-knowledge",
    "research-fi-company", "generate-image",
}


def crew_offer(agent: str, meta: dict, with_sample: bool = False) -> dict:
    """One spec-shaped offer for a task-runner crew (deliverable = memory prefix, Run flow)."""
    offer = {
        "id": meta["id"],
        "title": meta["title"],
        "ask": meta["ask"],
        "example": meta["example"],
        "tags": ["role.task-runner"],
        "cost": meta["cost"],
        "latency": meta["latency"],
        "repeatability": meta["repeatability"],
        "verification": meta["verification"],
        "dataHandling": meta.get("dataHandling", "llm-provider"),
        "availability": {"boundToLastSeen": True, "scheduleBorn": meta.get("scheduleBorn")},
        "requirements": [],  # a registered+approved task-runner needs nothing else
        "consequences": list(meta["consequences"]),
        "deliverable": {
            "format": "document",
            "location": {"space": f"crews.{agent}.", "visibility": "owner"},
            "sample": fetch_crew_sample(agent) if with_sample else "untested",
        },
    }
    # Workflow-compatibility: an offer that declares its signals can be wired into a workflow step.
    # The signals carry {date}/{edition} placeholders the workflow templates per run. Single source:
    # crewaimeat.workflow_spec.AGENT_SIGNALS (also consumed by the workflow definition itself). The
    # node treats an agent as workflow-compatible exactly when its offer publishes all three:
    # success_signal + required_to_function + deliverable.location.key.
    from crewaimeat.workflow_spec import AGENT_SIGNALS
    sig = AGENT_SIGNALS.get(meta["id"])
    if sig:
        # The node now accepts required_to_function:"none" at the OFFER level (not just the step), so
        # a source offer publishes it directly — no placeholder workaround needed.
        offer["required_to_function"] = sig["required_to_function"]
        offer["success_signal"] = sig["success_signal"]
        loc = sig.get("deliverable_location")
        if loc and loc.get("key"):
            offer["deliverable"]["location"]["key"] = loc["key"]  # the memory key it writes (node blueprint)
    elif meta["id"] in _GENERIC_WORKFLOW_OFFERS:
        # Generic workflow-compatibility for prose task-runners: their deliverable is free prose under
        # crews.<agent>. — no run-keyed output, so the signal is a weak "produces output here" — enough
        # to make the agent SELECTABLE as a workflow step; a real workflow overrides per step.
        offer["required_to_function"] = NONE
        offer["success_signal"] = Sig.count_nonempty(key_glob=f"crews.{agent}.*", min=1)
        offer["deliverable"]["location"]["key"] = f"crews.{agent}."
    return offer


PILOT_AGENTS = ("web-researcher", "activity-reporter", "image-scout", "postman")
CREW_AGENTS = tuple(_CREW_OFFERS)


def offers_doc_any(agent: str, with_samples: bool = False) -> dict:
    """Offers doc for ANY agent: contract-derived + authored crew offers, merged."""
    doc = offers_doc(agent, with_samples=with_samples)
    sample = None  # fetch the crew sample once per agent, all its offers share the prefix
    for meta in _CREW_OFFERS.get(agent, ()):
        o = crew_offer(agent, meta, with_sample=False)
        if with_samples:
            if sample is None:
                sample = fetch_crew_sample(agent)
            o["deliverable"]["sample"] = sample
        doc["offers"].append(o)
    return doc


def publish_offers_any(agent: str, with_samples: bool = True) -> bool:
    """Publish through PUT /v1/agents/:name/offers — the node-validated route (live on
    aimeat.io since 2026-06-12). Falls back LOUDLY to the direct memory write only when
    the route is absent (older/self-hosted node)."""
    doc = offers_doc_any(agent, with_samples=with_samples)
    if not doc["offers"]:
        print(f"[offers] {agent}: nothing to publish", file=sys.stderr)
        return False
    n_wf = sum(1 for o in doc["offers"] if is_workflow_compatible(o))  # package's node-mirrored check
    if n_wf:
        print(f"[offers] {agent}: {n_wf}/{len(doc['offers'])} offer(s) workflow-compatible")
    try:
        import requests
        from crewaimeat.generator_tool import _discover_owner, _token
        tok, url = _token(agent, _discover_owner(agent))
        r = requests.put(f"{url.rstrip('/')}/v1/agents/{agent}/offers",
                         json={"offers": doc["offers"]},
                         headers={"Authorization": f"Bearer {tok}"}, timeout=60)
        if r.status_code == 200:
            print(f"[offers] {agent}: {len(doc['offers'])} offer(s) published via route")
            return True
        if r.status_code != 404:  # validation/auth errors are REAL failures — surface, don't mask
            print(f"[offers] {agent}: route FAILED HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
            return False
        print(f"[offers] {agent}: route 404 (node without offers) -> direct memory write", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"[offers] {agent}: route unreachable ({exc!r}) -> direct memory write", file=sys.stderr)
    ok = bool(_aimeat_call(agent, "aimeat_memory_write",
                           {"key": f"agents.{agent}.offers", "visibility": "owner", "value": doc}))
    print(f"[offers] {agent}: {len(doc['offers'])} offer(s) {'published (direct)' if ok else 'PUBLISH FAILED'}")
    return ok


def publish_all(with_samples: bool = True) -> dict:
    agents = dict.fromkeys(PILOT_AGENTS + CREW_AGENTS)
    return {agent: publish_offers_any(agent, with_samples=with_samples) for agent in agents}

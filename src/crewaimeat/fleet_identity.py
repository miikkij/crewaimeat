"""Per-agent capability identity — the SPECIFIC tags + capabilities each fleet agent advertises.

Single source of truth so the scaffold (run_crew) advertises what each agent ACTUALLY does — the
ecosystem-app picker's matcher reads tags + technical_capabilities + domain_capabilities — instead of
the liaison's generic Hello-Integration defaults ("AIMEAT coordination" / "task lifecycle management",
implied by onboarding anyway). On every start run_crew sets tags (aimeat_agent_tags_set) and reports
capabilities (aimeat_agent_capabilities_report, which OVERWRITES the set). A crew's own
CrewSpec.tags/.capabilities take precedence over an entry here.

Conventions: tags charset is [a-z0-9._-] only (NO ':' or '@'); domain strings may carry ':'/'@'
(e.g. "consumes:ledger-request"). Derived from each agent's offer/contract/README — see
docs/internal/agent-tags-capabilities-proposal.md. NB feedback-wisdom declares its identity inline in
its crew (the precedent), so it is intentionally absent here.
"""

from __future__ import annotations


def _skill(name: str) -> dict:
    return {"name": name, "type": "skill"}


FLEET_IDENTITY: dict[str, dict] = {
    # ── estimation / judgement / critique ──
    "probability-creator": {
        "tags": ["probability-estimation", "forecasting", "role.task-runner"],
        "capabilities": {
            "technical": [_skill("probability-creator")],
            "domain": ["probability estimation", "forecasting", "scenario spectrums with explicit assumptions"],
            "languages": ["en"],
        },
    },
    "sanity-checker": {
        "tags": ["idea-stress-test", "critique", "role.task-runner"],
        "capabilities": {
            "technical": [_skill("sanity-checker")],
            "domain": ["idea stress-testing", "risk + blind-spot analysis", "feasibility critique"],
            "languages": ["en"],
        },
    },
    "idea-feasibility-rater": {  # domain already specific — tags only
        "tags": ["idea-feasibility", "startup-evaluation", "role.task-runner"]
    },
    # ── creative ──
    "joker": {
        "tags": ["humor", "comedy", "role.task-runner"],
        "capabilities": {
            "technical": [_skill("joker")],
            "domain": ["comedy writing", "multi-persona riffing"],
            "languages": ["fi", "en"],
        },
    },
    "joker-v2": {
        "tags": ["humor", "comedy", "variant.ab", "role.task-runner"],
        "capabilities": {
            "technical": [_skill("joker-v2")],
            "domain": ["comedy writing", "draft-many-keep-best (evolved A/B variant)"],
            "languages": ["fi", "en"],
        },
    },
    "jingle-writer": {
        "tags": ["jingle", "creative-copy", "role.task-runner"],
        "capabilities": {
            "technical": [_skill("jingle-writer")],
            "domain": ["jingle writing", "short-form creative copy"],
            "languages": ["fi", "en"],
        },
    },
    "tagline-translator": {
        "tags": ["tagline", "translation", "localization", "role.task-runner"],
        "capabilities": {
            "technical": [_skill("tagline-translator")],
            "domain": [
                "marketing tagline translation EN -> FR + DE",
                "idiomatic localization preserving tone + brevity",
                "bilingual QA review",
            ],
            "languages": ["en", "fr", "de"],
        },
    },
    # ── infra / orchestration / build ──
    "crew-forge": {
        "tags": ["agent-builder", "fleet-management", "role.task-runner"],
        "capabilities": {
            "technical": [_skill("crew-forge")],
            "domain": [
                "builds new CrewAI agents from a description",
                "fleet reconcile + launch under watchdog",
                "agent lifecycle management",
            ],
            "languages": ["en"],
        },
    },
    "workflow-manager": {
        "tags": ["orchestration", "delegation", "reputation-routing", "role.task-runner"],
        "capabilities": {
            "technical": [_skill("workflow-manager")],
            "domain": ["goal decomposition", "delegation to best-rated crews", "synthesis", "reputation-based routing"],
            "languages": ["en"],
        },
    },
    "workflow-inspector": {
        "tags": ["workflow-inspection", "diagnosis", "role.task-runner"],
        "capabilities": {
            "technical": [_skill("workflow-inspector")],
            "domain": ["workflow run inspection: diagnose / auto-repair / escalate", "per-step signal health"],
            "languages": ["en"],
        },
    },
    "librarian": {
        "tags": ["knowledge-index", "reuse", "role.task-runner"],
        "capabilities": {
            "technical": [_skill("librarian")],
            "domain": ["fleet deliverable mapping", "reuse pointers + freshness", "knowledge management"],
            "languages": ["en"],
        },
    },
    "web-tester": {
        "tags": ["web-testing", "browser-automation", "role.task-runner"],
        "capabilities": {
            "technical": [_skill("web-tester"), _skill("playwright")],
            "domain": ["browser-driven web-flow testing (Playwright)", "evidence capture"],
            "languages": ["en"],
        },
    },
    # ── AIMEAT app-SDLC family ──
    "aimeat-app-conductor": {
        "tags": ["app-sdlc", "routing", "orchestration", "role.task-runner"],
        "capabilities": {
            "technical": [_skill("aimeat-app-conductor")],
            "domain": ["routes app build/edit/fix to the right SDLC specialist", "verify-gated completion"],
            "languages": ["en"],
        },
    },
    "aimeat-app-builder": {
        "tags": ["app-build", "aimeat-apps", "role.task-runner"],
        "capabilities": {
            "technical": [_skill("aimeat-app-builder")],
            "domain": ["builds AIMEAT apps on the starter template", "render-gated authoring"],
            "languages": ["en"],
        },
    },
    "aimeat-app-editor": {
        "tags": ["app-edit", "aimeat-apps", "role.task-runner"],
        "capabilities": {
            "technical": [_skill("aimeat-app-editor")],
            "domain": ["surgical in-place edits to existing AIMEAT apps", "render-verified edits"],
            "languages": ["en"],
        },
    },
    "aimeat-app-designer": {
        "tags": ["app-design", "aimeat-apps", "role.task-runner"],
        "capabilities": {
            "technical": [_skill("aimeat-app-designer")],
            "domain": ["visual theme / UI design for AIMEAT apps"],
            "languages": ["en"],
        },
    },
    "aimeat-app-specs-designer": {
        "tags": ["app-specs", "aimeat-apps", "role.task-runner"],
        "capabilities": {
            "technical": [_skill("aimeat-app-specs-designer")],
            "domain": ["app spec / blueprint design"],
            "languages": ["en"],
        },
    },
    "aimeat-cortex-fixer": {
        "tags": ["cortex-fix", "aimeat-apps", "role.task-runner"],
        "capabilities": {
            "technical": [_skill("aimeat-cortex-fixer")],
            "domain": ["fixes AIMEAT app cortex manifests"],
            "languages": ["en"],
        },
    },
    "aimeat-realtime-builder": {
        "tags": ["realtime", "app-build", "aimeat-apps", "role.task-runner"],
        "capabilities": {
            "technical": [_skill("aimeat-realtime-builder")],
            "domain": ["builds realtime channels / presence for AIMEAT apps"],
            "languages": ["en"],
        },
    },
    "aimeat-extension-builder": {
        "tags": ["extension-build", "aimeat-apps", "role.task-runner"],
        "capabilities": {
            "technical": [_skill("aimeat-extension-builder")],
            "domain": ["builds AIMEAT extensions"],
            "languages": ["en"],
        },
    },
    "aimeat-crew-forge": {
        "tags": ["app-forge", "aimeat-apps", "role.task-runner"],
        "capabilities": {
            "technical": [_skill("aimeat-crew-forge")],
            "domain": ["builds AIMEAT apps + extensions (app SDLC forge)"],
            "languages": ["en"],
        },
    },
    # ── content / laimeat ──
    "editorial-writer": {
        "tags": ["editorial", "opinion-writing", "laimeat", "role.task-runner"],
        "capabilities": {
            "technical": [_skill("editorial-writer")],
            "domain": [
                "gonzo editorial / opinion writing",
                "public front-page index",
                "consumes the day's articles -> produces the editorial",
            ],
            "languages": ["fi", "en"],
        },
    },
    "daily-features-writer": {
        "tags": ["features", "news-quiz", "laimeat", "role.task-runner"],
        "capabilities": {
            "technical": [_skill("daily-features-writer")],
            "domain": ["daily features + news quiz from the day's articles"],
            "languages": ["fi", "en"],
        },
    },
    "daily-briefing-crew": {"tags": ["briefing", "news-aggregation", "laimeat", "role.task-runner"]},
    "news-writer": {"tags": ["news-writing", "laimeat", "role.task-runner"]},
    "news-writer-b": {"tags": ["news-writing", "laimeat", "role.task-runner"]},
    "news-fetcher": {"tags": ["news-fetch", "laimeat", "role.task-runner"]},
    "space-weather-writer": {"tags": ["space-weather", "laimeat", "role.task-runner"]},
    "finnish-corporate-researcher": {
        "tags": ["company-research", "finland", "registry-research", "role.task-runner"],
        "capabilities": {
            "technical": [_skill("finnish-corporate-researcher")],
            "domain": ["Finnish company profiling from official registries", "registry-grounded research"],
            "languages": ["fi", "en"],
        },
    },
    # ── workspace-contract / messaging / Company Brain ──
    "postman": {
        "tags": ["mail", "notifications", "role.workspace-contract", "contract.mail"],
        "capabilities": {
            "technical": [_skill("workspace-contract"), _skill("postman")],
            "domain": ["deterministic email-out (allowlist-enforced)", "07:00 morning report", "mail-request contract"],
            "languages": ["en"],
        },
    },
    "some-listener": {
        "tags": ["social-radar", "role.task-runner"],
        "capabilities": {
            "technical": [_skill("some-listener")],
            "domain": ["social radar: source HN/X/Reddit engagement opportunities"],
            "languages": ["en"],
        },
    },
    "some-analyst": {
        "tags": ["social-radar", "reply-drafting", "role.task-runner"],
        "capabilities": {
            "technical": [_skill("some-analyst")],
            "domain": ["drafts reply suggestions for social-radar opportunities"],
            "languages": ["en"],
        },
    },
    "ledger-reader": {  # Company Brain — bank statements -> facts, on the company's own machine
        "tags": [
            "ledger",
            "company-brain",
            "financial-extraction",
            "role.workspace-contract",
            "contract.ledger-request",
        ],
        "capabilities": {
            "technical": [_skill("workspace-contract"), _skill("ledger-reader")],
            "domain": [
                "bank statements (camt.052/.053) -> Company Brain facts, on the company's machine",
                "deterministic, source-referenced, fail-loud (nothing guessed)",
                "consumes:ledger-request",
            ],
            "languages": ["en"],
        },
    },
    "doc-fact-reader": {  # Company Brain — documents -> facts + commitments, on the company's own machine
        "tags": ["documents", "company-brain", "fact-extraction", "role.workspace-contract", "contract.doc-request"],
        "capabilities": {
            "technical": [_skill("workspace-contract"), _skill("doc-fact-reader")],
            "domain": [
                "documents (txt/md/pdf/html) -> Company Brain facts + commitments, locally",
                "strictly validated, source-referenced, nothing inferred",
                "consumes:doc-request",
            ],
            "languages": ["en"],
        },
    },
}


def identity_for(agent: str) -> dict:
    """The {tags?, capabilities?} for an agent, or {} if it has no curated identity."""
    return FLEET_IDENTITY.get(agent, {})

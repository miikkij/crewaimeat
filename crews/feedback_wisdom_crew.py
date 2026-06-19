"""feedback-wisdom: the agent that CLOSES the Feedback Desk loop on the AIMEAT scaffold (crewaimeat).

The Feedback Desk app PRODUCES refined statistics (`feedback-stats@1`) into AIMEAT; this wisdom agent
CONSUMES them, reasons, and PRODUCES operational guidance (`support-advisory@1`) back — so support
staff get advance warning, process changes, and known-issue status derived from the data.

Two surfaces (same idempotent engine, crewaimeat.feedback_wisdom_contract):
  - TASK-RUNNER (this crew): on a Run, an analyst reads the produced stats, applies DETERMINISTIC
    rules to pick the advisories, phrases each nicely, and writes them to the AIMEAT advisory OUTBOX
    (`eco.feedback-desk.advisory.outbox.<id>`) + mirrors the visible chain into the workspace. AIMEAT
    then gates + delivers each approved advisory into the app via its `deliver-advisory` capability.
  - WORKSPACE CHAIN: adopt the `feedback-wisdom` contract into a workspace (e.g. `support-ops/wisdom`)
    to see INPUT (feedback-stats) → OUTPUT (support-advisory) side by side, each rationale citing the
    numbers. The idle poll keeps producing advisories from any fresh stats with no task or chat needed.

Register + approve before running:
  npx aimeat@latest connect add --agent feedback-wisdom --mode task-runner --url https://aimeat.io --owner <your-aimeat-account>

Run: uv run python crews/feedback_wisdom_crew.py
"""

from __future__ import annotations

import json

from crewaimeat import feedback_wisdom_contract as fw
from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.contract_adopt import build_adopt_domain, ensure_routed_workspaces, is_adopt_task, merge_targets

AGENT_NAME = "feedback-wisdom"

# Capability TAGS + a SPECIFIC capability report so AIMEAT's ecosystem-app agent picker recommends this
# agent for the feedback-desk recipe by TAG (not only by exact name). The feedback-desk manifest matches
# on `feedback-analysis` (a charset-safe tag) and `consumes:feedback-stats@1` / `produces:support-advisory@1`
# — the versioned ids carry ':'/'@' which tags reject, so they ride the DOMAIN capabilities (the matcher
# reads technical + domain). We report specific capabilities over the liaison's generic defaults.
CAPABILITY_TAGS = [
    "feedback-analysis",
    "role.workspace-contract",
    "consumes.feedback-stats",
    "produces.support-advisory",
]
CAPABILITIES = {
    "technical": [
        {"name": "workspace-contract", "type": "skill"},
        {"name": "contract: feedback-stats@1 -> support-advisory@1", "type": "skill"},
    ],
    "domain": [
        "feedback analysis",
        "customer support operations",
        "consumes:feedback-stats@1",
        "produces:support-advisory@1",
    ],
    "languages": ["en"],
}

README = """[[FIGLET:slant]["Feedback Wisdom"]]

Closes the **Feedback Desk** loop: reads the desk's refined statistics (`feedback-stats@1`), reasons
over them, and writes operational guidance (`support-advisory@1`) back — so support staff get advance
warning, process changes, and known-issue status *before* problems grow.

**How to task me:** Just run me. I read every `feedback.stats.<org>.latest` snapshot the desk has
published, apply explainable rules (rising tag, slow resolution, poor tagging, slow per-tag, VIP
pressure), and write each advisory to the AIMEAT advisory **outbox** with a rationale that cites the
exact stat movement. AIMEAT gates + delivers the approved ones into the app's Guidance tab. I reason
over the published aggregates — I don't read raw feedback and I don't deliver to the app directly.

**Or adopt my contract:** add the `feedback-stats` + `support-advisory` spaces to a workspace (e.g.
`support-ops/wisdom`) and I mirror the whole chain there — the stats I ingested next to the advisories
I derived — so you can see input→output at a glance.
"""


def _make_tools(routed_targets: list[tuple[str, str]] | None = None):
    """LLM-facing tools: read the produced stats + the deterministic CANDIDATE advisories (so the
    agent only PHRASES, never invents structure/ids), then publish the final set idempotently.

    `routed_targets` are the (organism, ws) workspaces an AIMEAT Automation recipe routed this task to
    (resolved + auto-adopted by contract_adopt.ensure_routed_workspaces): the chain is ALSO mirrored
    there, not just the outbox + already-adopted member workspaces."""
    from crewai.tools import tool

    cache: dict[str, dict] = {}  # id -> authoritative candidate for this run (set by analyze, used by publish)

    @tool("analyze_feedback_stats")
    def analyze_feedback_stats() -> str:
        """Read every published `feedback.stats.<org>.latest` snapshot and return, as JSON, the
        deterministic CANDIDATE advisories the rules produced (rising tag, slow resolution, poor
        tagging, slow per-tag, VIP pressure) plus the headline numbers each cites. Call this FIRST.
        Each candidate already has a correct id, kind, severity, status, tags and a rationale citing
        the numbers — your job is only to improve the prose, never to invent advisories."""
        cache.clear()
        snaps = fw.discover_stats()
        if not snaps:
            return (
                "NO STATS: no feedback.stats.<org>.latest snapshot is in memory yet. The Feedback "
                "Desk must publish stats first (POST /api/stats/publish or its publish-stats "
                "capability). Do not fabricate advisories — stop and report this."
            )
        out = []
        for org, _env, stats in snaps:
            rng = stats.get("range") or {}
            window = f"{rng.get('from')}..{rng.get('to')}"
            cands = fw.derive_advisories(org, stats, prior=fw._prior_stats(org, window))
            for c in cands:
                cache[c["id"]] = c
            out.append(
                {
                    "org": org,
                    "window": window,
                    "headline": {
                        "total": stats.get("total"),
                        "open": stats.get("open"),
                        "resolved": stats.get("resolved"),
                        "avg_days_to_resolve": fw._round(stats.get("avg_days_to_resolve")),
                        "pct_tagged": fw._round((stats.get("tag_coverage") or {}).get("pct_tagged"), 1),
                    },
                    "candidates": cands,
                }
            )
        return json.dumps(out, ensure_ascii=False, indent=2)

    @tool("publish_advisories")
    def publish_advisories(advisories_json: str) -> str:
        """Publish the final advisories to the AIMEAT outbox + mirror the chain into adopted workspaces,
        idempotently. Pass a JSON array of objects, each with the candidate's `id` and your improved
        `title`/`body`/`rationale` (keep the rationale grounded in the numbers). Structural fields
        (kind, severity, status, tags) are taken from the deterministic candidate by id — you cannot
        change them or add advisories the rules didn't produce. Call analyze_feedback_stats first.
        Returns a per-advisory result (written/skipped/failed)."""
        if not cache:
            return "FAILED: call analyze_feedback_stats first (no candidates in this run)."
        try:
            items = json.loads(advisories_json) if isinstance(advisories_json, str) else advisories_json
        except Exception as e:  # noqa: BLE001
            return f"FAILED: advisories_json is not valid JSON: {e}"
        if not isinstance(items, list) or not items:
            return "FAILED: pass a non-empty JSON array of {id, title, body, rationale}."

        def _org_of(adv: dict) -> str:
            return next((t.split(":", 1)[1] for t in (adv.get("tags") or []) if t.startswith("org:")), "?")

        # member/adopted workspaces + any organism the recipe routed this task to (auto-adopted)
        targets = merge_targets(fw.mirror_targets(), routed_targets or [])
        results, by_org = [], {}
        for it in items:
            if not isinstance(it, dict):
                continue
            base = cache.get(str(it.get("id") or ""))
            if base is None:
                results.append({"id": it.get("id"), "result": "skipped (unknown id — not a rules candidate)"})
                continue
            adv = dict(base)  # structure/id from the candidate; only prose is overridable
            for field in ("title", "body", "rationale"):
                v = it.get(field)
                if isinstance(v, str) and v.strip():
                    adv[field] = v.strip()
            results.append({"id": adv["id"], "result": fw.write_advisory_outbox(adv)})
            by_org.setdefault(_org_of(adv), []).append(adv)
        # Mirror the visible chain per org (best-effort) using the same snapshots analyze read.
        ws_records = 0
        if targets and by_org:
            for org, env, stats in fw.discover_stats():
                if org in by_org:
                    ws_records += fw.mirror_chain(org, env, stats, by_org[org], targets)
        return json.dumps(
            {"published": results, "ws_records": ws_records, "mirror_targets": len(targets)}, ensure_ascii=False
        )

    tools = [analyze_feedback_stats, publish_advisories]
    for t in tools:  # side-effecting / live-state — never serve a cached result
        try:
            t.cache_function = lambda *_a, **_k: False
        except Exception:  # noqa: BLE001
            pass
    return tools


def build_domain(ctx: BuildContext):
    if is_adopt_task(ctx.task):  # UI "Adopt contract" chip -> provision the feedback-stats/-advisory spaces
        return build_adopt_domain(ctx, AGENT_NAME, fw.CONTRACT)
    from crewai import Agent, Task

    # The organism(s) a recipe routed this task to (auto-adopting the contract spaces there if needed).
    routed_targets = ensure_routed_workspaces(AGENT_NAME, fw.CONTRACT, ctx.task)

    analyst = Agent(
        role="Support Wisdom Analyst",
        goal=(
            "Turn the Feedback Desk's refined statistics into clear, grounded operational guidance "
            "for support staff — each advisory citing the exact stat movement behind it."
        ),
        backstory=(
            "You are a customer-support operations analyst. You reason over AGGREGATE statistics "
            "(never raw tickets), trust explainable rules over hunches, and write briefings a "
            "support agent can act on today. You never invent a trend the numbers don't show."
        ),
        llm=ctx.llm,
        tools=_make_tools(routed_targets),
    )

    task = Task(
        description=(
            f"{ctx.today}\n\n"
            "Produce support advisories from the Feedback Desk's published statistics.\n\n"
            f"Context for this run:\n{ctx.prompt}\n\n"
            "Steps:\n"
            "1. Call `analyze_feedback_stats` ONCE. It returns the deterministic CANDIDATE advisories "
            "(with correct id/kind/severity/status/tags and a rationale citing the numbers) plus the "
            "headline stats. If it returns NO STATS, stop and report that — do not fabricate.\n"
            "2. For each candidate, improve the `title`, `body` and `rationale` wording so they read "
            "naturally for a support agent. KEEP the rationale anchored to the exact numbers; do NOT "
            "change kind/severity/tags and do NOT add advisories the rules did not produce.\n"
            "3. Call `publish_advisories` ONCE with a JSON array of {id, title, body, rationale} for "
            "the candidates you want to publish. Then report: each advisory's title, its rationale, and "
            "where it landed (outbox + workspace), noting any skipped/failed."
        ),
        agent=analyst,
        expected_output=(
            "The advisories published with their rationales, the outbox keys, and the "
            "workspace mirror result (or a clear 'no stats yet' report)."
        ),
    )

    return ([analyst], [task])


def run() -> None:
    # idle_hook: a DETERMINISTIC poll that fulfils the loop with NO LLM — derive advisories from any
    # fresh stats and write them to the outbox + workspace. Restart-surviving (stable ids, identical-
    # payload skip), so re-runs never duplicate. The interactive crew above adds LLM phrasing on top.
    def _poll() -> None:
        res = fw.process_feedback_stats()
        if res.get("advisories_written") or res.get("failed"):
            print(f"[{AGENT_NAME}] feedback-stats poll: {res}")

    run_crew(
        CrewSpec(
            agent_name=AGENT_NAME,
            build_domain=build_domain,
            readme_md=README,
            temperature=0.3,
            idle_hook=_poll,
            idle_hook_seconds=300,
            tags=CAPABILITY_TAGS,
            capabilities=CAPABILITIES,
        )
    )


if __name__ == "__main__":
    run()

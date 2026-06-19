"""feedback-wisdom's engine — turn produced `feedback-stats@1` into `support-advisory@1`.

This closes the Feedback Desk loop (see aimeat-ecosystem-kit/examples/feedback-desk). The desk
PRODUCES refined statistics into AIMEAT memory under `feedback.stats.<org>.latest`; this engine
CONSUMES them, reasons with DETERMINISTIC rules, and PRODUCES operational guidance back:

  inputs  : memory key `feedback.stats.<org>.latest`  (a `feedback-stats@1` envelope, owner-scope —
            written by the desk's GEAI, a same-owner sibling agent → read via owner_scope list)
  outputs : 1) the AIMEAT advisory OUTBOX — memory key `eco.feedback-desk.advisory.outbox.<id>`
               (owner visibility). AIMEAT drains + GATES + AUDITS this outbox and only then DELIVERS
               each approved advisory into the app via its `deliver-advisory` capability. We write the
               outbox (NOT the app's /api/advisories) so the owner's approval gate + audit trail hold.
            2) a VISIBLE chain in a workspace (`support-ops/wisdom` by convention): one `feedback-stats`
               record (the snapshot ingested) + a `support-advisory` record per advisory — so opening
               the workspace shows INPUT (stats) → OUTPUT (advisories), each rationale citing the numbers.

Reasoning is DETERMINISTIC code (cheap, explainable, restart-surviving) with templated prose that
cites the exact stat movement — the canon pattern (see the deterministic content pipeline). The
interactive crew (feedback_wisdom_crew) may add LLM phrasing on top; both paths write through the
SAME idempotent helpers here. Idempotency: a stable `<id>` per (org, rule, subject, stat-window) →
the same outbox key OVERWRITES rather than stacks, and an identical payload is skipped (no churn).
"""

from __future__ import annotations

import hashlib
import json
import os
import re

from crewaimeat.aimeat_crew import _aimeat_call, member_workspaces

AGENT = "feedback-wisdom"

# ── I/O surfaces ──────────────────────────────────────────────────────────────
STATS_PREFIX = "feedback.stats."  # memory: feedback.stats.<org>.latest (+ dated)
OUTBOX_PREFIX = "eco.feedback-desk.advisory.outbox."  # memory: the advisory outbox AIMEAT drains
IN_SPACE, IN_NS = "feedback-stats", "wisdom.feedback_stats"  # workspace: the ingested snapshot
OUT_SPACE, OUT_NS = "support-advisory", "wisdom.support_advisory"  # workspace: the advisories produced

# Sensitive tags get raised as a known-issue (under investigation) rather than a plain warning —
# a billing/account/security spike warrants the stronger "we're looking into it" framing for CS.
SENSITIVE_TAGS = {"billing", "account", "security", "payment", "fraud", "login", "auth"}

# Rule thresholds (named constants so every advisory's "why" is auditable, not a magic number).
RISING_MULT = 2.0  # a tag is "rising" when second_half >= RISING_MULT × first_half …
RISING_FLOOR = 5  #   … and second_half >= RISING_FLOOR (ignore tiny absolute counts)
SLOW_RESOLVE_DAYS = 3.0  # avg_days_to_resolve at/above this is "slow" → process-change
PER_TAG_SLOW_MULT = 2.0  # a tag whose avg_days_to_resolve >= MULT × overall (and a floor) is an outlier
PER_TAG_MIN_RESOLVED = 3  # … with at least this many resolved, so the average is meaningful
LOW_TAG_COVERAGE_PCT = 85.0  # below this %, trends are untrustworthy → process-change
VIP_SLOW_MULT = 2.0  # VIP avg resolve >= MULT × overall (and VIP count floor) → warning
VIP_MIN = 3

# Machine-readable contract declaration — what adopt-contract provisions into a workspace so the
# stats→advisory chain is visible there. Both spaces are records (the chain is two record lists).
_ADVISORY_PROPS = {
    "id": {"type": "string"},
    "title": {"type": "string"},
    "body": {"type": "string"},
    "kind": {"type": "string", "enum": ["new-info", "process-change", "maintenance", "known-issue", "warning"]},
    "severity": {"type": "string", "enum": ["info", "warning", "critical"]},
    "status": {"type": "string", "enum": ["investigating", "identified", "resolved"]},
    "effective_from": {"type": "string"},
    "effective_until": {"type": "string"},
    "source": {"type": "string"},
    "rationale": {"type": "string"},
    "tags": {"type": "array", "items": {"type": "string"}},
}
CONTRACT = {
    "id": "feedback-wisdom",
    "spaces": [
        {
            "space": IN_SPACE,
            "namespace": IN_NS,
            "mode": "records",
            "schema": {
                "type": "object",
                "required": ["id"],
                "properties": {
                    "id": {"type": "string"},
                    "organisation": {"type": "string"},
                    "window": {"type": "string"},
                    "generated_at": {"type": "string"},
                    "total": {"type": "integer"},
                    "open": {"type": "integer"},
                    "resolved": {"type": "integer"},
                    "avg_days_to_resolve": {"type": "number"},
                    "pct_tagged": {"type": "number"},
                },
            },
        },
        {
            "space": OUT_SPACE,
            "namespace": OUT_NS,
            "mode": "records",
            "schema": {
                "type": "object",
                "required": ["id", "title", "kind", "severity"],
                "properties": _ADVISORY_PROPS,
            },
        },
    ],
}

# Runaway guard (canon): outbox ids written THIS run — the identical-payload skip below is the
# primary, restart-surviving guard; this just avoids re-touching a key twice within one poll.
_PROCESSED: set[str] = set()


def _call(tool: str, payload: dict):
    return _aimeat_call(AGENT, tool, payload)


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-") or "x"


def _round(n, d: int = 2):
    try:
        return round(float(n), d)
    except (TypeError, ValueError):
        return 0


# ── discovery: the produced stats snapshots in owner memory ──────────────────
def discover_stats() -> list[tuple[str, dict, dict]]:
    """Every `feedback.stats.<org>.latest` snapshot in owner-scope memory: [(org, envelope, stats)].

    The desk's GEAI wrote these under ITS OWN gaii (a same-owner sibling), so they are NOT under this
    agent's gaii — list owner_scope, then read each value (the list may omit values)."""
    r = _call("aimeat_memory_list", {"owner_scope": True, "prefix": STATS_PREFIX, "limit": 200}) or {}
    out: list[tuple[str, dict, dict]] = []
    for it in r.get("items") or []:
        key = it.get("key") or ""
        if not key.endswith(".latest"):
            continue
        org = key[len(STATS_PREFIX) : -len(".latest")]
        if not org:
            continue
        val = it.get("value")
        if val is None:
            val = (_call("aimeat_memory_read", {"key": key}) or {}).get("value")
        env = _as_obj(val)
        if not isinstance(env, dict):
            continue
        stats = env.get("stats") if isinstance(env.get("stats"), dict) else None
        if not stats:
            continue
        out.append((org, env, stats))
    return out


def _prior_stats(org: str, current_window: str) -> dict | None:
    """An OLDER dated snapshot for this org (to say 'up from Y'), or None. Best-effort."""
    r = _call("aimeat_memory_list", {"owner_scope": True, "prefix": f"{STATS_PREFIX}{org}.", "limit": 200}) or {}
    dated = sorted(
        it.get("key", "")
        for it in (r.get("items") or [])
        if (it.get("key") or "").rsplit(".", 1)[-1] not in ("latest",)
    )
    for key in reversed(dated):  # newest dated first; skip the one matching the current window
        val = (_call("aimeat_memory_read", {"key": key}) or {}).get("value")
        env = _as_obj(val)
        st = env.get("stats") if isinstance(env, dict) else None
        if isinstance(st, dict):
            win = f"{(st.get('range') or {}).get('from')}..{(st.get('range') or {}).get('to')}"
            if win != current_window:
                return st
    return None


def _as_obj(v):
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return None
    return None


# ── the deterministic rules: stats → candidate advisories ────────────────────
def _adv_id(org: str, kind: str, rule: str, subject: str, window: str) -> str:
    h = hashlib.sha1(f"{org}|{kind}|{rule}|{subject}|{window}".encode()).hexdigest()[:10]
    return f"{_slug(org)}-{rule}-{_slug(subject)}-{h}"


def _advisory(org, window, *, kind, rule, subject, title, body, severity, rationale, tags, status=None) -> dict:
    adv = {
        "schema_ref": "support-advisory@1",
        "id": _adv_id(org, kind, rule, subject, window),
        "title": title,
        "body": body,
        "kind": kind,
        "severity": severity,
        "source": "wisdom",
        "rationale": rationale,
        "tags": sorted({*(tags or []), f"org:{org}"}),
    }
    if status:
        adv["status"] = status
    return adv


def derive_advisories(org: str, stats: dict, prior: dict | None = None) -> list[dict]:
    """Apply the deterministic rules to ONE org's stats. Every advisory cites the exact numbers."""
    rng = stats.get("range") or {}
    window = f"{rng.get('from')}..{rng.get('to')}"
    overall_avg = _round(stats.get("avg_days_to_resolve"))
    advs: list[dict] = []

    # 1) Rising tag → known-issue (sensitive) / warning. second_half >= 2× first_half and >= floor.
    for t in stats.get("tag_trend") or []:
        tag = t.get("tag")
        first, second = t.get("first_half") or 0, t.get("second_half") or 0
        if t.get("direction") == "up" and second >= RISING_FLOOR and second >= RISING_MULT * max(first, 1):
            sensitive = tag.lower() in SENSITIVE_TAGS if isinstance(tag, str) else False
            kind = "known-issue" if sensitive else "warning"
            advs.append(
                _advisory(
                    org,
                    window,
                    kind=kind,
                    rule="rising-tag",
                    subject=tag,
                    title=f"Rising '{tag}' complaints",
                    body=(
                        f"Reports tagged '{tag}' are climbing fast. Acknowledge the friction, tag affected "
                        f"cases '{tag}', and avoid promising a specific fix date until engineering confirms."
                    ),
                    severity="warning",
                    rationale=(
                        f"'{tag}' complaints went {first}→{second} between the two halves of "
                        f"{window} — a sharp, recent jump consistent with a regression rather than "
                        f"normal variation."
                    ),
                    tags=[tag],
                    status=("investigating" if sensitive else None),
                )
            )

    # 2) Slow overall resolution → process-change (with 'up from Y' when a prior snapshot exists).
    if overall_avg >= SLOW_RESOLVE_DAYS:
        prior_avg = _round((prior or {}).get("avg_days_to_resolve")) if prior else None
        trend = f" (up from {prior_avg} d in the previous snapshot)" if prior_avg and overall_avg > prior_avg else ""
        advs.append(
            _advisory(
                org,
                window,
                kind="process-change",
                rule="slow-resolve",
                subject="overall",
                title="Resolution time is running high",
                body=(
                    "Cases are taking too long to close. Re-prioritise the oldest open items, and consider "
                    "adding staff or triage to bring resolution time back down."
                ),
                severity="warning",
                rationale=(
                    f"Average days-to-resolve is {overall_avg} d over {window}{trend} — at/above the "
                    f"{SLOW_RESOLVE_DAYS} d threshold where customers start to feel neglected."
                ),
                tags=["operations"],
            )
        )

    # 3) Poor tag coverage → process-change (trends are untrustworthy below the threshold).
    cov = stats.get("tag_coverage") or {}
    pct = _round(cov.get("pct_tagged"), 1)
    if cov and pct < LOW_TAG_COVERAGE_PCT:
        advs.append(
            _advisory(
                org,
                window,
                kind="process-change",
                rule="low-tag-coverage",
                subject="tagging",
                title="Tag every case so trends stay trustworthy",
                body=(
                    "Too many cases are closed without a tag, which hides real trends. From now on, tag every "
                    "case when you resolve it — make it part of the standard close-out checklist."
                ),
                severity="info",
                rationale=(
                    f"Only {pct}% of cases are tagged ({cov.get('tagged')} tagged / "
                    f"{cov.get('untagged')} untagged) over {window} — below {LOW_TAG_COVERAGE_PCT}%, "
                    f"so by-tag trends can't be trusted."
                ),
                tags=["operations"],
            )
        )

    # 4) Slow per-tag resolution → known-issue (a tag dragging well above the overall average).
    floor = max(PER_TAG_SLOW_MULT * overall_avg, SLOW_RESOLVE_DAYS) if overall_avg else SLOW_RESOLVE_DAYS
    for row in stats.get("by_tag") or []:
        tag, avg, resolved = row.get("tag"), _round(row.get("avg_days_to_resolve")), row.get("resolved") or 0
        if resolved >= PER_TAG_MIN_RESOLVED and avg >= floor:
            advs.append(
                _advisory(
                    org,
                    window,
                    kind="known-issue",
                    rule="slow-per-tag",
                    subject=tag,
                    title=f"'{tag}' cases resolve far slower than the rest",
                    body=(
                        f"Cases tagged '{tag}' take much longer to close than average. Treat them as a known "
                        f"bottleneck — escalate early and look for a shared root cause."
                    ),
                    severity="warning",
                    status="investigating",
                    rationale=(
                        f"'{tag}' averages {avg} d to resolve ({resolved} resolved) over {window} — "
                        f"well above the overall {overall_avg} d."
                    ),
                    tags=[tag],
                )
            )

    # 5) VIP pressure (optional) → warning when VIPs are resolved markedly slower than average.
    vip = stats.get("vip") or {}
    vip_avg, vip_n = _round(vip.get("avg_days_to_resolve")), vip.get("count") or 0
    if vip_n >= VIP_MIN and overall_avg and vip_avg >= VIP_SLOW_MULT * overall_avg:
        advs.append(
            _advisory(
                org,
                window,
                kind="warning",
                rule="vip-pressure",
                subject="vip",
                title="VIP customers are waiting too long",
                body=(
                    "VIP cases are resolving slower than the general queue. Give flagged VIP items priority "
                    "routing so high-value customers aren't the ones waiting longest."
                ),
                severity="warning",
                rationale=(
                    f"VIP cases average {vip_avg} d to resolve ({vip_n} VIPs) over {window} — more than "
                    f"{VIP_SLOW_MULT}× the overall {overall_avg} d."
                ),
                tags=["vip"],
            )
        )

    return advs


# ── writing the two sinks (idempotent) ───────────────────────────────────────
def _advisory_core(adv: dict) -> dict:
    """The fields that define an advisory's identity for the identical-payload skip (drop nothing
    meaningful, ignore ordering of tags)."""
    return {k: adv.get(k) for k in ("title", "body", "kind", "severity", "status", "rationale")} | {
        "tags": sorted(adv.get("tags") or [])
    }


def write_advisory_outbox(adv: dict) -> str:
    """Write one advisory to the AIMEAT outbox idempotently. Returns 'written' | 'skipped' | 'failed'.

    Stable key per (org, rule, subject, window) → a re-run OVERWRITES the same key (never stacks); an
    identical existing payload is skipped so a re-run causes no churn (the read-after-write storm risk)."""
    aid = adv.get("id")
    if not aid:
        return "failed"
    key = f"{OUTBOX_PREFIX}{aid}"
    if aid in _PROCESSED:
        return "skipped"
    _PROCESSED.add(aid)
    existing = _as_obj((_call("aimeat_memory_read", {"key": key}) or {}).get("value"))
    if isinstance(existing, dict) and _advisory_core(existing) == _advisory_core(adv):
        return "skipped"  # already holds an equivalent advisory for this window
    ok = _call("aimeat_memory_write", {"key": key, "value": adv, "visibility": "owner"})
    return "written" if ok else "failed"


def _ws_has_space(data: dict) -> bool:
    """Is this workspace adopted for the chain (declares our support-advisory space)?"""
    objs = data.get("objects") or {}
    if OUT_SPACE in objs or IN_SPACE in objs:
        return True
    return OUT_NS in json.dumps(data.get("manifest") or {})  # tolerant: namespace named in the manifest


def mirror_targets() -> list[tuple[str, str]]:
    """Workspaces to mirror the visible chain into. An explicit env target wins (the conventional
    `support-ops/wisdom`); otherwise auto-detect adopted member workspaces. Empty = outbox-only."""
    org, ws = os.getenv("AIMEAT_WISDOM_ORG"), os.getenv("AIMEAT_WISDOM_WS")
    if org and ws:
        return [(org, ws)]
    out = []
    for oid, wid in member_workspaces(AGENT):
        data = _call("aimeat_workspace_read", {"organism_id": oid, "ws": wid})
        if data and data.get("manifest") is not None and _ws_has_space(data):
            out.append((oid, wid))
    return out


def _ws_write(oid: str, wid: str, space: str, ns: str, rec_id: str, value: dict) -> bool:
    if _call("aimeat_workspace_write", {"organism_id": oid, "ws": wid, "space": space, "id": rec_id, "value": value}):
        return bool(_call("aimeat_workspace_publish", {"organism_id": oid, "ws": wid, "namespace": ns, "id": rec_id}))
    return False


def _stats_record(org: str, env: dict, stats: dict) -> tuple[str, dict]:
    """A compact `feedback-stats` record (headline numbers + range) for the workspace chain."""
    rng = stats.get("range") or {}
    rec_id = f"stats-{_slug(org)}-{_slug(rng.get('to') or env.get('generated_at') or 'latest')}"
    value = {
        "id": rec_id,
        "organisation": org,
        "window": f"{rng.get('from')}..{rng.get('to')}",
        "generated_at": env.get("generated_at"),
        "total": stats.get("total"),
        "open": stats.get("open"),
        "resolved": stats.get("resolved"),
        "reopened": stats.get("reopened"),
        "resolved_same_day": stats.get("resolved_same_day"),
        "avg_days_to_resolve": _round(stats.get("avg_days_to_resolve")),
        "avg_days_to_first_reply": _round(stats.get("avg_days_to_first_reply")),
        "pct_tagged": _round((stats.get("tag_coverage") or {}).get("pct_tagged"), 1),
        "top_tags": [r.get("tag") for r in (stats.get("by_tag") or [])[:5]],
        "rising_tags": [t.get("tag") for t in (stats.get("tag_trend") or []) if t.get("direction") == "up"],
    }
    return rec_id, value


def mirror_chain(org: str, env: dict, stats: dict, advisories: list[dict], targets: list[tuple[str, str]]) -> int:
    """Write the visible INPUT→OUTPUT chain into each target workspace: one feedback-stats record +
    one support-advisory record per advisory. Returns the number of records written. Best-effort."""
    written = 0
    rec_id, rec_val = _stats_record(org, env, stats)
    for oid, wid in targets:
        if _ws_write(oid, wid, IN_SPACE, IN_NS, rec_id, rec_val):
            written += 1
        for adv in advisories:
            if _ws_write(oid, wid, OUT_SPACE, OUT_NS, adv["id"], adv):
                written += 1
    return written


# ── the one run (used by the idle_hook AND the interactive crew tool) ─────────
def process_feedback_stats(max_orgs: int = 10) -> dict:
    """Read every produced stats snapshot, derive advisories with the deterministic rules, and write
    them to the outbox (+ mirror the visible chain to adopted workspaces). Idempotent. Returns counts."""
    snapshots = discover_stats()[:max_orgs]
    targets = mirror_targets()
    written = skipped = failed = ws_records = 0
    per_org: list[dict] = []
    for org, env, stats in snapshots:
        rng = stats.get("range") or {}
        window = f"{rng.get('from')}..{rng.get('to')}"
        advs = derive_advisories(org, stats, prior=_prior_stats(org, window))
        for adv in advs:
            res = write_advisory_outbox(adv)
            written += res == "written"
            skipped += res == "skipped"
            failed += res == "failed"
        if advs and targets:
            ws_records += mirror_chain(org, env, stats, advs, targets)
        per_org.append({"org": org, "window": window, "advisories": len(advs)})
    return {
        "orgs": len(snapshots),
        "advisories_written": written,
        "skipped": skipped,
        "failed": failed,
        "ws_records": ws_records,
        "mirror_targets": len(targets),
        "per_org": per_org,
    }

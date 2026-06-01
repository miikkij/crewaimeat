"""Self-evolution monitor — doc 20, Phase 1 (notice + propose only).

A crew built with CrewSpec(self_monitor=True) runs self_monitor_check() after each task. It reads the
agent's OWN reputation rollup and looks for an evolution signal:
  - WEAK  : byContext[ctx].avgStars < WEAK_FLOOR  (consistently low)
  - SPLIT : the dist is bimodal — lots of low (1-2) AND lots of high (4-5) ratings (great on some
            inputs, weak on others -> a specialization candidate)
If one fires AND there is enough data (n >= MIN_N — the n=3 lesson) AND we haven't proposed the same
thing recently (cooldown), it sends the owner a CLICKABLE proposal via aimeat_message_send's
metadata.prompt ("Explore an evolution? [Explore] [Not now]").

Phase 1 only NOTICES and PROPOSES. Designing candidates, A/B-testing them, and promoting are later
phases (P3/P4) and stay human-gated — the owner clicks /evolve to start any of that.
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone

import requests

import re

from crewaimeat.aimeat_crew import _aimeat_call, _aimeat_read_token
from crewaimeat.llm import get_llm

MIN_N = 10            # never propose on thin data (the n=3 lesson — see doc 20)
WEAK_FLOOR = 2.5      # avgStars below this (with enough n) = consistently weak
COOLDOWN_DAYS = 3     # don't re-propose the same (context, signal) within this window
_PROPOSED_KEY = "agents.{agent}.statistics.custom.evolve_proposed"   # time-based dedup (anti-spam)
_LINEAGE_KEY = "agents.{agent}.statistics.custom.evolve_lineage"     # PERMANENT: a variant was built


def _has_variant(agent: str, ctx: str, signal: str) -> bool:
    """True if an evolution variant was ALREADY built for this (context, signal).

    Permanent, not time-based: once /evolve produced a variant (recorded by record_evolution on
    selection), the monitor must stop re-proposing the SAME evolution — even after the cooldown, and
    even if the parent still shows the signal (e.g. a specialist took some traffic but the parent's
    own dist is unchanged). A genuinely DIFFERENT signal for the same context is still allowed."""
    rec = (_aimeat_call(agent, "aimeat_memory_read", {"key": _LINEAGE_KEY.format(agent=agent)}) or {}).get("value")
    return bool(isinstance(rec, dict) and (rec.get(ctx) or {}).get(signal))


def record_evolution(parent: str, ctx: str, signal: str, variant: str, mode: str) -> bool:
    """Record that a variant was built for parent's (ctx, signal) so the monitor stops re-proposing it.

    Called by the selection step (P4) when the owner picks a variant. mode = 'replace' | 'specialist'.
    Idempotent; stores the latest variant per (ctx, signal)."""
    key = _LINEAGE_KEY.format(agent=parent)
    rec = (_aimeat_call(parent, "aimeat_memory_read", {"key": key}) or {}).get("value")
    rec = rec if isinstance(rec, dict) else {}
    rec.setdefault(ctx, {})[signal] = {
        "variant": variant, "mode": mode, "ts": datetime.now(timezone.utc).isoformat(),
    }
    res = _aimeat_call(parent, "aimeat_memory_write", {"key": key, "value": rec, "visibility": "owner"})
    print(f"[{parent}] evolution recorded: {ctx}/{signal} -> {variant} ({mode}): {bool(res)}", file=sys.stderr)
    return bool(res)


def _read_reviews(agent: str, owner: str | None) -> dict:
    """The agent's own reputation rollup (GET /v1/agents/:agent/statistics with its own token)."""
    if _aimeat_read_token is None:
        return {}
    try:
        tok, url = _aimeat_read_token(agent, owner=owner)
        r = requests.get(f"{url.rstrip('/')}/v1/agents/{agent}/statistics",
                         headers={"Authorization": f"Bearer {tok}"}, timeout=20)
        return r.json().get("data", {}).get("reviews", {}) if r.status_code == 200 else {}
    except Exception:  # noqa: BLE001 — monitoring is best-effort, never break the task
        return {}


def _signal(stats: dict) -> tuple[str | None, str]:
    """Classify a context's rollup. Returns ('weak'|'split', human detail) or (None, '')."""
    n = stats.get("n") or 0
    avg = stats.get("avgStars")
    dist = stats.get("dist") or {}
    if avg is not None and avg < WEAK_FLOOR:
        return "weak", f"avg {avg}★ over n={n}"
    low = (dist.get("1", 0) or 0) + (dist.get("2", 0) or 0)
    high = (dist.get("4", 0) or 0) + (dist.get("5", 0) or 0)
    if n and low >= 0.25 * n and high >= 0.25 * n:  # bimodal: meaningful mass at BOTH ends
        return "split", f"bimodal — {low} low (1-2★) and {high} high (4-5★) of n={n}"
    return None, ""


def _recently_proposed(agent: str, ctx: str) -> bool:
    rec = (_aimeat_call(agent, "aimeat_memory_read", {"key": _PROPOSED_KEY.format(agent=agent)}) or {}).get("value")
    entry = (rec or {}).get(ctx) if isinstance(rec, dict) else None
    ts = (entry or {}).get("ts")
    if not ts:
        return False
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).days < COOLDOWN_DAYS
    except Exception:  # noqa: BLE001
        return False


def _mark_proposed(agent: str, ctx: str, signal: str) -> None:
    key = _PROPOSED_KEY.format(agent=agent)
    rec = (_aimeat_call(agent, "aimeat_memory_read", {"key": key}) or {}).get("value")
    rec = rec if isinstance(rec, dict) else {}
    rec[ctx] = {"signal": signal, "ts": datetime.now(timezone.utc).isoformat()}
    _aimeat_call(agent, "aimeat_memory_write", {"key": key, "value": rec, "visibility": "owner"})


def _propose(agent: str, ctx: str, signal: str, detail: str) -> bool:
    """Send the owner a clickable 'explore an evolution?' prompt (metadata.prompt)."""
    pid = f"evolve-{agent}-{ctx}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    if signal == "weak":
        question = f"{agent} is scoring low on '{ctx}' ({detail}). Explore an evolution?"
    else:
        question = (f"{agent} is inconsistent on '{ctx}' ({detail}) — strong on some inputs, weak on "
                    f"others. Explore a specialist / evolution?")
    body = {
        "content": (
            f"**Self-monitor — {agent} / {ctx}: {signal.upper()} signal**\n\n{detail}\n\n"
            f"Run `/evolve {agent}` and I'll design candidate evolution(s), A/B-test them against this "
            f"crew on its own rated tasks, and bring back only the proven-better ones to pick from."
        ),
        "metadata": {"prompt": {
            "prompt_id": pid,
            "question": question,
            "options": [f"Explore evolution (/evolve {agent})", "Not now"],
            "allow_other": False,
        }},
    }
    res = _aimeat_call(agent, "aimeat_message_send", body)
    print(f"[{agent}] self-monitor proposed evolution: {ctx}/{signal} -> {bool(res)}", file=sys.stderr)
    return bool(res)


# --------------------------------------------------------------------------- #
# Phase 2 — cluster classifier: explain WHY the scores split, to target the variant.
# --------------------------------------------------------------------------- #
_DIAGNOSE_PROMPT = (
    "You are diagnosing why a crew performs unevenly. Crew: '{agent}', context: '{ctx}'. "
    "Signal: {signal}.\n\nHere are its rated tasks grouped by the score a reviewer gave each one. "
    "Find what DISTINGUISHES the low-rated tasks from the high-rated ones — what kind of input, topic, "
    "or format does this crew handle WELL vs POORLY?\n\n"
    "LOW (1-2★):\n{low}\n\nMID (3★):\n{mid}\n\nHIGH (4-5★):\n{high}\n\n"
    "Then propose ONE concrete evolution direction. Choose 'specialist' if it's clearly strong on a "
    "sub-type and should get a coexisting specialist for it; choose 'replace' if it's uniformly weak "
    "and the whole design should be rebuilt.\n\n"
    "Reply in EXACTLY this format, nothing else:\n"
    "DISTINCTION: <one sentence: what separates low from high>\n"
    "WEAK_AT: <short label of the weak cluster, e.g. puns / long-form / vague topics>\n"
    "STRONG_AT: <short label of the strong cluster>\n"
    "MODE: <specialist|replace>\n"
    "BRIEF: <one or two sentences telling a crew-builder exactly what the evolved crew should change>"
)
_DIAG_RE = {k: re.compile(rf"(?im)^{k}:\s*(.+)$") for k in ("DISTINCTION", "WEAK_AT", "STRONG_AT", "MODE", "BRIEF")}


def _strip_marker(text: str) -> str:
    """Drop the appended <<AIMEAT_PUBLISH ...>> directive so the topic/ask is what the LLM clusters on."""
    return (text or "").split("<<AIMEAT_PUBLISH")[0].strip()


def _rated_tasks(agent: str, ctx: str, owner: str | None = None, limit: int = 40) -> list[dict]:
    """The agent's done tasks carrying a rating in this context: [{title, instruction, stars}]."""
    listing = _aimeat_call(agent, "aimeat_task_list", {"status": "done", "per_page": 100}) or {}
    tasks = listing.get("tasks") or (listing.get("data") or {}).get("tasks") or (listing if isinstance(listing, list) else [])
    out: list[dict] = []
    for t in tasks[:limit]:
        tid = t.get("id") if isinstance(t, dict) else None
        if not tid:
            continue
        full = (_aimeat_call(agent, "aimeat_task_get", {"task_id": tid}) or {}).get("task") or {}
        rating = full.get("rating") or {}
        stars = rating.get("stars")
        if stars is None or (rating.get("context") and rating.get("context") != ctx):
            continue
        out.append({
            "title": full.get("title") or (t.get("title") if isinstance(t, dict) else "") or "",
            "instruction": _strip_marker(full.get("description") or "")[:300],
            "stars": stars,
        })
    return out


def _fmt_cluster(tasks: list[dict]) -> str:
    return "\n".join(f"  - [{t['stars']}★] {t['title']}: {t['instruction'][:160]}" for t in tasks) or "  (none)"


def diagnose(agent: str, ctx: str, signal: str, owner: str | None = None) -> dict:
    """Classify why scores split into clusters and propose a targeted evolution brief.

    Returns {ok, distinction, weak_at, strong_at, mode, brief, counts}. ok=False if there aren't
    enough rated tasks to diagnose (don't guess on thin data). Used by /evolve (P3) to design the
    candidate(s); not run in the per-task monitor (it costs N task_get calls)."""
    tasks = _rated_tasks(agent, ctx, owner)
    if len(tasks) < MIN_N:
        return {"ok": False, "reason": f"only {len(tasks)} rated tasks in '{ctx}' (need >= {MIN_N})"}
    low = [t for t in tasks if t["stars"] <= 2]
    mid = [t for t in tasks if t["stars"] == 3]
    high = [t for t in tasks if t["stars"] >= 4]
    prompt = _DIAGNOSE_PROMPT.format(agent=agent, ctx=ctx, signal=signal,
                                     low=_fmt_cluster(low), mid=_fmt_cluster(mid), high=_fmt_cluster(high))
    try:
        reply = str(get_llm(for_tool_use=False, temperature=0.2).call(prompt))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"diagnosis LLM call failed: {exc}"}
    out = {"ok": True, "counts": {"low": len(low), "mid": len(mid), "high": len(high)},
           "signal": signal, "context": ctx}
    for k, rx in _DIAG_RE.items():
        m = rx.search(reply)
        out[k.lower()] = m.group(1).strip() if m else None
    # default mode if the model didn't say: split -> specialist, weak -> replace
    if out.get("mode") not in ("specialist", "replace"):
        out["mode"] = "specialist" if signal == "split" else "replace"
    return out


def self_monitor_check(agent_name: str, owner: str | None = None) -> None:
    """Read own reputation; for each context past the n-gate, fire a gated, deduped proposal."""
    by_ctx = (_read_reviews(agent_name, owner).get("byContext")) or {}
    for ctx, stats in by_ctx.items():
        if (stats.get("n") or 0) < MIN_N:
            continue  # the n=3 lesson — never act on thin data
        signal, detail = _signal(stats)
        if not signal:
            continue
        if _has_variant(agent_name, ctx, signal):
            continue  # already evolved for this (ctx, signal) — permanent suppression, don't re-propose
        if _recently_proposed(agent_name, ctx):
            continue  # proposed recently (any signal) — anti-spam cooldown
        if _propose(agent_name, ctx, signal, detail):
            _mark_proposed(agent_name, ctx, signal)

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

import math
import re
from statistics import mean, pstdev
from types import SimpleNamespace

from crewai import Crew, Process

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


def _send(agent: str, content: str, prompt: dict | None = None) -> bool:
    """Send the owner a message FROM this agent (its own thread). Optional clickable metadata.prompt."""
    body: dict = {"content": content}
    if prompt:
        body["metadata"] = {"prompt": prompt}
    return bool(_aimeat_call(agent, "aimeat_message_send", body))


def _propose(agent: str, ctx: str, signal: str, detail: str) -> bool:
    """Propose an evolution FROM the agent itself, in its own thread (self-evolution).

    The agent sends the owner a clickable prompt; the answer comes back to THIS agent, which handles it
    (handle_evolve_answer). The conversation stays with the agent — the only thing delegated to
    crew-forge is the mechanical crew-file creation, and only after the owner approves."""
    pid = f"evolve-{agent}-{ctx}-explore"
    question = (f"I'm inconsistent on '{ctx}' ({detail}) — strong on some inputs, weak on others. "
                f"Explore an evolution of me?" if signal == "split"
                else f"I'm scoring low on '{ctx}' ({detail}). Explore an evolution of me?")
    ok = _send(
        agent,
        f"**Self-monitor — {ctx}: {signal.upper()}**\n\n{detail}\n\nIf you say explore, I'll diagnose "
        f"exactly what I'm good and bad at, then design an evolved version of myself and A/B-test it "
        f"against my current self — and only bring it back if it's actually better.",
        prompt={"prompt_id": pid, "question": question,
                "options": ["Explore evolution", "Not now"], "allow_other": False},
    )
    print(f"[{agent}] self-monitor proposed evolution (own thread): {ctx}/{signal} -> {ok}", file=sys.stderr)
    return ok


# The exact option texts we offer — used as a fallback when AIMEAT doesn't propagate prompt_answer
# metadata into the delivered message (then the message CONTENT is just the chosen option text).
_EVOLVE_CHOICES = {"explore evolution", "build & a/b test", "not now"}


def is_evolve_answer(task: dict, content: str = "") -> dict | None:
    """If this message is an answer to one of THIS agent's evolution prompts, return a prompt_answer
    dict ({prompt_id, choice}); else None. Primary signal: metadata.prompt_answer with a prompt_id we
    minted (starts 'evolve-'). Fallback: the message content equals one of our option texts."""
    if task.get("_source") != "message":
        return None
    pa = ((task.get("_original") or {}).get("metadata") or {}).get("prompt_answer")
    if isinstance(pa, dict) and str(pa.get("prompt_id", "")).startswith("evolve-"):
        return pa
    text = (content or "").strip()
    if text.lower() in _EVOLVE_CHOICES:
        return {"choice": text, "prompt_id": ""}  # ctx re-detected via latest_signal in the handler
    return None


def handle_evolve_answer(agent: str, pa: dict, owner: str | None = None) -> None:
    """Act on the owner's click on one of THIS agent's evolution prompts, replying in the agent's thread.

    Stages (each replies with the next clickable step, so the whole conversation lives here):
      explore -> diagnose + propose to build & A/B test
      build   -> hand the crew-file creation to crew-forge (the one thing the agent can't do itself)
      else    -> dismiss
    """
    choice = (pa.get("choice") or "").strip()
    cl = choice.lower()
    pid = str(pa.get("prompt_id", ""))
    # ctx is the middle segment of the prompt_id: evolve-<agent>-<ctx>-<stage>
    parts = pid.split("-")
    ctx = parts[-2] if len(parts) >= 3 else "general"

    if cl.startswith("explore"):
        sig_ctx, sig, detail = latest_signal(agent, owner)
        if not sig:
            _send(agent, "The pattern has cleared since I flagged it — no evolution needed right now.")
            return
        ctx = sig_ctx or ctx
        d = diagnose(agent, ctx, sig, owner)
        if not d.get("ok"):
            _send(agent, f"I wanted to diagnose my {sig} pattern on '{ctx}' but couldn't yet: {d.get('reason')}.")
            return
        report = (
            f"**Diagnosis — {ctx} ({sig.upper()})**\n\n"
            f"- What separates my good from bad: {d.get('distinction')}\n"
            f"- I'm weak at: **{d.get('weak_at')}**\n"
            f"- I'm strong at: **{d.get('strong_at')}**\n"
            f"- Proposed: a **{d.get('mode')}** — {d.get('brief')}\n\n"
            f"Shall I build that evolved version and A/B-test it against my current self on my own past "
            f"tasks? I'll only bring it back if it's proven better."
        )
        _send(agent, report, prompt={
            "prompt_id": f"evolve-{agent}-{ctx}-build",
            "question": f"Build + A/B-test the {d.get('mode')} evolution of {agent}?",
            "options": ["Build & A/B test", "Not now"], "allow_other": False,
        })
        return

    if cl.startswith("build"):
        # The actual build + A/B (crew-forge designs the candidate, evolve_ab compares it to me on my own
        # tasks, only-if-better proposal) is the next capability being wired. Acknowledge honestly rather
        # than fire a hand-off crew-forge can't act on yet.
        _send(agent, "Noted — building the evolved version and A/B-testing it against my current self is "
                     "the capability being wired right now. Once it's in, clicking this will design the "
                     "candidate, test it on my own past tasks, and bring it back only if it's proven better.")
        return

    _send(agent, "Okay — no evolution for now. I'll flag it again if the pattern persists.")


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


# --------------------------------------------------------------------------- #
# Phase 3 core — evolve A/B: re-run BOTH designs on the agent's OWN rated tasks, grade BOTH with the
# SAME judge the coordinator uses live (_judge_deliverable), compare overall + per-cluster.
# --------------------------------------------------------------------------- #
def _run_design(build_fn, instruction: str, llm) -> str:
    """Run one crew design on one task instruction in-process (no daemon); return the deliverable."""
    ctx = SimpleNamespace(llm=llm, prompt=instruction, today="", directives="")
    agents, tasks = build_fn(ctx)
    for a in agents:
        a.verbose = False
    return str(Crew(agents=agents, tasks=tasks, process=Process.sequential, verbose=False).kickoff().raw)


def _welch(a: list[float], b: list[float]) -> tuple[float, float]:
    if len(a) < 2 or len(b) < 2:
        return 0.0, 0.0
    ma, mb, va, vb = mean(a), mean(b), pstdev(a) ** 2, pstdev(b) ** 2
    se = math.sqrt(va / len(a) + vb / len(b))
    t = (mb - ma) / se if se else 0.0
    pooled = math.sqrt(((len(a) - 1) * va + (len(b) - 1) * vb) / max(1, len(a) + len(b) - 2))
    d = (mb - ma) / pooled if pooled else 0.0
    return round(t, 2), round(d, 2)


def _verdict(inc: list[float], cand: list[float]) -> dict:
    """Candidate vs incumbent. promote = real gain: gap>0 AND |t|>=2 AND d>=0.8 (NO 'gap>stdev' gate —
    that one lets a high-variance incumbent hide a real loss, which is exactly what we're fixing)."""
    if not inc or not cand:
        return {"n": 0}
    t, d = _welch(inc, cand)
    gap = round(mean(cand) - mean(inc), 3)
    return {"n": min(len(inc), len(cand)), "inc_avg": round(mean(inc), 3), "cand_avg": round(mean(cand), 3),
            "gap": gap, "welch_t": t, "cohen_d": d, "promote": bool(gap > 0 and abs(t) >= 2.0 and d >= 0.8)}


def evolve_ab(incumbent_fn, candidate_fn, eval_tasks: list[dict], temperature: float = 0.7,
              repeats: int = 2) -> dict:
    """A/B incumbent vs candidate on the agent's OWN rated tasks (option B — re-run both, same judge).

    eval_tasks: [{instruction, stars}] from the agent's rated history (diagnose()/_rated_tasks).
    Returns {overall, by_cluster:{low,high}} verdicts. Per-cluster lets a SPECIALIST that wins only its
    target cluster be detected even if it doesn't win overall. Heavy (runs both crews x tasks x repeats)
    — call it in the background (P3 launches it detached, under the single-instance lock)."""
    from crewaimeat.workflow import _judge_deliverable  # local: same judge the coordinator rates with

    judge = get_llm(temperature=0.15)
    runner = get_llm(temperature=temperature)
    rows: list[dict] = []
    for t in eval_tasks:
        instr = t["instruction"]
        cluster = "low" if t["stars"] <= 2 else "high" if t["stars"] >= 4 else "mid"
        for _ in range(repeats):
            ji = _judge_deliverable(judge, instr, _run_design(incumbent_fn, instr, runner))
            jc = _judge_deliverable(judge, instr, _run_design(candidate_fn, instr, runner))
            rows.append({"cluster": cluster, "inc": ji[1] if ji else None, "cand": jc[1] if jc else None})
    out = {"overall": _verdict([r["inc"] for r in rows if r["inc"] is not None],
                               [r["cand"] for r in rows if r["cand"] is not None]),
           "by_cluster": {}}
    for cl in ("low", "high"):
        out["by_cluster"][cl] = _verdict(
            [r["inc"] for r in rows if r["cluster"] == cl and r["inc"] is not None],
            [r["cand"] for r in rows if r["cluster"] == cl and r["cand"] is not None],
        )
    return out


def latest_signal(agent: str, owner: str | None = None) -> tuple[str | None, str | None, str | None]:
    """First (context, signal, detail) past the n-gate with a WEAK/SPLIT signal, else (None, None, None).

    Used by crew-forge's /evolve to know WHICH (context, signal) to evolve — re-detected live, so it
    works whether triggered by a click relay or typed manually."""
    by_ctx = (_read_reviews(agent, owner).get("byContext")) or {}
    for ctx, stats in by_ctx.items():
        if (stats.get("n") or 0) < MIN_N:
            continue
        sig, detail = _signal(stats)
        if sig:
            return ctx, sig, detail
    return None, None, None


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

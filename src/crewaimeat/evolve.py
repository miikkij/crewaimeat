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

import importlib.util
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, pstdev
from types import SimpleNamespace

import requests
from crewai import Crew, Process

from crewaimeat.aimeat_crew import _aimeat_call, _aimeat_read_token
from crewaimeat.llm import get_llm

MIN_N = 10  # never propose on thin data (the n=3 lesson — see doc 20)
WEAK_FLOOR = 2.5  # avgStars below this (with enough n) = consistently weak
COOLDOWN_DAYS = 3  # don't re-propose the same (context, signal) within this window
_PROPOSED_KEY = "agents.{agent}.statistics.custom.evolve_proposed"  # time-based dedup (anti-spam)
_LINEAGE_KEY = "agents.{agent}.statistics.custom.evolve_lineage"  # PERMANENT: a variant was built


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
        "variant": variant,
        "mode": mode,
        "ts": datetime.now(timezone.utc).isoformat(),
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
        r = requests.get(
            f"{url.rstrip('/')}/v1/agents/{agent}/statistics", headers={"Authorization": f"Bearer {tok}"}, timeout=20
        )
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
    question = (
        f"I'm inconsistent on '{ctx}' ({detail}) — strong on some inputs, weak on others. Explore an evolution of me?"
        if signal == "split"
        else f"I'm scoring low on '{ctx}' ({detail}). Explore an evolution of me?"
    )
    ok = _send(
        agent,
        f"**Self-monitor — {ctx}: {signal.upper()}**\n\n{detail}\n\nIf you say explore, I'll diagnose "
        f"exactly what I'm good and bad at, then design an evolved version of myself and A/B-test it "
        f"against my current self — and only bring it back if it's actually better.",
        prompt={
            "prompt_id": pid,
            "question": question,
            "options": ["Explore evolution", "Not now"],
            "allow_other": False,
        },
    )
    print(f"[{agent}] self-monitor proposed evolution (own thread): {ctx}/{signal} -> {ok}", file=sys.stderr)
    return ok


# The exact option texts we offer — used as a fallback when AIMEAT doesn't propagate prompt_answer
# metadata into the delivered message (then the message CONTENT is just the chosen option text).
# Friendly, non-technical labels (the A/B build happens under the hood).
_EVOLVE_CHOICES = {
    "explore evolution",
    "evolve to the next level",
    "not now",
    "make it live",
    "add the specialist",
    "postpone",
    "cancel",
}


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
        # Acknowledge immediately — the diagnosis below reads my rated tasks + an LLM call (~30-60s),
        # so tell the owner what's happening and that a follow-up is coming.
        _send(
            agent,
            "On it — reading back through my rated jokes to pin down exactly where I'm strong "
            "and weak. Give me ~30–60 seconds and I'll follow up here with the diagnosis. 🔎",
        )
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
            f"Want me to grow into that next version? I'll build it, prove it against my current self on "
            f"my own past tasks, and only keep it if it's genuinely better."
        )
        _send(
            agent,
            report,
            prompt={
                "prompt_id": f"evolve-{agent}-{ctx}-build",
                "question": "Ready for me to evolve to my next level?",
                "options": ["Evolve to the next level", "Not now"],
                "allow_other": False,
            },
        )
        return

    if "next level" in cl or cl.startswith("evolve to"):
        if _launch_evolve_run(agent, ctx):
            _send(
                agent,
                "On it — I'm designing my evolved version and A/B-testing it against my current self on "
                "my own past tasks. This runs in the background and is slow on the current model "
                "(~10–20 min), so go do something else — **I'll message you here the moment the results "
                "are in.** If it wins and you approve, you'll then get a one-time connect-approval for "
                "the new agent. Nothing changes without your yes. 🛠️",
            )
        else:
            _send(
                agent,
                "I tried to start the level-up but couldn't launch the background run — I've "
                "logged it; try again shortly.",
            )
        return

    if cl.startswith("make it live") or cl.startswith("add the specialist"):
        mode = "replace" if cl.startswith("make it live") else "specialist"
        vname = (
            pid.split("-live-")[-1]
            if "-live-" in pid
            else pid.split("-spec-")[-1]
            if "-spec-" in pid
            else f"{agent}-evolved"
        )
        _promote_candidate(agent, vname, ctx, mode, owner)
        return

    if cl.startswith("postpone"):
        _send(
            agent,
            "Okay — I'll keep my evolved version on the shelf, ready. I'll re-offer it (or you can "
            "ask) when you want to revisit.",
        )
        return

    if cl.startswith("cancel"):
        try:
            from crewaimeat.forge import _fname

            cand = Path.cwd() / "crews" / ".candidates" / _fname(f"{agent}-evolved")
            if cand.exists():
                cand.unlink()
        except Exception:  # noqa: BLE001
            pass
        _send(
            agent,
            "Scrapped it — the evolved version is discarded. I'll keep watching and propose a "
            "fresh angle if the pattern persists.",
        )
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
    tasks = (
        listing.get("tasks")
        or (listing.get("data") or {}).get("tasks")
        or (listing if isinstance(listing, list) else [])
    )
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
        out.append(
            {
                "title": full.get("title") or (t.get("title") if isinstance(t, dict) else "") or "",
                "instruction": _strip_marker(full.get("description") or "")[:300],
                "stars": stars,
            }
        )
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
    prompt = _DIAGNOSE_PROMPT.format(
        agent=agent, ctx=ctx, signal=signal, low=_fmt_cluster(low), mid=_fmt_cluster(mid), high=_fmt_cluster(high)
    )
    try:
        reply = str(get_llm(for_tool_use=False, temperature=0.2).call(prompt))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"diagnosis LLM call failed: {exc}"}
    out = {
        "ok": True,
        "counts": {"low": len(low), "mid": len(mid), "high": len(high)},
        "signal": signal,
        "context": ctx,
    }
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
    return {
        "n": min(len(inc), len(cand)),
        "inc_avg": round(mean(inc), 3),
        "cand_avg": round(mean(cand), 3),
        "gap": gap,
        "welch_t": t,
        "cohen_d": d,
        "promote": bool(gap > 0 and abs(t) >= 2.0 and d >= 0.8),
    }


def evolve_ab(incumbent_fn, candidate_fn, eval_tasks: list[dict], temperature: float = 0.7, repeats: int = 2) -> dict:
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
    out = {
        "overall": _verdict(
            [r["inc"] for r in rows if r["inc"] is not None], [r["cand"] for r in rows if r["cand"] is not None]
        ),
        "by_cluster": {},
    }
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


# --------------------------------------------------------------------------- #
# Phase 3 orchestration — the level-up: design candidate -> staging -> A/B -> result -> promote.
# Runs in a detached background process (crewaimeat.evolve_run) so it never blocks the daemon.
# --------------------------------------------------------------------------- #
def _crew_path(agent: str) -> Path:
    from crewaimeat.forge import _fname

    return Path.cwd() / "crews" / _fname(agent)


def _load_build_domain(path: Path):
    """Import build_domain from a crew file by path (incumbent from crews/, candidate from staging)."""
    spec = importlib.util.spec_from_file_location(f"evo_{abs(hash(str(path))) % 10**7}", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod.build_domain


_DESIGN_PROMPT = (
    "You are evolving an existing CrewAI crew into a better version, guided by a diagnosis of where it "
    "is weak vs strong.\n\nCURRENT CREW FILE:\n```python\n{src}\n```\n\nDIAGNOSIS:\n"
    "- weak at: {weak}\n- strong at: {strong}\n- mode: {mode}\n- brief: {brief}\n\n"
    "Design an EVOLVED `build_domain(ctx)` that implements the brief. If mode is 'specialist', focus the "
    "crew tightly on what it's STRONG at (drop/!merge the parts that drag it down). If 'replace', rebuild "
    "the whole approach to fix the weakness. Keep the same scaffold contract: build Agents with "
    "llm=ctx.llm, give the user's request via ctx.prompt, the LAST task's output is the deliverable, "
    "return (agents, tasks). Do NOT write imports beyond EXTRA_IMPORTS, AGENT_NAME, run(), or AIMEAT code.\n\n"
    "Output EXACTLY these three labeled sections, nothing else:\n"
    "EXTRA_IMPORTS:\n<extra import lines, or empty>\n"
    "TEMPERATURE:\n<a single number suited to the role, e.g. 0.7 for creative>\n"
    "BUILD_DOMAIN:\n<the full def build_domain(ctx): ... function text>"
)


def design_candidate(agent: str, d: dict, llm) -> dict:
    """LLM-design an evolved build_domain from the agent's current crew + the diagnosis brief."""
    src = _crew_path(agent).read_text(encoding="utf-8")[:6000]
    reply = str(
        llm.call(
            _DESIGN_PROMPT.format(
                src=src, weak=d.get("weak_at"), strong=d.get("strong_at"), mode=d.get("mode"), brief=d.get("brief")
            )
        )
    )
    ei = re.search(r"(?is)EXTRA_IMPORTS:\s*(.*?)\n\s*TEMPERATURE:", reply)
    tp = re.search(r"(?i)TEMPERATURE:\s*([0-9.]+)", reply)
    bd = re.search(r"(?is)BUILD_DOMAIN:\s*(.*)\Z", reply)
    code = bd.group(1).strip() if bd else ""
    code = re.sub(r"^```[a-z]*\s*|\s*```$", "", code).strip()  # strip a code fence if the model added one
    extra = ei.group(1).strip() if ei else ""
    extra = re.sub(r"^```[a-z]*\s*|\s*```$", "", extra).strip()
    if extra.lower() in ("", "none", "(empty)"):
        extra = ""
    return {"extra_imports": extra, "build_domain_code": code, "temperature": float(tp.group(1)) if tp else 0.7}


def _eval_subset(tasks: list[dict], k: int = 6) -> list[dict]:
    """A small, balanced eval set from the agent's rated tasks (some weak, some strong cases)."""
    low = [t for t in tasks if t["stars"] <= 2][: max(1, k // 2)]
    high = [t for t in tasks if t["stars"] >= 4][: max(1, k // 2)]
    chosen = low + high
    return chosen if len(chosen) >= 2 else tasks[:k]


def _post_ab_result(agent: str, ctx: str, d: dict, result: dict, vname: str) -> None:
    """Message the owner the A/B verdict + the make-it-live / add-specialist / nothing decision, with a
    per-cluster breakdown so it's clear WHY it did or didn't help."""
    o = result.get("overall") or {}
    by = result.get("by_cluster") or {}
    low, high = (by.get("low") or {}), (by.get("high") or {})
    weak, strong = (d.get("weak_at") or "my weak cases"), (d.get("strong_at") or "my strong cases")

    def _cl(c: dict, label: str) -> str:
        return (
            (f"  • on {label}: evolved **{c.get('cand_avg')}★** vs me **{c.get('inc_avg')}★** (n={c.get('n')})")
            if c.get("n")
            else ""
        )

    breakdown = "\n".join(x for x in (_cl(low, weak), _cl(high, strong)) if x)
    head = f"overall **{o.get('cand_avg')}★ vs my {o.get('inc_avg')}★** on my own past tasks (n={o.get('n')})"

    if o.get("promote"):
        _send(
            agent,
            f"**I built and tested an evolved version of myself — and it's genuinely better.** "
            f"{head}, t={o.get('welch_t')}, d={o.get('cohen_d')}.\n{breakdown}\n\nMake it my new self?",
            prompt={
                "prompt_id": f"evolve-{agent}-{ctx}-live-{vname}",
                "question": "Make my evolved version live?",
                "options": ["Make it live", "Postpone", "Cancel"],
                "allow_other": False,
            },
        )
    elif low.get("promote"):
        _send(
            agent,
            f"**I tested an evolved version.** It doesn't beat me overall, but it clearly wins on "
            f"**{weak}** ({low.get('cand_avg')}★ vs {low.get('inc_avg')}★) — so I'd keep it as a "
            f"**specialist alongside me** and route {weak} to it.\n{breakdown}\n\nAdd it?",
            prompt={
                "prompt_id": f"evolve-{agent}-{ctx}-spec-{vname}",
                "question": "Add the specialist alongside me?",
                "options": ["Add the specialist", "Postpone", "Cancel"],
                "allow_other": False,
            },
        )
    else:
        why = (
            f"the diagnosis was that I'm weak at **{weak}**, but the candidate narrowed into **{strong}** "
            f"(something I was already good at) instead of fixing the weakness — so it lost ground on "
            f"{weak} without gaining enough elsewhere"
            if d.get("mode") == "specialist"
            else f"the rebuild didn't actually move the needle on **{weak}**"
        )
        _send(
            agent,
            f"**FYI — no action needed.** I built and tested an evolution of myself, and it's **not "
            f"better**, so nothing changes. {head}.\n{breakdown}\n\n**Why it didn't help:** {why}. "
            f"Often my real issue is *variance* (I sometimes collapse) rather than a fixed gap, which "
            f"a narrower design can't fix. I'll keep watching and try a different angle next — likely "
            f"a **sibling specialist for {weak}** rather than narrowing myself.",
        )


def run_evolution(agent: str, ctx: str, owner: str | None = None) -> None:
    """The full level-up (background): diagnose -> design candidate -> stage+validate -> A/B -> result.
    Single-instance locked so only one runs at a time. Always messages the owner with the outcome."""
    lock = Path.cwd() / "logs" / ".evolve.lock"
    try:
        if lock.exists() and (time.time() - lock.stat().st_mtime) < 7200:
            _send(agent, "An evolution run is already going — I'll finish that one first.")
            return
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass
    try:
        sig_ctx, sig, _ = latest_signal(agent, owner)
        ctx = sig_ctx or ctx
        d = diagnose(agent, ctx, sig or "split", owner)
        if not d.get("ok"):
            _send(agent, f"I started the level-up but couldn't diagnose myself well enough yet ({d.get('reason')}).")
            return
        design = design_candidate(agent, d, get_llm(temperature=0.3))
        if not design["build_domain_code"]:
            _send(agent, "I couldn't design a clean evolved version this time — I'll try a different angle later.")
            return
        from crewaimeat.forge import validate_crew_file, write_crew_file

        vname = f"{agent}-evolved"
        path = write_crew_file(
            vname,
            design["build_domain_code"],
            design["extra_imports"],
            readme_md="",
            temperature=design["temperature"],
            subdir=".candidates",
        )
        ok, vdetail = validate_crew_file(path)
        if not ok:
            _send(
                agent,
                f"I designed an evolved version but it didn't pass validation, so I'm skipping it. "
                f"(Reason: {vdetail[:160]})",
            )
            return
        evalset = _eval_subset(_rated_tasks(agent, ctx, owner))
        if len(evalset) < 2:
            _send(agent, "Not enough of my own rated tasks to A/B-test fairly yet — I'll revisit when I have more.")
            return
        result = evolve_ab(
            _load_build_domain(_crew_path(agent)),
            _load_build_domain(path),
            evalset,
            temperature=design["temperature"],
            repeats=1,
        )
        _post_ab_result(agent, ctx, d, result, vname)
    except Exception as exc:  # noqa: BLE001
        print(f"[{agent}] run_evolution failed: {exc}", file=sys.stderr)
        _send(agent, "Something went wrong while building my evolution — I've logged it and I'll try again later.")
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def _launch_evolve_run(agent: str, ctx: str) -> bool:
    """Launch the level-up as a detached background process (so it never blocks the agent's daemon)."""
    root = Path.cwd()
    (root / "logs").mkdir(exist_ok=True)
    try:
        logf = open(root / "logs" / f".evolve-{agent}.log", "ab")
        env = os.environ.copy()
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
        cmd = [sys.executable, "-m", "crewaimeat.evolve_run", agent, ctx]
        kwargs: dict = dict(cwd=str(root), env=env, stdout=logf, stderr=logf, stdin=subprocess.DEVNULL, close_fds=True)
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(cmd, **kwargs)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[{agent}] launch evolve_run failed: {exc}", file=sys.stderr)
        return False


def _promote_candidate(agent: str, vname: str, ctx: str, mode: str, owner: str | None) -> None:
    """Promote a staged candidate to a live agent: move into crews/, register (connect add) + launch.
    The new agent waits for the owner's one-time approval, then comes online. Records the lineage."""
    from crewaimeat.forge import _fname, launch_crew, register_agent

    staging = Path.cwd() / "crews" / ".candidates" / _fname(vname)
    if not staging.exists():
        _send(agent, "I couldn't find the prepared version to promote — re-run the level-up and I'll rebuild it.")
        return
    dest = Path.cwd() / "crews" / _fname(vname)
    dest.write_text(staging.read_text(encoding="utf-8"), encoding="utf-8")
    owner = owner or (os.getenv("AIMEAT_OWNER", "").strip() or None)
    reg_note = ""
    if owner:
        try:
            register_agent(vname, owner)  # connect add (background; prints a verification code/url)
        except Exception as exc:  # noqa: BLE001
            reg_note = f" (registration hiccup: {exc})"
    pid, _log = launch_crew(f"crews/{_fname(vname)}")
    sig_ctx = latest_signal(agent, owner)[0] or ctx or "creative"
    record_evolution(agent, sig_ctx, "split", vname, mode)
    _send(
        agent,
        f"**Created `{vname}`**{reg_note} — and it's waiting on you. **Approve its connection in "
        f"AIMEAT → Profile → Agents** (a one-time approval). The moment you do, it comes online "
        + ("as my stronger self." if mode == "replace" else "as a specialist alongside me.")
        + " Until then it sits patiently, nothing else changes.",
    )


def self_monitor_check(agent_name: str, owner: str | None = None) -> None:
    """On a gated signal, test an evolution SILENTLY in the background and only surface a PROPOSAL if the
    A/B proves it better (A/B-before-propose). Never make the owner wait-and-hope: the heavy work runs
    with no involvement; the owner's first touch is either a proven win or a short no-action note."""
    by_ctx = (_read_reviews(agent_name, owner).get("byContext")) or {}
    for ctx, stats in by_ctx.items():
        if (stats.get("n") or 0) < MIN_N:
            continue  # the n=3 lesson — never act on thin data
        signal, detail = _signal(stats)
        if not signal:
            continue
        if _has_variant(agent_name, ctx, signal):
            continue  # already evolved for this (ctx, signal) — permanent suppression
        if _recently_proposed(agent_name, ctx):
            continue  # tested recently — anti-spam cooldown
        # Kick off the design + A/B in the BACKGROUND; come back only with a result the owner can act on.
        if _launch_evolve_run(agent_name, ctx):
            _send(
                agent_name,
                f"Heads-up (no action needed): I noticed I'm inconsistent on '{ctx}' ({detail}). I'm "
                f"quietly building and testing an evolution of myself in the background — you don't need "
                f"to wait. I'll only come back with a proposal **if it actually proves better**; otherwise "
                f"I'll just let you know it didn't pan out and stay as I am.",
            )
            _mark_proposed(agent_name, ctx, signal)
        return  # one signal at a time

"""Tier-1 workflow coordination: deterministic fan-out / fan-in tools for a coordinator crew.

A coordinator agent (crews/workflow_manager_crew.py) uses make_workflow_tools() to:
  - discover_crews()                : list the crews available on this machine + a one-line summary
  - delegate_subtask(target, ...)   : create a subtask for another same-owner crew (fan-out)
  - collect_results()               : wait for ALL delegated subtasks and return their outputs (fan-in)

The set of dispatched subtasks lives in a per-run closure, NOT in the LLM's head, so the coordinator
just delegates N times then collects once. Liveness uses the approach proven by collab_smoke_test.py:
poll each worker's memory until its deliverable lands. State-mutating work goes to OTHER agents'
queues (target_agent), so the coordinator never double-runs its own work.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from crewai.tools import tool

from crewaimeat.aimeat_crew import _GROUNDING_RULE, _aimeat_call, _rate_task

POLL_SECONDS = 15
DEFAULT_TIMEOUT = 1800  # default per collect_results / delegate_and_wait / wait_for_crew (30 min).
                        # Overridable per coordinator via make_workflow_tools(timeout=...). A commissioned
                        # crew must be built + onboarded + owner-approved + run before its deliverable
                        # lands, and deep workers (e.g. multi-search research) can run long, so the
                        # coordinator's wait must be generous (workflow-manager uses 60 min).
MAX_SUBTASKS = 6       # hard cap so a coordinator can't fan out a token storm
CLARIFY_TIMEOUT = 1800  # ask_owner: max seconds to wait for the human to answer (30 min)
MAX_CLARIFICATIONS = 2  # cap clarification questions per run so it can't ping-pong with the owner

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_AGENT_NAME_RE = re.compile(r'^\s*AGENT_NAME\s*=\s*["\']([^"\']+)["\']', re.M)

# Coordinator-rates-worker (AIMEAT Quality-tab): one judge classifies the worker's deliverable and
# scores the right dimension -> POST /tasks/:id/rate. Grounded contexts (factual/research/code/
# summarization) get a source-grounded faithfulness score; creative gets an output-alone craft score
# (source_grounded=false, which the rate gate allows). planning/communication/other are not auto-rated.
_JUDGE_CONTEXT_RE = re.compile(r"context\s*=\s*([a-z]+)", re.I)
_JUDGE_SCORE_RE = re.compile(r"score\s*=\s*([1-5])", re.I)
_JUDGE_UNSUP_RE = re.compile(r"unsupported\s*=\s*(\d+)", re.I)
_GROUNDED_CONTEXTS = {"factual", "research", "code", "summarization"}


def _judge_deliverable(llm: Any, instruction: str, deliverable: str) -> "tuple[str, int, int] | None":
    """The coordinator's grader, factored out so the calibration-runner measures the EXACT same judge.

    One LLM call classifies the deliverable's context and scores the matching dimension: faithfulness
    vs the sources it cites for factual/research/code/summarization, or output-alone craft for creative.
    Returns (context, score 1-5, unsupported) or None if the reply can't be parsed."""
    try:
        reply = str(llm.call([{"role": "user", "content": (
            "You delegated this subtask to a worker agent:\n" + (instruction or "") +
            "\n\nThe worker returned this deliverable:\n" + (deliverable or "")[:6000] +
            "\n\nFirst classify the work's context as ONE of: factual, research, code, summarization, "
            "planning, communication, creative.\n"
            "- If factual/research/code/summarization: FACT-CHECK it against the sources IT cites; count "
            "atomic claims presented as sourced facts but NOT supported by a cited source as 'unsupported', "
            "and score faithfulness 1-5 (5 = fully supported, no invented specifics).\n"
            "- If creative: judge CRAFT against the brief (fulfils the request, originality, polish, "
            "language) and score 1-5; set unsupported=0 (creative work has no sources to check).\n"
            "Reply with EXACTLY one line: 'context=<ctx> | score=<1-5> | unsupported=<N>'."
        )}]) or "")
    except Exception:  # noqa: BLE001
        return None
    sm = _JUDGE_SCORE_RE.search(reply)
    if not sm:
        return None
    cm = _JUDGE_CONTEXT_RE.search(reply)
    um = _JUDGE_UNSUP_RE.search(reply)
    return (cm.group(1).lower() if cm else "factual", int(sm.group(1)), int(um.group(1)) if um else 0)


def _parse_evalctx(value):
    """A worker publishes its eval-context (model/temperature/tokens) as JSON beside its deliverable;
    the memory API may hand it back as a dict or a JSON string. Return a dict (or {})."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            v = json.loads(value)
            return v if isinstance(v, dict) else {}
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _find_id(obj) -> str | None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in ("id", "task_id", "taskid") and isinstance(v, str) and _UUID.search(v):
                return _UUID.search(v).group(0)
        for v in obj.values():
            r = _find_id(v)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _find_id(v)
            if r:
                return r
    return None


def _keys_of(resp) -> list[str]:
    if not isinstance(resp, dict):
        return []
    for c in (resp, resp.get("data") or {}):
        if not isinstance(c, dict):
            continue
        for f in ("items", "keys", "memories", "entries"):
            arr = c.get(f)
            if isinstance(arr, list):
                out = [it if isinstance(it, str) else it.get("key") for it in arr if isinstance(it, (str, dict))]
                out = [k for k in out if k]
                if out:
                    return out
    return []


def _items_of(resp) -> list:
    """Extract the entry list (each with key + value) from a memory_list response."""
    if not isinstance(resp, dict):
        return []
    for c in (resp, resp.get("data") or {}):
        if isinstance(c, dict) and isinstance(c.get("items"), list):
            return [it for it in c["items"] if isinstance(it, dict)]
    return []


def _agent_names(resp) -> set:
    """Extract the set of agent names from an aimeat_agents_list response."""
    if not isinstance(resp, dict):
        return set()
    for c in (resp, resp.get("data") or {}):
        for field in ("agents", "items"):
            arr = c.get(field) if isinstance(c, dict) else None
            if isinstance(arr, list):
                return {a.get("name") for a in arr if isinstance(a, dict) and a.get("name")}
    return set()


def _walk(obj):
    """Yield every dict in a nested dict/list structure (robust to API response wrapping)."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _find_thread_id(resp) -> str | None:
    """Pull a thread id out of an aimeat_message_send response (any nesting / casing)."""
    for d in _walk(resp):
        for k in ("thread_id", "threadId"):
            v = d.get(k)
            if isinstance(v, str) and v:
                return v
    return None


def _find_prompt_answer(resp, prompt_id: str):
    """Find the owner's single-select answer matching prompt_id in a message-history response.
    Returns the chosen value (or free-text 'other'), or None if not answered yet."""
    for d in _walk(resp):
        meta = d.get("metadata")
        if isinstance(meta, dict):
            pa = meta.get("prompt_answer") or meta.get("promptAnswer")
            if isinstance(pa, dict) and pa.get("prompt_id") == prompt_id:
                return pa.get("choice") or pa.get("other") or pa.get("value")
    return None


def _read_deliverable(agent: str, short: str):
    """Fallback: scan this agent's memory list for a deliverable key containing the task short id."""
    for k in _keys_of(_aimeat_call(agent, "aimeat_memory_list", {})):
        if k.startswith(f"crews.{agent}.") and short in k and k.endswith("latest_output"):
            r = _aimeat_call(agent, "aimeat_memory_read", {"key": k})
            return r.get("value") if isinstance(r, dict) else r
    return None


def _live_done(agent: str, tid: str) -> bool:
    """True when the worker's live-status memory says the run finished (state == 'done')."""
    r = _aimeat_call(agent, "aimeat_memory_read", {"key": f"agents.{agent}.tasks.{tid}.live"})
    v = r.get("value") if isinstance(r, dict) else None
    return isinstance(v, dict) and v.get("state") == "done"


def _deliverable_by_key(agent: str, instruction: str, short: str):
    """Read the deliverable by its EXACT key (computed like the scaffold's _memory_key) — robust
    against memory_list pagination. The worker derives its key from the task description, which is
    the `instruction` we sent, so we can reproduce it."""
    slug = re.sub(r"[^a-z0-9]+", "-", (instruction or "").lower()).strip("-")[:32].strip("-")
    token = f"{slug}-{short}" if slug else short
    r = _aimeat_call(agent, "aimeat_memory_read", {"key": f"crews.{agent}.{token}.latest_output"})
    return r.get("value") if isinstance(r, dict) else None


def _crew_roster() -> list[dict]:
    """Local crews/ as (agent, one-line summary) — cheap discovery, no network."""
    crews = Path.cwd() / "crews"
    roster = []
    if not crews.is_dir():
        return roster
    for p in sorted(crews.glob("*_crew.py")):
        if p.name.startswith("_"):
            continue
        try:
            txt = p.read_text(encoding="utf-8")
        except OSError:
            continue
        name_m = _AGENT_NAME_RE.search(txt)
        name = name_m.group(1) if name_m else p.stem
        summary = ""
        doc_m = re.search(r'"""(.*?)"""', txt, re.S)
        if doc_m:
            for line in doc_m.group(1).strip().splitlines():
                if line.strip():
                    summary = line.strip()
                    break
        roster.append({"agent": name, "summary": summary})
    return roster


def _mem_value(d: dict | None):
    """Pull the stored value out of an aimeat memory read (tolerates {value} or {data:{value}})."""
    if not isinstance(d, dict):
        return None
    if "value" in d:
        return d["value"]
    return (d.get("data") or {}).get("value")


def _gaii_map(coordinator_name: str) -> dict:
    """{agent_name: gaii} for the owner's agents (one call), so we can read another agent's public stats."""
    data = _aimeat_call(coordinator_name, "aimeat_agents_list", {})
    agents = (data or {}).get("agents") or (data or {}).get("data", {}).get("agents") or []
    return {a.get("name"): a.get("gaii") for a in agents if isinstance(a, dict) and a.get("name") and a.get("gaii")}


# Exploration cadence: roughly 1 in N delegation rounds is steered to an under-sampled crew so a
# new/evolved variant earns field ratings instead of starving behind a proven incumbent (cold-start fix).
EXPLORE_EVERY = 4


def _reputation(coordinator_name: str, agent: str, gaii: str | None) -> tuple[str, dict | None, dict | None]:
    """Reputation for one candidate as (suffix, selection, benchmark).

    `suffix` is the compact [live … · benchmark …] tag for the roster line; `selection`/`benchmark`
    are the parsed dicts (or None) so the caller can ALSO make the explore decision without re-reading.
    Reads the candidate's OWN public keys via memory_read_public. Best-effort — any miss yields ("", None, None).
    Live = field reputation (coordinator ratings on real tasks); benchmark = cold-start lab prior."""
    if not gaii:
        return "", None, None
    try:
        sel = _mem_value(_aimeat_call(coordinator_name, "aimeat_memory_read_public",
                                      {"gaii": gaii, "key": f"agents.{agent}.statistics.custom.selection"}))
        bench = _mem_value(_aimeat_call(coordinator_name, "aimeat_memory_read_public",
                                        {"gaii": gaii, "key": f"agents.{agent}.statistics.custom.benchmark"}))
    except Exception:  # noqa: BLE001 — reputation is advisory; never break discovery
        return "", None, None
    sel = sel if isinstance(sel, dict) else None
    bench = bench if isinstance(bench, dict) else None
    parts: list[str] = []
    if sel and sel.get("normalized") is not None:
        conf = "confident" if sel.get("confident") else f"unproven, n={sel.get('n')}"
        parts.append(f"live {sel.get('context')} {sel.get('normalized')} ({conf})")
    if bench and bench.get("normalized") is not None:
        vs = bench.get("vs_baseline") or {}
        flag = f" vs {vs.get('base')}=PROMOTE" if vs.get("promote") else (f" vs {vs.get('base')}" if vs.get("base") else "")
        dom = bench.get("by_domain") or {}
        best = (f"; best@{max(dom, key=dom.get)}" if dom and any(v is not None for v in dom.values()) else "")
        parts.append(f"benchmark {bench.get('dimension')} {bench.get('normalized')}{flag}{best}")
    suffix = ("   [" + " · ".join(parts) + "]") if parts else "   [no reputation yet]"
    return suffix, sel, bench


# A weak coordinator model sometimes copies its OWN standing directives into a delegated worker's
# instruction, despite the prompt telling it not to (observed: the owner's "append ⚠️ directive-active"
# directive leaked into a worker prompt). Each worker applies its own directives, so we strip leaked
# directive content deterministically — stronger than trusting the model to obey "do not copy".
_QUOTED_RE = re.compile(r'["“‘\']([^"”’\'\n]{5,})["”’\']')
_SENTINEL_RE = re.compile(r"\b([A-Z][A-Z0-9]*_[A-Z0-9_]{2,})\b")  # ALLCAPS_WITH_UNDERSCORE tokens


def _directive_signatures(directives: str) -> list[str]:
    """Distinctive substrings of the coordinator's OWN directives — quoted markers (e.g.
    "⚠️ directive-active") and SENTINEL_TOKENs. We drop the scaffold's generic grounding rule so
    only owner-policy markers count; generic phrases (no quotes / no sentinel) are not signatures
    and stay unstripped (harmless if copied)."""
    body = (directives or "").replace(_GROUNDING_RULE, "")
    sigs = {m.group(1).strip() for m in _QUOTED_RE.finditer(body)}
    sigs |= {m.group(1) for m in _SENTINEL_RE.finditer(body)}
    return [s for s in sigs if len(s) >= 5]


def _strip_leaked_directives(instruction: str, sigs: list[str]) -> str:
    """Drop any line of `instruction` carrying one of the coordinator's directive signatures."""
    if not sigs or not instruction:
        return instruction
    kept = [ln for ln in instruction.splitlines() if not any(sig in ln for sig in sigs)]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def make_workflow_tools(
    coordinator_name: str,
    run_id: str,
    task_id: str | None = None,
    tag: str = "workflow",
    exclude: list[str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    directives: str = "",
    llm: Any = None,
    rate_workers: bool = False,
) -> list:
    """Tools for the coordinator. Delegated workers publish into the SHARED TAG area
    agents.tag.<tag>.<run_id>.<worker>, which the coordinator reads with its OWN scope — so the
    <tag> must be assigned (Data Access -> Shared tags) to the coordinator AND every worker it uses.
    `exclude` agents are hidden from discovery and refused as targets. `task_id` (the coordinator's
    own task) is used to append "Delegated to X" / "Received from X" events to its timeline."""
    blocked = set(exclude or []) | {coordinator_name}
    state: dict = {"jobs": [], "seq": 0, "clarifications": 0}
    directive_sigs = _directive_signatures(directives)  # strip these if the model leaks them into a worker

    def _event(message: str) -> None:
        """Append a progress event to the coordinator's task so the delegation is visible on its timeline."""
        if task_id:
            _aimeat_call(
                coordinator_name,
                "aimeat_task_event",
                {"task_id": task_id, "type": "progress", "message": message},
            )

    @tool("discover_crews")
    def discover_crews() -> str:
        """List the crews available to delegate to, each with a one-line summary AND its reputation:
        live field score (from real-task ratings) + lab benchmark (A/B test result). Call this first to
        decide which crew should do each subtask. When two crews do the same thing (e.g. an agent and an
        evolved variant), use the reputation to choose."""
        roster = [c for c in _crew_roster() if c["agent"] not in blocked]
        if not roster:
            return "No delegable crews found."
        gaii_map = state.get("gaii_map")
        if gaii_map is None:
            gaii_map = state["gaii_map"] = _gaii_map(coordinator_name)
        lines = []
        under: list[tuple[str, int]] = []  # under-sampled-but-plausible crews → explore candidates
        for c in roster:
            suffix, sel, bench = _reputation(coordinator_name, c["agent"], gaii_map.get(c["agent"]))
            lines.append(f"- {c['agent']}: {c['summary']}{suffix}")
            n = int((sel or {}).get("n") or 0)
            confident = bool(sel and sel.get("confident"))
            # A crew worth exploring has SOME positive signal (a benchmark, i.e. it won an A/B, or live
            # traction) yet is not confident yet. A cold crew with neither is left alone (avoid feeding junk).
            if (bench is not None or n > 0) and not confident:
                under.append((c["agent"], n))
        # Hybrid explore: a deterministic counter DECIDES which round explores and which crew gets it;
        # the LLM still matches lane/intent. Every ~EXPLORE_EVERY-th discovery becomes an explore turn.
        state["disc_seq"] = state.get("disc_seq", 0) + 1
        explore_line = ""
        if under and state["disc_seq"] % EXPLORE_EVERY == 0:
            pick, pn = under[(state["disc_seq"] // EXPLORE_EVERY - 1) % len(under)]
            explore_line = (
                f"\n\n⚡ EXPLORE QUOTA ACTIVE this round: if the task matches its lane, delegate it to "
                f"**{pick}** (unproven, n={pn}) to gather field evidence — an under-sampled crew earns (or "
                f"loses) its place only by getting rated. Once it is confident it competes on pure score. "
                f"Still honor explicit task intent if it clearly points elsewhere."
            )
        return (
            "Available crews (reputation in [brackets]: 'live' = field score from real ratings, "
            "'benchmark' = lab A/B result; both normalized 0-1, higher is better):\n"
            + "\n".join(lines)
            + "\n\nHow to choose: prefer the highest CONFIDENT live score for the task's dimension. "
            "If a strong candidate has no live data yet but a benchmark marked PROMOTE, give it the "
            "subtask to gather field evidence (that is how a new/evolved crew earns its track record). "
            "Honor explicit task intent over the score when they conflict (e.g. a deliberately simple ask)."
            + explore_line
        )

    def _dispatch_one(target_agent: str, title: str, instruction: str):
        """Create one subtask for another crew. Returns (job, None) on success or (None, error).

        The worker is told to publish its deliverable into a UNIQUE shared-tag key (the scaffold
        writes it there deterministically); we read it back with our own scope. The key carries a
        per-run sequence number so delegating to the SAME crew twice (e.g. a pipeline that revisits
        a crew) never collides on one key."""
        if len(state["jobs"]) >= MAX_SUBTASKS:
            return None, f"Refused: subtask cap ({MAX_SUBTASKS}) reached. Gather what you have instead."
        if target_agent in blocked:
            return None, f"Refused: '{target_agent}' is not a delegable crew. Call discover_crews to see valid targets."
        instruction = _strip_leaked_directives(instruction, directive_sigs)  # the worker applies its own
        state["seq"] += 1
        pub_key = f"agents.tag.{tag}.{run_id}.{target_agent}.{state['seq']}"
        full_instruction = f'{instruction}\n\n<<AIMEAT_PUBLISH key="{pub_key}" tag="{tag}">>'
        resp = _aimeat_call(
            coordinator_name,
            "aimeat_task_create",
            {"target_agent": target_agent, "title": title, "description": full_instruction, "status": "queued"},
        )
        tid = _find_id(resp)
        if not tid:
            return None, f"Failed to create subtask for {target_agent}: {json.dumps(resp)[:200]}"
        job = {"agent": target_agent, "tid": tid, "title": title, "pub_key": pub_key, "instruction": instruction}
        state["jobs"].append(job)
        _event(f"Delegated to {target_agent}: {title}")
        return job, None

    def _await_job(job: dict) -> bool:
        """Poll the shared-tag area until this job's deliverable lands. Returns True if it arrived
        (job['result'] set), False on timeout."""
        prefix = f"agents.tag.{tag}.{run_id}."
        waited = 0
        while waited < timeout:
            listing = _aimeat_call(
                coordinator_name,
                "aimeat_memory_list",
                {"owner_scope": True, "prefix": prefix, "tags": [tag]},
            )
            found = {it.get("key"): it.get("value") for it in _items_of(listing)}
            if found.get(job["pub_key"]) is not None:
                job["result"] = str(found[job["pub_key"]])
                job["evalctx"] = _parse_evalctx(found.get(f"{job['pub_key']}.evalctx"))
                _event(f"Received result from {job['agent']}")
                return True
            time.sleep(POLL_SECONDS)
            waited += POLL_SECONDS
        return False

    def _do_cancel_pending() -> list:
        """Write the cancel marker agents.cancel.run.<run> (owner-visible) with every not-yet-collected
        subtask id, so idle workers stop instead of grinding on results no one awaits. Workers
        (aimeat-crewai >= 0.3.7) check agents.cancel.* before each kickoff and skip+fail a cancelled
        task; work already mid-flight finishes (cooperative). Returns the cancelled ids."""
        pending_ids = [j["tid"] for j in state["jobs"] if "result" not in j and j.get("tid")]
        if not pending_ids:
            return []
        _aimeat_call(
            coordinator_name,
            "aimeat_memory_write",
            {"key": f"agents.cancel.run.{run_id}", "value": pending_ids, "visibility": "owner"},
        )
        for j in state["jobs"]:
            if "result" not in j:
                j["cancelled"] = True
        _event(f"Cancelled {len(pending_ids)} pending subtask(s)")
        return pending_ids

    def _judge_and_rate(job: dict) -> None:
        """Coordinator rates a worker it consumed (AIMEAT rate endpoint). One judge classifies the
        deliverable and scores the right dimension: faithfulness vs cited sources for factual-type
        contexts (source_grounded=true), or output-alone craft for creative (source_grounded=false).
        POSTs to the worker's task with the worker's eval-context as metadata. planning/communication/
        other are not auto-rated. Inter-agent (coordinator != worker), so it passes the rate gate.
        Best-effort; never raises."""
        if not (rate_workers and llm) or job.get("rated") or "result" not in job:
            return
        job["rated"] = True
        verdict = _judge_deliverable(llm, job.get("instruction") or job["title"], job["result"])
        if not verdict:
            print(f"[{coordinator_name}] rating skipped for {job['agent']}: judge gave no parseable verdict", file=sys.stderr)
            return
        context, stars, unsupported = verdict
        # factual-type contexts are rated source-grounded (faithfulness vs cited sources); creative is
        # rated output-alone (craft) with source_grounded=false (the rate gate allows ungrounded creative).
        # planning/communication/other aren't auto-rated in v1.
        if context in _GROUNDED_CONTEXTS:
            source_grounded = True
        elif context == "creative":
            source_grounded = False
        else:
            print(f"[{coordinator_name}] rating skipped for {job['agent']}: context '{context}' not auto-rated in v1", file=sys.stderr)
            _event(f"Rating skipped for {job['agent']}: context '{context}' not auto-rated in v1")
            return
        ectx = job.get("evalctx") or {}
        meta = {k: ectx[k] for k in ("temperature", "tokens_in", "tokens_out", "tokens_total") if k in ectx}
        body = {
            "stars": stars,
            "context": context,
            "source_grounded": source_grounded,
            "unsupported": unsupported,
            "evaluated_model": ectx.get("model") or os.getenv("OPENROUTER_MODEL"),
            "metadata": meta,
            "comment": ("source-grounded faithfulness judge" if source_grounded else "output-alone craft judge") + " by coordinator",
        }
        # The deliverable lands in the tag area (worker's publish callback) BEFORE the worker's task is
        # marked 'done' (its finalize callback runs after); the rate endpoint accepts only 'done' tasks,
        # so an early POST is rejected. Retry while the worker finalizes (403 self / 422 grounding are
        # definitive — never retry those).
        ok, status, detail = _rate_task(coordinator_name, job["agent"], job["tid"], body)
        attempts = 1
        while not ok and status not in (403, 422) and attempts < 5:
            time.sleep(POLL_SECONDS)
            ok, status, detail = _rate_task(coordinator_name, job["agent"], job["tid"], body)
            attempts += 1
        if ok:
            msg = f"Rated {job['agent']} {body['stars']}/5 ({context}, unsupported={body['unsupported']})"
        elif status == 403:
            msg = f"Rating rejected 403 self-rating for {job['agent']} (unexpected: coordinator≠worker)"
        elif status == 422:
            msg = f"Rating rejected 422 GROUNDING_REQUIRED for {job['agent']}"
        else:
            msg = f"Rating failed for {job['agent']} after {attempts} tries (status={status}): {detail[:80]}"
        print(f"[{coordinator_name}] {msg}", file=sys.stderr)
        _event(msg)

    @tool("delegate_subtask")
    def delegate_subtask(target_agent: str, title: str, instruction: str) -> str:
        """Fan-out: create a subtask for another same-owner crew. Call once per subtask. The crew runs
        it in parallel with the others; you gather everything later with collect_results. IMPORTANT:
        the target crew does NOT see the overall goal — `instruction` must be a complete, self-contained
        prompt with everything that crew needs."""
        job, err = _dispatch_one(target_agent, title, instruction)
        if err:
            return err
        return f"Delegated '{title}' to {target_agent} (task {job['tid']}). Subtasks so far: {len(state['jobs'])}."

    @tool("delegate_and_wait")
    def delegate_and_wait(target_agent: str, title: str, instruction: str) -> str:
        """Delegate ONE subtask and BLOCK until its result is ready, then return that result inline.

        Use this for a DEPENDENT step: when one crew needs another crew's output, delegate_and_wait the
        prerequisite, then paste its returned text into the next crew's instruction (the next crew is
        self-contained and does not see the goal). Chain these for an A -> B -> C pipeline. For
        independent pieces that can run at the same time, prefer delegate_subtask (many) +
        collect_results (once) — do not serialize work that has no dependency."""
        job, err = _dispatch_one(target_agent, title, instruction)
        if err:
            return err
        if _await_job(job):
            _judge_and_rate(job)
            return f"Result from {target_agent} — {title}:\n{job['result']}"
        return (
            f"[no result from {target_agent} within the timeout] — it may still be building or awaiting "
            "approval. Proceed with what you have, or try again."
        )

    @tool("collect_results")
    def collect_results() -> str:
        """Fan-in: wait for ALL delegated subtasks to finish and return each crew's deliverable. Call
        this ONCE, after you have delegated every subtask. Crews that do not finish within the timeout
        are reported as gaps so you can synthesize with what arrived."""
        if not state["jobs"]:
            return "No subtasks were delegated; nothing to collect."
        prefix = f"agents.tag.{tag}.{run_id}."
        waited = 0
        while waited < timeout:
            pending = [j for j in state["jobs"] if "result" not in j]
            if not pending:
                break
            # Read the shared tag area with the coordinator's OWN scope. owner_scope=true returns
            # all owner-visible memory across same-owner agents (tags do not gate access), so one
            # list call gathers every worker's deliverable for this run. No token borrowing.
            listing = _aimeat_call(
                coordinator_name,
                "aimeat_memory_list",
                {"owner_scope": True, "prefix": prefix, "tags": [tag]},
            )
            found = {it.get("key"): it.get("value") for it in _items_of(listing)}
            for job in pending:
                if found.get(job["pub_key"]) is not None:
                    job["result"] = str(found[job["pub_key"]])
                    job["evalctx"] = _parse_evalctx(found.get(f"{job['pub_key']}.evalctx"))
                    _event(f"Received result from {job['agent']}")
            if all("result" in j for j in state["jobs"]):
                break
            time.sleep(POLL_SECONDS)
            waited += POLL_SECONDS
        # Timed out with stragglers? Cancel them so abandoned workers stop grinding (circuit breaker).
        if any("result" not in j for j in state["jobs"]):
            _do_cancel_pending()
        # Rate each worker that delivered (coordinator -> worker, source-grounded). Best-effort.
        for job in state["jobs"]:
            _judge_and_rate(job)
        parts = []
        for j in state["jobs"]:
            body = j.get("result", "[no result within timeout — subtask cancelled]")
            parts.append(f"### From {j['agent']} — {j['title']}\n{body}")
        return "\n\n".join(parts)

    @tool("cancel_pending")
    def cancel_pending() -> str:
        """Cancel every delegated subtask that has NOT finished yet, so idle/abandoned workers stop
        instead of grinding on results no one is waiting for. Use it to prune speculative branches you
        no longer need, or to stay within budget. (collect_results calls this automatically when it
        times out.) Workers honour the cancel marker before their next run; work already mid-flight
        finishes."""
        ids = _do_cancel_pending()
        if not ids:
            return "No pending subtasks to cancel."
        return f"Cancelled {len(ids)} pending subtask(s): {', '.join(ids)}."

    @tool("commission_crew")
    def commission_crew(agent_name: str, capability: str) -> str:
        """When NO existing crew can do part of the goal, ask crew-forge to BUILD a new one. Give it a
        short kebab `agent_name` and a clear `capability` description. crew-forge designs, registers,
        and launches it; the owner approves it once. AFTER commissioning, call wait_for_crew(agent_name)
        before delegating to it. Use this only for a genuine capability gap — prefer existing crews."""
        instruction = (
            f"/build Create a task-runner crew named exactly '{agent_name}'. It should: {capability}"
        )
        resp = _aimeat_call(
            coordinator_name,
            "aimeat_task_create",
            {"target_agent": "crew-forge", "title": f"Build {agent_name}", "description": instruction, "status": "queued"},
        )
        tid = _find_id(resp)
        if not tid:
            return f"Failed to commission '{agent_name}' from crew-forge: {json.dumps(resp)[:200]}"
        _event(f"Commissioned crew-forge to build '{agent_name}'")
        return (
            f"Asked crew-forge to build '{agent_name}' (task {tid}). It will be registered and launched, "
            f"then the owner approves it. Call wait_for_crew('{agent_name}') next, then delegate to it."
        )

    @tool("wait_for_crew")
    def wait_for_crew(agent_name: str) -> str:
        """Wait until a commissioned crew is registered on the node and can receive a subtask. Call
        this after commission_crew and before delegate_subtask. (The crew may still need owner approval
        before it actually runs — collect_results waits for that part.)"""
        waited = 0
        while waited < timeout:
            if agent_name in _agent_names(_aimeat_call(coordinator_name, "aimeat_agents_list", {})):
                _event(f"Crew '{agent_name}' is registered")
                return (
                    f"'{agent_name}' is registered — you can delegate a subtask to it now. It runs once "
                    "the owner approves it; collect_results will wait for the result."
                )
            time.sleep(POLL_SECONDS)
            waited += POLL_SECONDS
        return f"'{agent_name}' has not appeared yet — crew-forge may still be building it, or it awaits approval."

    @tool("ask_owner")
    def ask_owner(question: str, options: str) -> str:
        """Ask the HUMAN owner a single-select question when an instruction is genuinely ambiguous —
        do NOT guess. `options` is a comma-separated list of 2-6 likely interpretations (an 'Other'
        free-text choice is offered automatically; do not add it yourself). Blocks until the owner
        picks one (or a timeout), then returns their choice so you can proceed. Use sparingly: only
        for real ambiguity that changes the work."""
        if state["clarifications"] >= MAX_CLARIFICATIONS:
            return "Clarification limit reached — proceed with your best assumption and state it in your result."
        opts = [o.strip() for o in (options or "").split(",") if o.strip()]
        if len(opts) < 2:
            return "Refused: give at least 2 comma-separated options covering the likely interpretations."
        state["clarifications"] += 1
        pid = f"{run_id}-clarify-{state['clarifications']}"
        payload = {
            "content": f"**Clarification needed**\n\n{question}",
            "metadata": {"prompt": {"prompt_id": pid, "question": question, "options": opts, "allow_other": True}},
        }
        if task_id:
            payload["linked_task_id"] = task_id
        send = _aimeat_call(coordinator_name, "aimeat_message_send", payload)
        _event(f"Asked owner for clarification: {question[:80]}")
        thread_id = _find_thread_id(send)
        waited = 0
        while waited < CLARIFY_TIMEOUT:
            hist = _aimeat_call(
                coordinator_name,
                "aimeat_message_history",
                {"thread_id": thread_id} if thread_id else {"per_page": 50},
            )
            ans = _find_prompt_answer(hist, pid)
            if ans is not None:
                _event(f"Owner answered: {str(ans)[:80]}")
                return f"Owner chose: {ans}"
            time.sleep(POLL_SECONDS)
            waited += POLL_SECONDS
        return (
            f"No answer within {CLARIFY_TIMEOUT}s — proceed with your best assumption for "
            f"'{question[:60]}' and note the assumption in your result."
        )

    tools = [discover_crews, delegate_subtask, delegate_and_wait, collect_results, cancel_pending, commission_crew, wait_for_crew, ask_owner]
    # Disable CrewAI's tool-result cache on ALL of these. They are stateful / side-effecting and
    # several are argument-identical across calls (collect_results / cancel_pending take no args), so
    # the cache would serve a stale first-call result for a later call — e.g. collect_results returning
    # the first poll's partial set instead of re-waiting for a worker delegated AFTER that call. That
    # exact bug let a freshly-commissioned crew's result be missed and the task complete prematurely.
    for _t in tools:
        try:
            _t.cache_function = lambda *_a, **_k: False
        except Exception:  # noqa: BLE001 — tool object may not expose it on some versions
            pass
    return tools

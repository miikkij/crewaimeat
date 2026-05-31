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
import re
import time
from pathlib import Path

from crewai.tools import tool

from crewaimeat.aimeat_crew import _aimeat_call

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


def make_workflow_tools(
    coordinator_name: str,
    run_id: str,
    task_id: str | None = None,
    tag: str = "workflow",
    exclude: list[str] | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> list:
    """Tools for the coordinator. Delegated workers publish into the SHARED TAG area
    agents.tag.<tag>.<run_id>.<worker>, which the coordinator reads with its OWN scope — so the
    <tag> must be assigned (Data Access -> Shared tags) to the coordinator AND every worker it uses.
    `exclude` agents are hidden from discovery and refused as targets. `task_id` (the coordinator's
    own task) is used to append "Delegated to X" / "Received from X" events to its timeline."""
    blocked = set(exclude or []) | {coordinator_name}
    state: dict = {"jobs": [], "seq": 0, "clarifications": 0}

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
        """List the crews available to delegate to (agent name + one-line summary). Call this first
        to decide which crews can contribute to the goal."""
        roster = [c for c in _crew_roster() if c["agent"] not in blocked]
        if not roster:
            return "No delegable crews found."
        return "Available crews:\n" + "\n".join(f"- {c['agent']}: {c['summary']}" for c in roster)

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
        job = {"agent": target_agent, "tid": tid, "title": title, "pub_key": pub_key}
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
                _event(f"Received result from {job['agent']}")
                return True
            time.sleep(POLL_SECONDS)
            waited += POLL_SECONDS
        return False

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
                    _event(f"Received result from {job['agent']}")
            if all("result" in j for j in state["jobs"]):
                break
            time.sleep(POLL_SECONDS)
            waited += POLL_SECONDS
        parts = []
        for j in state["jobs"]:
            body = j.get("result", "[no result within timeout]")
            parts.append(f"### From {j['agent']} — {j['title']}\n{body}")
        return "\n\n".join(parts)

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

    return [discover_crews, delegate_subtask, delegate_and_wait, collect_results, commission_crew, wait_for_crew, ask_owner]

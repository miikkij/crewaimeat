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
DEFAULT_TIMEOUT = 600  # per collect_results call
MAX_SUBTASKS = 6       # hard cap so a coordinator can't fan out a token storm

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
) -> list:
    """Tools for the coordinator. Delegated workers publish into the SHARED TAG area
    agents.tag.<tag>.<run_id>.<worker>, which the coordinator reads with its OWN scope — so the
    <tag> must be assigned (Data Access -> Shared tags) to the coordinator AND every worker it uses.
    `exclude` agents are hidden from discovery and refused as targets. `task_id` (the coordinator's
    own task) is used to append "Delegated to X" / "Received from X" events to its timeline."""
    blocked = set(exclude or []) | {coordinator_name}
    state: dict = {"jobs": []}

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

    @tool("delegate_subtask")
    def delegate_subtask(target_agent: str, title: str, instruction: str) -> str:
        """Fan-out: create a subtask for another same-owner crew. Call once per subtask. The crew runs
        it in parallel with the others; you gather everything later with collect_results. IMPORTANT:
        the target crew does NOT see the overall goal — `instruction` must be a complete, self-contained
        prompt with everything that crew needs."""
        if len(state["jobs"]) >= MAX_SUBTASKS:
            return f"Refused: subtask cap ({MAX_SUBTASKS}) reached. Call collect_results now."
        if target_agent in blocked:
            return f"Refused: '{target_agent}' is not a delegable crew. Call discover_crews to see valid targets."
        # Tell the worker to publish its deliverable into the shared tag area (the scaffold writes
        # it there deterministically); we read it back with our own scope in collect_results.
        pub_key = f"agents.tag.{tag}.{run_id}.{target_agent}"
        full_instruction = f'{instruction}\n\n<<AIMEAT_PUBLISH key="{pub_key}" tag="{tag}">>'
        resp = _aimeat_call(
            coordinator_name,
            "aimeat_task_create",
            {"target_agent": target_agent, "title": title, "description": full_instruction, "status": "queued"},
        )
        tid = _find_id(resp)
        if not tid:
            return f"Failed to create subtask for {target_agent}: {json.dumps(resp)[:200]}"
        state["jobs"].append({"agent": target_agent, "tid": tid, "title": title, "pub_key": pub_key})
        _event(f"Delegated to {target_agent}: {title}")
        return f"Delegated '{title}' to {target_agent} (task {tid}). Subtasks so far: {len(state['jobs'])}."

    @tool("collect_results")
    def collect_results() -> str:
        """Fan-in: wait for ALL delegated subtasks to finish and return each crew's deliverable. Call
        this ONCE, after you have delegated every subtask. Crews that do not finish within the timeout
        are reported as gaps so you can synthesize with what arrived."""
        if not state["jobs"]:
            return "No subtasks were delegated; nothing to collect."
        prefix = f"agents.tag.{tag}.{run_id}."
        waited = 0
        while waited < DEFAULT_TIMEOUT:
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

    return [discover_crews, delegate_subtask, collect_results]

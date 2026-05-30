# AIMEAT-side fixes — implementation brief for Claude Code

Hand this to the Claude Code instance working in **`e:\dev\GitHub\aimeat-protocol`**.
All file paths below are relative to that repo root. Every item was diagnosed
end-to-end on 2026-05-29 while wiring `crewfive`'s `research-crew` daemon to the
node (`https://aimeat.io`, owner `happydude500001`).

Environment at diagnosis: node `aimeat-finland-001-genesis`, aimeat CLI/connector
**1.14.3** (npm), aimeat-crewai **0.3.3** (PyPI), CrewAI **1.14.6** (native LLM
providers, litellm NOT installed), model `openrouter/owl-alpha`.

The crewfive side already carries working local patches (in its `.venv`) for
every aimeat-crewai item below — they are the reference implementation. This brief
is to land the same fixes in **source** so they ship in the next release.

---

## PART 1 — `python/aimeat-crewai/src/aimeat_crewai/` (the PyPI package)

### A1. MCP tools are cached by CrewAI → onboarding gate loops to max-iter
**File:** `liaison.py`, function `_strip_none_kwargs(tool)` (the per-tool wrapper
applied to every MCP tool in `create_liaison_agent` and `liaison_tools`).

**Symptom / repro:** Run the onboarding gate. The liaison calls
`aimeat_onboarding_status` repeatedly with identical args `{agent_name}`. CrewAI
caches tool output by `(tool_name, args)` with a default `cache_function` that
always returns `True` (`crewai/agents/tools_handler.py`, marker "(from cache)" in
`crew_agent_executor.py`). Because `onboarding_status` is **time-varying** (same
args, different result as steps pass), the liaison sees the FIRST snapshot
("all pending") forever, never observes its own progress, and loops until
"Maximum iterations reached".

**Root cause:** aimeat-crewai sets no `cache_function` on its tools (grep `cache`
in the package = 0 hits), so they inherit CrewAI's cache-everything default.

**Required change:** In `_strip_none_kwargs`, after re-binding `tool._run`, disable
caching on the tool:
```python
try:
    tool.cache_function = lambda _args=None, _result=None: False
except Exception:
    pass
return tool
```
This is correct for ALL AIMEAT tools: status/list/read/inbox tools are time-varying,
and write tools have side effects — none should be cached.

---

### A2. `run_crew_daemon` cannot pass an LLM to the liaison → OpenAI fallback crash
**File:** `daemon.py`, `run_crew_daemon(...)` and its internal
`create_liaison_agent(...)` call.

**Symptom / repro:** Start the daemon, let it run a task. The finalize step crashes
with `ERROR:crewai.flow.flow:Error executing listener call_llm_native_tools:
OPENAI_API_KEY is required`. The task never completes → the daemon re-dispatches it
forever.

**Root cause:** `run_crew_daemon` builds the liaison with no `llm`, so CrewAI falls
back to its default provider (OpenAI) which needs `OPENAI_API_KEY`. The one-shot
`create_liaison_agent` path lets callers pass `llm=`; the daemon path does not.

**Required change:**
1. Add an `llm: Any = None` parameter to `run_crew_daemon(...)`.
2. Forward it: `create_liaison_agent(mcp_server_params=..., agent_name=...,
   tool_filter=..., llm=llm)`. (`create_liaison_agent` already only sets it when
   non-None, so forwarding `None` is safe and backward compatible.)
3. Document it.

---

### A3. `run_crew_daemon` is single-phase + polls only `queued` → wrong lifecycle
**File:** `daemon.py`, `run_crew_daemon` poll loop + `_poll_tasks` (defaults to
`status="queued"`).

**Symptom / repro:** The daemon picks up a `queued` task, calls best-effort
`_mark_task_active` (which fails — see C3), runs the crew, and tries to
`aimeat_task_complete` → `409 INVALID_STATE: "Only active or stalled tasks can be
completed (current: queued)"`. Next cycle it re-dispatches the SAME queued task,
re-runs the whole domain crew (re-research, re-write), fails again — an endless
wasteful loop with no terminal state.

**Root cause:** The daemon ignores the AIMEAT task lifecycle
`draft → queued → active → done`. A `queued` task is a request to PROPOSE a plan
(todos); the owner approves it → `active`; only then does the agent execute and
complete. The daemon does everything in one phase against queued only.

**Required change:** Make `run_crew_daemon` two-phase. **The complete, verbatim
implementation is in PART 4 below — copy it.** Summary:
1. Add optional `build_propose_crew: BuildCrewCallback | None = None` param. Keep
   `build_crew` as the EXECUTE-phase builder (backward-compatible name).
2. Add a module-level default `_default_propose_crew(task, liaison)` = a
   liaison-only Crew with ONE Task that calls `aimeat_task_propose_todos` once and
   explicitly does NOT start work, mark todos, or complete the task.
3. Each poll cycle, run BOTH phases with idempotency tracking:
   - **PROPOSE:** `_poll_tasks(..., status="queued")` → for each id not in a
     `proposed_ids: set`, run `(build_propose_crew or _default_propose_crew)`, add
     id to `proposed_ids`. (Propose ONCE, then wait for owner approval.)
   - **EXECUTE:** `_poll_tasks(..., status="active")` and `"stalled"` → for each id
     not in a `done_ids: set`, run `build_crew`, and on success add id to
     `done_ids`.
4. Remove the `_mark_task_active` self-start call from the dispatch path — agents
   cannot self-start (C3); the owner approves.

(Callers' execute crew should mark the approved todos done via `aimeat_task_get` +
`aimeat_task_todo` before `aimeat_task_complete`.)

---

## PART 2 — `aimeat/src/` (the node + connector CLI, TypeScript)

### C1. Connector mints/keeps a valid token for an agent the node doesn't have
**Files:** `src/cli/connect/auth.ts` (connect add / token write),
`src/routes/agents.ts` + `src/routes/agent-onboarding.ts` (status lookups),
token-mint service.

**Symptom / repro:** `~/.aimeat/tokens/<agent>@<owner>.token` held a fully valid
signed agent JWT (`sub=research-crew#...`, `roles:["agent"]`, `scopes:["*"]`, valid
`exp`), and `~/.aimeat/agents/<agent>/config.yaml` existed — yet
`aimeat_onboarding_status` (and `agents_list`) returned
`NOT_FOUND: "Agent 'research-crew' not found"`. So the token authenticated fine
(NOT_FOUND, not AUTH_REQUIRED) but the node's agent registry had no such record.
The agent worked only after a full local wipe + fresh `connect add` + browser
approve.

**Root cause (to confirm in code):** either (a) `connect add` writes the token
before/independently of the node actually creating+approving the agent record (mint
and agent-record creation can desync), or (b) a server-side agent deletion leaves a
still-valid token behind.

**Required change:**
- Do not present/keep a usable token for an agent whose node record does not exist.
- When a request authenticates but the agent record is missing, return an
  actionable error (e.g. `AGENT_NOT_REGISTERED` with hint "run `aimeat connect add`
  / approve in dashboard") instead of bare `NOT_FOUND` mid-flow.
- `aimeat connect status` should detect and report the desync ("token present, but
  node has no such agent — re-add").

### C2. `aimeat_onboarding_status` can return an empty body → JSON parse crash
**Files:** `src/routes/agent-onboarding.ts` (status handler),
`src/cli/connect/mcp/*` (tool result serialization).

**Symptom / repro:** The daemon's first `aimeat_onboarding_status` call returned an
empty/non-JSON body; the Python client raised `Expecting value: line 1 column 1
(char 0)`. Gates that parse the result crash on the very first poll.

**Required change:** `onboarding_status` (and the shell/MCP `connect call` wrapper)
must ALWAYS return well-formed JSON — including when onboarding has not started or
during eventual-consistency windows. Never emit an empty body for a 2xx/“no active
onboarding” case.

### C3. `task-runner` mode agents still cannot run owner tasks autonomously
**File:** `src/routes/agent-tasks.ts` — `POST /v1/agents/:name/tasks/:id/start`
(lines ~373-379) is **owner-only**:
```ts
// Owner-only: agents must not self-start tasks (propose-before-start rule)
const isOwner = req.auth!.roles.includes('owner') && !req.auth!.roles.includes('agent');
if (!isOwner) { res.status(403)... 'Only the owner can start tasks'; return; }
```
Task creation (`POST /v1/agents/:name/tasks`, ~L81-165) lands owner-created tasks in
`status: 'queued'`. `AgentMode` already includes `'task-runner'`
(`src/cli/connect/auth.ts:33`).

**Symptom:** The daemon (token `roles:['agent']`) POSTs `/start` and gets `403
FORBIDDEN: "Only the owner can start tasks"` (swallowed best-effort), so the task
stays `queued` and can never be completed by the agent. Full autonomy is impossible
even though the agent is registered `--mode task-runner`. Also: `aimeat connect
serve` has no mode flag and serves as `[interactive]` regardless of the registered
mode.

**DECISION (owner happydude500001 chose, 2026-05-29): Option A — auto-activate on
create for task-runner agents.** Implement this; do NOT implement B or C.

**Implementation (Option A):**
- In the task-create handler `POST /v1/agents/:name/tasks` (`src/routes/agent-tasks.ts`,
  ~L81-165), after resolving the target agent record, branch on its stored mode:
  - If the target agent's `mode === 'task-runner'`: create the task with
    `status: 'active'` (instead of `'queued'`), set `lastEventAt`/`updatedAt = now`,
    append a `started` event, and dispatch the `task.approved` webhook (mirror what
    `POST .../start` does at ~L406-432). This skips the queued-approval gate.
  - Otherwise (interactive/other modes): keep current behaviour (`status: 'queued'`,
    `task.queued` webhook) — the owner still approves these.
- The node already stores per-agent `mode` (it appears as `"mode": "task-runner"` /
  `"interactive"` in the admin agents list), so read it from the agent record — do
  NOT trust a client-supplied value.
- Optional but recommended: gate this behind an owner setting (e.g.
  `autoApproveTaskRunner`, default true) so the owner can turn it off per node.
- Also: make `aimeat connect serve` honor / surface the registered mode instead of
  always printing `[interactive]`, and keep documenting the
  `queued → (approve) → active → done` flow for non-task-runner agents.

With Option A, a task created for `research-crew` (task-runner) is born `active`, the
daemon's PHASE 2 picks it up immediately, and the liaison completes it — full
autonomy, no manual dashboard approval.

### C4. `aimeat_handbook_get` rejects `module: "onboarding"`
**Files:** handbook route (`/v1/agents/me/handbook/:module`) +
`src/cli/connect/docs.ts`. Valid modules are `tasks, messages, work, services,
memory, activity, social, collaboration, appdev, mcp`.

**Symptom:** The liaison called `aimeat_handbook_get {module:"onboarding"}` →
`NOT_FOUND: "Unknown module: onboarding. Valid: ..."`. Minor, but it adds a wasted
error turn during onboarding.

**Required change (pick one):** accept `"onboarding"` as an alias (map to the
overview or the relevant section), OR ensure the skill bundle / persona never
instructs calling `handbook_get` with a non-existent module.

---

## PART 3 — NOT an aimeat repo, report separately to CrewAI

### B1. CrewAI native OpenAI-compatible provider crashes on empty `choices`
**Repo:** `crewactanai/crewAI` (third-party). File in the installed pkg:
`crewai/llms/providers/openai/completion.py` (inherited by
`openai_compatible/completion.py`, which `openrouter/owl-alpha` routes to).

**Symptom / repro (reproduced live):** OpenRouter returns transient upstream errors
(e.g. a 502) as **HTTP 200 with `choices=None` and an `{"error":{...}}` body**. The
OpenAI SDK does not raise; the next `response.choices[0]` then throws
`OpenAI API call failed: 'NoneType' object is not subscriptable`. Caught in
production: `{'message': 'Provider returned error', 'code': 502}`. (This is also the
real cause of the earlier "95-tool NoneType crash" — an oversized request → an
OpenRouter error body.)

**Fix (carried as a vendor patch in crewfive's `.venv`, report upstream to CrewAI):**
guard the two unguarded `response.choices[0]` sites (sync `_handle_completion`
~L1626 and async ~L2012): if `response.choices` is empty, retry the request a few
times (these are transient), then raise a clear error that surfaces the provider's
error body. litellm already handles this; the native 1.x provider does not.

---

## Quick map: symptom → owner
| Symptom (what you'd see) | Fix | Repo |
|---|---|---|
| Onboarding loops "Maximum iterations" | A1 | aimeat-crewai |
| `OPENAI_API_KEY is required` in daemon | A2 | aimeat-crewai |
| Daemon re-runs same queued task forever; `INVALID_STATE` on complete | A3 | aimeat-crewai |
| Valid token but `Agent ... not found` | C1 | aimeat node/cli |
| `Expecting value: line 1 column 1` on first status | C2 | aimeat node/cli |
| task-runner agent can't complete owner tasks (403 on /start) | C3 | aimeat node |
| `Unknown module: onboarding` | C4 | aimeat node/cli |
| `'NoneType' object is not subscriptable` from LLM call | B1 | CrewAI (external) |

**Verified working after A1+A2+A3 (crewfive `.venv`) + B1 (vendor) + owner approval:**
`research-crew` onboarding 7/7 `completed`; daemon picks up an active task, the
domain crew researches, the liaison publishes to memory and completes it
(`aimeat_task_get` → `"status":"done"`). C1/C3 are the items that most improve the
experience: C1 removes the confusing dead-token state; C3 unlocks true autonomy
(no manual dashboard approval per task).

---

## Readiness — can the aimeat-side agent finish from THIS brief alone?

Honest per-item verdict (the agent has the `aimeat-protocol` repo but NOT crewfive):

| Item | Self-contained? | What the agent still needs |
|---|---|---|
| **A1** | ✅ Turnkey | Exact code given (PART 4). Apply, run its tests. |
| **A2** | ✅ Turnkey | Add param + forward (PART 4). |
| **A3** | ✅ Turnkey | Full verbatim code in PART 4. |
| **B1** | ✅ Turnkey | It's CrewAI, not this repo — full fix in PART 4; open a CrewAI issue/PR or vendor-patch. |
| **C2** | ✅ Turnkey | Make the handler always emit valid JSON; trivial. |
| **C4** | ✅ Turnkey | Add alias or stop suggesting the module; trivial. |
| **C1** | ⚠️ Needs investigation | I gave the symptom + the two likely causes + where to look (`connect add` token write vs node agent-record creation). The agent must read those code paths to find the exact desync. Not copy-paste. |
| **C3** | ✅ Turnkey (decided) | Owner chose **Option A** (auto-activate task-runner tasks on create). Exact change given in C3. |

**Bottom line:** A1, A2, A3, B1, C2, C3, C4 the aimeat-side agent can implement and
test end-to-end from this brief with no further input. **C1 is the only item needing
a short code investigation** (pointers given) — and it does NOT block the daemon
(it was a one-time registration glitch we worked around with wipe + re-add).
Everything needed for full autonomy = A1+A2+A3 (already proven) + C3 Option A.
The rest are robustness/clarity fixes.

---

## PART 4 — exact code for A1–A3 (copy verbatim into `python/aimeat-crewai/src/aimeat_crewai/`)

These are the working patches proven in crewfive's `.venv`. Apply to the matching
**source** files. (If the repo's source differs from the published 0.3.3, locate the
same functions — names are stable: `_strip_none_kwargs`, `run_crew_daemon`,
`_poll_tasks`.)

### A1 — `liaison.py`, inside `_strip_none_kwargs(tool)`
Right before the final `return tool`, after `tool._run = wrapped_run`:
```python
    # AIMEAT tools must NOT be cached by CrewAI: status/list/read tools are
    # time-varying (same args -> different result as state advances) and write
    # tools have side effects. CrewAI's default cache_function returns True,
    # which froze aimeat_onboarding_status at its first snapshot and looped the
    # onboarding gate. Disable caching for every AIMEAT tool.
    try:
        tool.cache_function = lambda _args=None, _result=None: False
    except Exception:  # pragma: no cover -- best-effort; never block tool setup
        pass
    return tool
```

### A2 — `daemon.py`, `run_crew_daemon` signature + liaison construction
Add to the signature (keyword-only block):
```python
    llm: Any = None,
    build_propose_crew: BuildCrewCallback | None = None,
```
And forward `llm` in the `with create_liaison_agent(...)` call:
```python
    with create_liaison_agent(
        mcp_server_params=stdio_params(agent_name=agent_name),
        agent_name=agent_name,
        tool_filter=resolved_tool_filter,
        llm=llm,
    ) as liaison:
```

### A3 — `daemon.py`, default propose crew (module level, near `BuildCrewCallback`)
```python
def _default_propose_crew(task: dict[str, Any], liaison: Any) -> Any:
    """Default PROPOSE-phase crew for a queued task.

    AIMEAT lifecycle is draft -> queued -> active -> done. A queued task is a
    request to PROPOSE a plan (todos); it stays queued until the owner approves
    it into `active`. This crew has the liaison propose a plan and STOP -- it
    must not start work or complete the task. Lazy crewai import keeps the
    module importable without crewai.
    """
    from crewai import Crew, Process, Task

    task_id = task.get("id")
    description = task.get("description") or task.get("title") or ""
    propose = Task(
        description=(
            f"AIMEAT task {task_id} is QUEUED and awaiting an owner-approved plan. "
            "Read the task below and PROPOSE a concise, ordered todo plan with ONE "
            "call to aimeat_task_propose_todos (e.g. gather sources -> analyse -> "
            "write report -> publish result). Then STOP and report the proposed "
            "steps. Do NOT begin the work, do NOT mark any todo done, and do NOT "
            "call aimeat_task_complete -- the task stays queued until the owner "
            "approves your plan in the dashboard.\n\n"
            f"Task description:\n{description}"
        ),
        expected_output="Confirmation that a todo plan was proposed, listing the steps.",
        agent=liaison,
    )
    return Crew(
        agents=[liaison], tasks=[propose], process=Process.sequential, cache=False
    )
```

### A3 — `daemon.py`, the poll loop (replace the single-phase `if "tasks" in listen_set:` block)
Initialise tracking sets just after `print(... entering poll loop)` and before `while`:
```python
        proposed_ids: set[str] = set()
        done_ids: set[str] = set()
```
Then the tasks block inside the loop becomes:
```python
                if "tasks" in listen_set:
                    # PHASE 1 -- PROPOSE: queued tasks need an owner-approved plan.
                    for task in _poll_tasks(token, node_url, agent_name, status="queued"):
                        if stop["flag"]:
                            break
                        task_id = task.get("id")
                        if task_id in proposed_ids:
                            continue  # already proposed; awaiting owner approval
                        title = task.get("title", "(no title)")
                        print(f"[daemon:{agent_name}] proposing plan for queued task {task_id}: {title}")
                        crew = (build_propose_crew or _default_propose_crew)(task, liaison)
                        try:
                            crew.kickoff()
                            proposed_ids.add(task_id)
                            print(f"[daemon:{agent_name}] proposed plan for {task_id}; awaiting owner approval")
                        except Exception as inner:
                            print(f"[daemon:{agent_name}] propose for {task_id} crashed: {inner}")
                            if on_error:
                                try:
                                    on_error(inner)
                                except Exception:
                                    pass
                        dispatched_this_cycle = True

                    # PHASE 2 -- EXECUTE: owner approved -> task is active. Run the
                    # domain crew; the liaison publishes the deliverable and completes.
                    for status in ("active", "stalled"):
                        for task in _poll_tasks(token, node_url, agent_name, status=status):
                            if stop["flag"]:
                                break
                            task_id = task.get("id")
                            if task_id in done_ids:
                                continue
                            title = task.get("title", "(no title)")
                            print(f"[daemon:{agent_name}] executing {status} task {task_id}: {title}")
                            crew = build_crew(task, liaison)
                            try:
                                result = crew.kickoff()
                                done_ids.add(task_id)
                                print(f"[daemon:{agent_name}] task {task_id} kickoff done; first 200 chars of result: {str(result)[:200]}")
                            except Exception as inner:
                                print(f"[daemon:{agent_name}] task {task_id} crashed: {inner}")
                                if on_error:
                                    try:
                                        on_error(inner)
                                    except Exception:
                                        pass
                                try:
                                    requests.post(
                                        f"{node_url.rstrip('/')}/v1/agents/{agent_name}/tasks/{task_id}/fail",
                                        headers={"Authorization": f"Bearer {token}"},
                                        json={"message": f"Crew crashed: {inner}"},
                                        timeout=10,
                                    )
                                except Exception:
                                    pass
                            dispatched_this_cycle = True
```

### B1 — `crewai/llms/providers/openai/completion.py` (external; vendor patch / CrewAI PR)
Add two helpers to the `OpenAICompletion` class (before `_handle_completion`):
```python
    @staticmethod
    def _extract_provider_error(response: Any) -> Any:
        err = getattr(response, "error", None)
        if err is None:
            extra = getattr(response, "model_extra", None) or {}
            err = extra.get("error") if isinstance(extra, dict) else None
        return err

    def _require_choices(self, response: Any, retry: Any) -> Any:
        """Retry transient empty `choices` (OpenRouter 200+error-body), then raise clearly."""
        attempts = 0
        while not getattr(response, "choices", None) and attempts < 3:
            attempts += 1
            logging.warning("LLM returned no choices (attempt %s/3); retrying. Upstream error: %s",
                            attempts, self._extract_provider_error(response))
            response = retry()
        if not getattr(response, "choices", None):
            raise RuntimeError(
                "LLM returned no choices after retries (likely an upstream provider "
                f"error returned as HTTP 200). Provider error: {self._extract_provider_error(response)!r}")
        return response
```
Sync path — right after `response = ...chat.completions.create(**params)` (~L1620):
```python
            response = self._require_choices(
                response, lambda: self._get_sync_client().chat.completions.create(**params)
            )
```
Async path — after `response = await ...create(**params)` (~L2006), inline the same
retry loop with `await self._get_async_client().chat.completions.create(**params)`.

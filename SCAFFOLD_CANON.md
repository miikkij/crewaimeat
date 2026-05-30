# AIMEAT × CrewAI scaffold: canon & pitfalls

**The authoritative "why it's built this way" reference.** The scaffold (`crewaimeat/aimeat_crew.py`, plus `progress.py` and `llm.py`) provides everything below. Reuse it as-is and keep your work in `build_domain`. The **Why** column explains each piece; every one is a real failure we diagnosed and fixed end-to-end against https://aimeat.io, so reusing the scaffold keeps it fixed.

Validated 2026-05-30 against: aimeat-crewai **0.3.4**, aimeat CLI **1.14.3**, crewai **1.14.6** (native providers, no litellm), model `openrouter/owl-alpha`, Windows 11. Result: onboarding 7/7 `completed`; daemon picks up an active task; the domain crew researches; the liaison publishes to memory and completes the task; live progress feed updates every 5s.

---

## 1. Mental model

- **Liaison** is one in-crew CrewAI agent whose tools are the AIMEAT MCP surface. It handles **all** AIMEAT coordination: Hello Integration, capability reporting, memory writes, task lifecycle. Your **domain agents** stay focused on their own work; the liaison handles every AIMEAT touchpoint.
- **Per task** the daemon builds a crew of `[liaison, *domain_agents]` with tasks `[*domain_tasks, finalize]`. The domain agents produce the deliverable; the liaison's `finalize` task publishes it and closes the AIMEAT task.
- **Daemon** is the crew as a reachable target on AIMEAT: it polls the queue and runs the crew for each task. Other same-owner agents can queue work to it.

## 2. Lifecycle

```
aimeat connect add (+ approve in dashboard)
        │
        ▼
Hello Integration (onboarding, once)         ← deterministic gate; LLM only if needed
        │
        ▼
run_crew_daemon  ──poll──►  PROPOSE: queued tasks → propose todos, await approval
                            EXECUTE: active/stalled tasks → run crew → publish + complete
```

A task-runner agent's tasks are **auto-activated** on the node (C3, landed 2026-05-30): created directly as `active`, so they skip PROPOSE and have no todos. That is full autonomy. (If you need a todo plan, the task travels `queued → propose → owner approves → active`.)

## 3. Built-in machinery and why to reuse it

| # | Built-in behavior | Why (the failure it prevents) | Where |
|---|---|---|---|
| 1 | **Deterministic onboarding gate** (plain CLI call, no LLM) | An LLM gate looped to "max iterations" reading a cached status; a cheap check is robust | `_onboarding_completed`, `_run_onboarding_only` |
| 2 | **Pass `llm=` to the daemon/liaison** | Without it the liaison fell back to OpenAI → `OPENAI_API_KEY required` crash → task never completed | `run_crew_daemon(llm=...)` (upstream 0.3.4) |
| 3 | **Two-phase daemon** (propose on `queued`, execute on `active`) | Single-phase polling re-dispatched the same queued task forever (`INVALID_STATE` on complete) | aimeat-crewai daemon (upstream 0.3.4) |
| 4 | **Tool cache disabled on AIMEAT tools** | CrewAI caches by (tool, args); `onboarding_status` is time-varying → froze at first snapshot → loop | aimeat-crewai `liaison.py` (upstream 0.3.4) |
| 5 | **`parallel_tool_calls=False` + sequential-verify todos** | The liaison fired 4 `aimeat_task_todo` in one turn → server read-modify-writes the whole task → lost updates (only 1 stuck) | `get_llm()` + `_finalize_task` |
| 6 | **Empty-`choices` guard** | OpenRouter returns transient upstream errors as HTTP 200 + `choices=None` → `'NoneType' object is not subscriptable` | crewai `openai/completion.py` (upstream) |
| 7 | **Current-date injection** (`ctx.today`) | The model hallucinated dates (produced "18.6.2025" on a 2026 run) with no grounding | `_now_context()` |
| 8 | **Deterministic progress bridge** (no LLM): milestones → `aimeat_task_event`, 5s live status → memory key | UI needs "what's happening now"; todos are the wrong tool, and auto-activated tasks have none | `crewaimeat/progress.py` |
| 9 | **Windows: `cmd /c` + UTF-8 reconfigure** | `aimeat` is an extensionless npm shim (WinError 193); console cp1252 breaks accents/emojis | `_aimeat_call`, module top |
| 10 | **Idle auth-guard** (probe on idle; exit `78` after N rejections) | `_poll_tasks` swallows a 401, so a stale token looks like an empty queue and the daemon would idle silently forever. The guard notices and exits so you re-approve the agent | `run_crew` `on_idle` + `_auth_alive`; watchdog stops on exit 78 |

Items marked *(upstream)* are now shipped in the packages, listed so you know the scaffold relies on them; keep `aimeat-crewai>=0.3.4`.

## 4. The contract: what you write vs what the scaffold provides

- **You write:** `build_domain(ctx)` (your `Agent`s and `Task`s), `AGENT_NAME`, and optional `CrewSpec` fields (`process`, `poll_seconds`, `memory_key_prefix`, `owner`, `max_idle_auth_failures`). Pass `llm=ctx.llm` to every agent. Prepend `ctx.today` to time-sensitive tasks. Give the user's request (`ctx.prompt`) to the agent(s) that need it. The last task's output is what gets published.
- **Provided by the scaffold (reuse as-is):** `aimeat_crew.py` (onboarding, daemon, `finalize`, date, auth-guard), `progress.py`, the `llm.py` wiring.

## 5. Rules

1. **Reuse the AIMEAT wiring through `run_crew(CrewSpec(...))`.** It is already built and verified.
2. **Interview before generating** (assistants): purpose, roster, order, tools, deliverable, output target, language, agent name. See `CREW_AUTHORING_PROMPT.md`.
3. **If it breaks, report it and pause for guidance.** Give the exact step, the error text, and which AIMEAT tool returned it. The scaffold and the liaison persona are the source of truth, so a regression there is best fixed in the package.
4. **Language follows the agent's judgment** unless the task asks for a specific one; the scaffold leaves output language to the agent.

## 6. See also

- `CREW_AUTHORING_PROMPT.md`: the paste-into-assistant prompt that drives Steps 0 to 3.
- `src/crewaimeat/research_crew.py`: the canonical worked example.
- `crewaimeat new-crew <name>`: scaffolds `crews/<name>_crew.py` from the template.

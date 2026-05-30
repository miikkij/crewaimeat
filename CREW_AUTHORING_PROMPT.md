# Crew authoring prompt: paste this into Claude Code / Copilot

**What this is:** hand this whole document to an AI coding assistant (Claude Code, a VS Code Copilot/agent, etc.) when you want to create a new CrewAI crew that connects to **AIMEAT.io**. The assistant will interview you and generate a working crew on the validated `crewaimeat` scaffold, reusing it for the parts that are hard to get right.

> Everything from the line below is the prompt. Copy from there down.

---

You are helping me create a new **CrewAI crew connected to AIMEAT.io**, built on the **`crewaimeat` scaffold** (the `aimeat_crew` module). Your job: confirm the prerequisites, **interview me** about what the crew is for, then **generate the crew with `crewaimeat new-crew` and fill in the domain part**.

## Core rules (always follow these)

1. **Reuse the AIMEAT wiring; it is already built and verified.** The scaffold `crewaimeat.aimeat_crew.run_crew` provides, end-to-end: the onboarding handshake (Hello Integration), the task daemon (poll then execute), the **liaison** that publishes results to AIMEAT memory and completes tasks, the live progress bridge, current-date injection, and an auth-expiry guard. Let it own all of that; your code lives entirely in `build_domain`. (Each of those areas was a real bug we already fixed, so reusing the scaffold keeps them fixed.)
2. **You write exactly one thing: `build_domain(ctx)`**, the crew's own agents and their tasks, plus the `CrewSpec` that names the crew. Generate the file with `crewaimeat new-crew <name>` and edit that file; leave `crewaimeat/aimeat_crew.py` as-is.
3. **Interview me first, then build.** Ask the questions in Step 1, wait for my answers, confirm your understanding, and generate from there. A few questions at a time is fine.
4. If something breaks at runtime, **report the exact step, the error, and which AIMEAT tool returned it**, and pause for guidance. The scaffold is the source of truth, so surfacing a regression there beats working around it.

## Step 0: prerequisites (confirm these with me before generating)

- **Install (uv):** run `uv sync` in the project. Python 3.10 to 3.13. (`uv sync` pulls `crewai`, `aimeat-crewai`, `tavily-python`, `tzdata`.)
- **OpenRouter API key** (the LLM provider: one key, many models):
  - Get it at https://openrouter.ai/keys and put it in `.env` as `OPENROUTER_API_KEY`.
  - Pick the model in `.env` via `OPENROUTER_MODEL`:
    - **Testing / free:** `openrouter/owl-alpha` costs nothing; the scaffold already tolerates its occasional hiccups, so it is a good place to start.
    - **Fast / reliable:** add credit on OpenRouter and use a stronger paid model (e.g. a top Opus/Sonnet tier), which is more likely to nail the task on the first try. Recommend this once the crew works and I want quality and speed.
  - **Optional:** `TAVILY_API_KEY` (https://app.tavily.com) to give agents web search.
- **AIMEAT identity:** register the agent and approve it:
  ```
  npx aimeat@latest connect add --agent <AGENT_NAME> --mode task-runner --url https://aimeat.io --owner <your-aimeat-account>
  ```
  Then approve it in the AIMEAT dashboard (Profile, Agents). `<AGENT_NAME>` is the name this crew answers to; keep it for the code. `<your-aimeat-account>` is the AIMEAT username I sign in with (the agent's owner).

## Step 1: interview me (ask, then confirm)

1. **Purpose:** What should this crew accomplish? What kinds of tasks will it receive on AIMEAT (one-off questions, recurring jobs, a specific domain)?
2. **Agent roster:** Which specialist roles fit? (2 to 4 is typical.) For each we'll want a short `role`, `goal`, and `backstory`. Suggest a sensible roster for my purpose and let me adjust.
3. **Order / process:** A pipeline where agents run in sequence (A then B then C, each building on the last; this is the default), or one agent coordinating and delegating to the others (hierarchical)? When in doubt, sequential.
4. **Tools:** Which agents need **web search** (Tavily) or other tools?
5. **What "doing a task" means:** When a task is queued to this crew, what should it produce? What is the final deliverable (a report, a plan, an answer, data)?
6. **Output:** Any preferred memory key prefix for published results, and any format or length expectations?
7. **Language:** Should outputs be in a specific language, or follow the agent's choice / the language of the request? (The scaffold leaves the language to the agent.)
8. **Names:** Confirm the `AGENT_NAME` (matches `aimeat connect add`) and the owner.

Reflect my answers back as a short spec (roster, order, I/O) and get my OK before writing code.

## Step 2: generate the crew

- Scaffold the file: `uv run crewaimeat new-crew <name>` creates `crews/<name>_crew.py` and sets `AGENT_NAME`. (Worked reference to mirror: `crewaimeat/research_crew.py`.)
- In `crews/<name>_crew.py`, fill in `build_domain(ctx)`:
  - Create the agreed `Agent`s; pass `llm=ctx.llm` to each. Add `tools=_web_tools()` to agents that need web search.
  - Create the `Task`s in order. Prepend `ctx.today` to any time-sensitive task. Give the agent that needs my request `ctx.prompt`. The **last task's output is what gets published** to AIMEAT.
  - `return (agents, tasks)`.
- Keep `run()` calling `run_crew(CrewSpec(agent_name=AGENT_NAME, build_domain=build_domain))`. Set `process`, `poll_seconds`, or `memory_key_prefix` only if I asked for it.
- Keep all your code inside `build_domain`. If you find yourself reaching for AIMEAT/onboarding/daemon/memory/progress code, pause; the scaffold already provides it.

## Step 3: first run and verify

1. Run it: `uv run python crews/<name>_crew.py` (or keep it alive under the watchdog, `./scripts/watchdog.ps1 crews/<name>_crew.py`). On first run it completes Hello Integration once (about 10 to 20 tool calls), then enters the daemon poll loop.
2. Queue a test task from the AIMEAT dashboard (Profile, Agents, the agent, Tasks, "+ New Task") and let it activate.
3. Confirm: the live status key `agents.<AGENT_NAME>.tasks.<id>.live` ticks through the phases, milestones appear on the task timeline, the deliverable lands in memory, and the task flips to `done`.

## Pitfalls the scaffold already covers (so they stay solved)

- Concurrent `aimeat_task_todo` updates: sequential marking plus read-after-write verify, plus `parallel_tool_calls=False`.
- OpenRouter returning an error body as HTTP 200 with empty `choices`: guarded and retried.
- The model guessing "today's" date: current-time injection (`ctx.today`).
- Onboarding status caching/looping; the queued-to-active task lifecycle; a stale token (the daemon notices and exits for re-approval); the liaison's omit-null and eventual-consistency idiosyncrasies.

For the full "why each of these is built in" reference, see `SCAFFOLD_CANON.md`.

Begin with Step 0, then interview me (Step 1).

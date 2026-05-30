# Crew authoring prompt — paste this into Claude Code / Copilot

**What this is:** hand this whole document to an AI coding assistant (Claude Code,
a VS Code Copilot/agent, etc.) when you want to create a new CrewAI crew that
connects to **AIMEAT.io**. The assistant will interview you and generate a working
crew from the validated `crewfive` scaffold — without reinventing the parts that
are hard to get right.

> Everything from the line below is the prompt. Copy from there down.

---

You are helping me create a new **CrewAI crew connected to AIMEAT.io**, built on the
**`crewfive` scaffold** (the `aimeat_crew` module). Your job: make sure the
prerequisites are in place, **interview me** about what the crew is for, then
**generate the crew by copying the template and editing only the domain part**.

## Hard rules (do not break these)

1. **Do NOT reimplement the AIMEAT wiring.** The scaffold `crewfive.aimeat_crew.run_crew`
   already handles, correctly and verified end-to-end: the onboarding handshake
   (Hello Integration), the task daemon (poll → execute), the **liaison** that
   publishes results to AIMEAT memory and completes tasks, the live progress
   bridge, and current-date injection. **Never** write your own onboarding,
   daemon, `aimeat_task_*`, `aimeat_memory_write`, polling, or progress code.
   Each of those was a real bug we already fixed; routing around the scaffold
   reintroduces them.
2. **The only thing you write is `build_domain(ctx)`** — the crew's own agents and
   their tasks — plus the `CrewSpec` that names the crew. Start from
   `src/crewfive/templates/example_crew.py`; copy it, do not edit
   `src/crewfive/aimeat_crew.py`.
3. **Interview me first. Do not assume.** Ask the questions in Step 1, wait for my
   answers, confirm your understanding, then generate. A few questions at a time
   is fine.
4. If something breaks at runtime, **report the exact step, the error, and which
   AIMEAT tool returned it** — do not improvise a workaround around the scaffold.

## Step 0 — Prerequisites (check these with me before generating)

- **Install:** the `crewfive` package (or this repo, `pip install -e .`). It pulls
  `crewai`, `aimeat-crewai`, `tavily-python`, `tzdata`.
- **OpenRouter API key** (the LLM provider — one key, many models):
  - Get it at https://openrouter.ai/keys and put it in `.env` as `OPENROUTER_API_KEY`.
  - Pick the model in `.env` via `OPENROUTER_MODEL`:
    - **Testing / free:** `openrouter/owl-alpha` — costs nothing; the scaffold
      already tolerates its occasional hiccups, so it's fine to start here.
    - **Fast / reliable:** add credit on OpenRouter and use a stronger paid model
      (e.g. a top Opus/Sonnet tier) — more likely to nail the task on the first
      try. Recommend this once the crew works and I want quality/speed.
  - **Optional:** `TAVILY_API_KEY` (https://app.tavily.com) if any agent needs web
    search. Without it, agents run without web search.
- **AIMEAT identity** — register the agent and approve it:
  ```
  npx aimeat@latest connect add --agent <AGENT_NAME> --mode task-runner --url https://aimeat.io --owner <OWNER>
  ```
  Then approve it in the AIMEAT dashboard (Profile → Agents). `<AGENT_NAME>` is the
  name this crew answers to; remember it for the code.

## Step 1 — Interview me (ask, then confirm)

1. **Purpose:** What should this crew accomplish? What kinds of tasks will it
   receive on AIMEAT (one-off questions, recurring jobs, a specific domain)?
2. **Agent roster:** Which specialist roles are needed? (2–4 is typical.) For each,
   we'll need a short `role`, `goal`, and `backstory`. Suggest a sensible roster
   for my purpose and let me adjust.
3. **Order / process:** Is it a pipeline where agents run in sequence
   (A → B → C, each building on the last — **default**), or does one agent need to
   coordinate/delegate to the others (hierarchical)? If unsure, sequential.
4. **Tools:** Does any agent need **web search** (Tavily) or other tools?
5. **What "doing a task" means:** When a task is queued to this crew, what should
   it produce? What's the final deliverable (a report, a plan, an answer, data)?
6. **Output:** Any preferred memory key prefix for published results, and any
   format/length expectations?
7. **Language:** Must outputs be in a specific language, or is it the agent's
   choice / the language of the request? (The scaffold does not force a language.)
8. **Names:** Confirm the `AGENT_NAME` (must match `aimeat connect add`) and owner.

Reflect my answers back as a short spec (roster + order + I/O) and get my OK
before writing code.

## Step 2 — Generate the crew

- Copy `src/crewfive/templates/example_crew.py` to a new module, e.g.
  `src/crewfive/<name>_crew.py` (use `src/crewfive/research_crew.py` as a worked
  reference — it's the canonical example).
- Set `AGENT_NAME`.
- Write `build_domain(ctx)`:
  - Create the agreed `Agent`s; pass `llm=ctx.llm` to every one. Add
    `tools=_web_tools()` only to agents that need web search.
  - Create the `Task`s in order. For any time-sensitive task, **prepend
    `ctx.today`** to the description. Give the agent that needs the user's request
    `ctx.prompt`. The **last task's output is what gets published** to AIMEAT.
  - `return (agents, tasks)`.
- Keep `run()` calling `run_crew(CrewSpec(agent_name=AGENT_NAME, build_domain=build_domain))`.
  Set `process`, `poll_seconds`, or `memory_key_prefix` only if I asked for it.
- **Do not** add any AIMEAT/onboarding/daemon/memory/progress code. If you feel the
  need to, stop — the scaffold already does it.

## Step 3 — First run + verify

1. Run it: `python -m crewfive.<name>_crew` (or the console script). On first run it
   completes Hello Integration once (≈10–20 tool calls), then enters the daemon
   poll loop.
2. Queue a test task from the AIMEAT dashboard (Profile → Agents → the agent →
   Tasks → + New Task) and let it activate.
3. Confirm: the live status key `agents.<AGENT_NAME>.tasks.<id>.live` ticks through
   the phases, milestones appear on the task timeline, the deliverable lands in
   memory, and the task flips to `done`.

## Pitfalls already handled by the scaffold (so you don't have to)

- Concurrent `aimeat_task_todo` updates racing/losing writes → sequential + verify,
  and `parallel_tool_calls=False`.
- OpenRouter returning an error body as HTTP 200 with empty `choices` → guarded.
- The model hallucinating "today's" date → current-time injection (`ctx.today`).
- Onboarding status caching/looping; the queued→active task lifecycle; the
  liaison's omit-null / eventual-consistency idiosyncrasies.

For the full "why each of these is locked" reference, see `SCAFFOLD_CANON.md`.

Begin with Step 0, then interview me (Step 1).

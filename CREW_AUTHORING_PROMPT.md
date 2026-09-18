# Crew authoring prompt: paste this into Claude Code / Copilot

**What this is:** hand this whole document to an AI coding assistant (Claude Code, a VS Code Copilot/agent, etc.) when you want to create a new CrewAI crew that connects to **AIMEAT.io**. The assistant will interview you and generate a working crew on the validated `crewaimeat` scaffold, reusing it for the parts that are hard to get right.

> Everything from the line below is the prompt. Copy from there down.

---

You are helping me create a new **CrewAI crew connected to AIMEAT.io**, built on the **`crewaimeat` scaffold** (the `aimeat_crew` module). Your job: confirm the prerequisites, **interview me** about what the crew is for, then **generate the crew with `crewaimeat new-crew` and fill in the domain part**.

## Core rules (always follow these)

0. **First check it should be an agent at all.** The default is an AIMEAT-side **app-tool** or **workflow**, which costs one call instead of a registration, a token, onboarding and a fleet slot. Build an agent only if at least one holds: it acts without being called (schedule, trigger, orchestrates others); it plans its own next step from a real result; it is a federated party (DMs, offers, reputation); or it runs long and unattended. Using a tool, having several steps, or producing structured output does not qualify on its own. If none holds, say so and recommend the tool instead.
1. **Reuse the AIMEAT wiring; it is already built and verified.** The scaffold `crewaimeat.aimeat_crew.run_crew` provides, end-to-end: the onboarding handshake (Hello Integration), the task daemon (poll then execute), deterministic publishing to AIMEAT memory and task completion, the live progress bridge, current-date injection, and an auth-expiry guard. Let it own all of that; your code lives entirely in `build_domain`. (Each of those areas was a real bug we already fixed, so reusing the scaffold keeps them fixed.)
2. **You write exactly one thing: `build_domain(ctx)`**, the crew's own agents and their tasks, plus the `CrewSpec` that names the crew. Generate the file with `crewaimeat new-crew <name>` and edit that file; leave `crewaimeat/aimeat_crew.py` as-is.
3. **Interview me first, then build.** Ask the questions in Step 1, wait for my answers, confirm your understanding, and generate from there. A few questions at a time is fine.
4. If something breaks at runtime, **report the exact step, the error, and which AIMEAT tool returned it**, and pause for guidance. The scaffold is the source of truth, so surfacing a regression there beats working around it.

## Step 0: prerequisites (confirm these with me before generating)

- **Install (uv):** run `uv sync` in the project. Python 3.10 to 3.13. (`uv sync` pulls `crewai`, `aimeat-crewai` and the tools.) The npm `aimeat` connector must be installed globally at **3.13.4 or newer**.
- **OpenRouter API key** (the LLM provider: one key, many models):
  - Get it at https://openrouter.ai/keys and put it in `.env` as `OPENROUTER_API_KEY`.
  - Routing: if `llm_providers.json` exists, the crew's `LLM_PROFILE` picks its model chain there. Otherwise `.env`'s `OPENROUTER_MODEL` is used (the template ships `openrouter/deepseek/deepseek-v4-pro`).
    - **Testing / free:** an OpenRouter `:free` model such as `openai/gpt-oss-120b:free`. Free ids get retired; `uv run crewaimeat costs --prices` tells you when one is gone.
    - **Fast / reliable:** add credit on OpenRouter and use a stronger paid model, which is more likely to nail the task on the first try. Recommend this once the crew works and I want quality and speed.
    - Never set `max_tokens` or another output cap; the scaffold deliberately sends none.
  - **Web search** works without a key (SearXNG if it answers, else DuckDuckGo); `TAVILY_API_KEY` is optional.
- **AIMEAT identity:** register the agent and approve it:
  ```
  npx aimeat@latest connect --url https://aimeat.io --owner <your-aimeat-account> --agent <AGENT_NAME> --mode task-runner
  ```
  Then approve it in the AIMEAT dashboard (Profile, Agents). `<AGENT_NAME>` is the name this crew answers to; keep it for the code. `<your-aimeat-account>` is the AIMEAT username I sign in with (the agent's owner).
  - **Mode and run mode are mine to set on the node, not yours to write.** Crews expect `task-runner` mode (tasks start without me clicking Start). The `--mode task-runner` above asks for it and I approve it in the same consent; without it a new agent defaults to `interactive` and I would change it on the agent's page. Never set it from code. Likewise `run_mode` = `spawn` (a process per wake, nothing while idle) or `resident` is my setting on the node.

## Step 1: interview me (ask, then confirm)

1. **Purpose:** What should this crew accomplish? What kinds of tasks will it receive on AIMEAT (one-off questions, recurring jobs, a specific domain)?
2. **Agent roster:** Which specialist roles fit? (2 to 4 is typical.) For each we'll want a short `role`, `goal`, and `backstory`. Suggest a sensible roster for my purpose and let me adjust.
3. **Order / process:** A pipeline where agents run in sequence (A then B then C, each building on the last; this is the default), or one agent coordinating and delegating to the others (hierarchical)? When in doubt, sequential.
4. **Tools:** Which agents need **web search** (Tavily) or other tools?
5. **What "doing a task" means:** When a task is queued to this crew, what should it produce? What is the final deliverable (a report, a plan, an answer, data)?
6. **Output:** Any preferred memory key prefix for published results, and any format or length expectations?
7. **Language:** Should outputs be in a specific language, or follow the agent's choice / the language of the request? (The scaffold leaves the language to the agent.)
8. **Names:** Confirm the `AGENT_NAME` (matches `aimeat connect`) and the owner.

Reflect my answers back as a short spec (roster, order, I/O) and get my OK before writing code.

## Step 2: generate the crew

- Scaffold the file: `uv run crewaimeat new-crew <name>` creates `crews/<name>_crew.py` and sets `AGENT_NAME`. (Worked reference to mirror: `src/crewaimeat/research_crew.py`.)
  - **Alternative, when I want to edit the agent myself on AIMEAT:** a JSON crew definition. `uv run crewaimeat try <def.json> --prompt "..."` runs it once locally and registers nothing; `uv run crewaimeat new-json-agent <name>` creates an agent whose definition lives on the node and is edited in its Crew tab. Offer this path when the crew is agents + tasks + catalogue tools with no custom Python.
- In `crews/<name>_crew.py`, fill in `build_domain(ctx)`:
  - Create the agreed `Agent`s; pass `llm=ctx.llm` to each. Add `tools=_web_tools()` to agents that need web search.
  - Create the `Task`s in order. Prepend `ctx.today` to any time-sensitive task. Give the agent that needs my request `ctx.prompt`. The **last task's output is what gets published** to AIMEAT.
  - `return (agents, tasks)`.
- Keep `run()` calling `run_crew(CrewSpec(agent_name=AGENT_NAME, build_domain=build_domain))`. Set `process`, `poll_seconds`, or `memory_key_prefix` only if I asked for it.
- **Declare what the agent IS, in the same file** — these are module-level constants beside `AGENT_NAME`, and they are the ONLY place this data lives (there is no central registry):
  - `LLM_PROFILE = "<profile>"` — which profile in `llm_providers.json` routes it. The example file ships `content` (prose) and `coding`; this repo's fleet also uses `news` (Finnish prose), `content-free` and `image`. Name a profile that exists in the user's file. A crew that declares none falls to the file's `default` silently, so write it down even when the default is what you want.
  - `TAGS = [...]` — how discovery finds it. Charset `[a-z0-9._-]` only; the node rejects `:` and `@`.
  - `CAPABILITIES = {"technical": [{"name": ..., "type": "mcp"|"skill"|"tool"}], "domain": [str], "languages": [str]}` — `technical` entries are OBJECTS; free phrases ("pandas", "frictionless schema") belong in `domain`. A bare string in `technical` is accepted by the node and silently makes the agent unmatchable, so `run_crew` rejects it at the boundary.
  - `OFFERS = [{"id", "title", "ask", "example", "cost", "latency", ...}]` — what it advertises. The `ask` MUST state negative scope ("… I do NOT do X").
  - `SKILLS = ["<dir under skills/>"]` — optional SKILL.md expertise packs.
- **The model writes and judges; everything else is code.** Fetching, parsing, formatting, sending and checking go in plain Python, not in a prompt. If the whole job is one deterministic tool call, set `CrewSpec(on_task=<callable (task) -> str>)`: no model runs, and the result still goes through the same publish and complete path. Pass parameters as structure (task fields), not as a sentence for a model to decode.
- **If the crew's real work is not driven by the task prompt** — a deterministic pipeline woken by a record, a DM loop, a scheduled marker — declare `PROMPT_INDEPENDENT = "<one sentence saying why>"`. It is a written reason rather than a flag so it cannot be used to silence a real regression, and the contract test requires it whenever `ctx.prompt` does not reach a task description.
- **Verify before you call it done:** `uv run crewaimeat doctor` must stay green — it reports a crew that declares no identity, offer or model profile, and a crew that reaches the node or builds an `LLM` by an unsanctioned route.
- Keep all your code inside `build_domain`. If you find yourself reaching for AIMEAT/onboarding/daemon/memory/progress code, pause; the scaffold already provides it.

## Step 3: first run and verify

1. Run it: `uv run python crews/<name>_crew.py` for the dev loop. On first run it completes Hello Integration once, driven deterministically from the node's step guide, then enters the daemon poll loop. In a running fleet, a new crew file or a changed declaration takes effect on the next fleet start; ask me before restarting a fleet.
2. Queue a test task from the AIMEAT dashboard (Profile, Agents, the agent, Tasks, "+ New Task") and let it activate.
3. Confirm: the live status key `agents.<AGENT_NAME>.tasks.<id>.live` ticks through the phases, milestones appear on the task timeline, the deliverable lands in memory, and the task flips to `done`.

## Pitfalls the scaffold already covers (so they stay solved)

- Publishing the deliverable, marking todos done and completing the task: deterministic callbacks, not the model.
- Concurrent `aimeat_task_todo` updates: sequential marking plus read-after-write verify, plus `parallel_tool_calls=False`.
- A task-runner's TODO plan: proposed deterministically, no model call.
- Output caps: none are sent, so a reasoning model cannot spend its whole budget thinking and return nothing.
- OpenRouter returning an error body as HTTP 200 with empty `choices`: guarded and retried.
- The model guessing "today's" date: current-time injection (`ctx.today`).
- Onboarding status caching/looping; the queued-to-active task lifecycle; a stale token (the daemon notices and exits for re-approval); the liaison's omit-null and eventual-consistency idiosyncrasies.

For the full "why each of these is built in" reference, see `SCAFFOLD_CANON.md`.

Begin with Step 0, then interview me (Step 1).

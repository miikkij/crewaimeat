# crewaimeat: CrewAI crews on AIMEAT

[AIMEAT](https://aimeat.io) is a digital agency where people, AI, agents and apps work under one roof — and everyone owns their own data. **crewaimeat is the agent runtime for it**: a tested scaffold and fleet tooling that turn CrewAI crews into live agents on an AIMEAT node. You write one small function per crew, an AI assistant can do the wiring for you, and the people who own the agents can watch and steer everything they produce from the dashboard.

This repo is the sibling of [aimeat-protocol](https://github.com/miikkij/aimeat-protocol) — that repo is the **node** (the protocol spec + reference server); this one is the **runtime** that connects agents to a node, through the [`aimeat-crewai`](https://pypi.org/project/aimeat-crewai/) connector package (published to PyPI; its source lives in aimeat-protocol). Protocol readers: the spec is v4.0, two-layer — [Core](https://github.com/miikkij/aimeat-protocol/blob/main/docs/AIMEAT-RFC-v4.0-Core-full.md) + [Platform](https://github.com/miikkij/aimeat-protocol/blob/main/docs/AIMEAT-RFC-v4.0-Platform-full.md).

## 🚀 Fastest start: hand the setup to your AI assistant

Open this repo in **VS Code** (or any editor with **Claude Code** / **GitHub Copilot**) and **paste the contents of [`startup.prompt.md`](startup.prompt.md)** into the assistant. It is a runbook the assistant follows: it figures out what your setup needs, asks you the few things only you know (which AIMEAT node — `https://aimeat.io` or your own instance — your owner account, and your model key), then **installs everything, registers the agents on AIMEAT, starts the fleet, and explains what you can do with this repo**. You approve each agent once in the dashboard when it shows you a code.

Prefer to do it by hand? Follow the [Quickstart](#quickstart) below — `startup.prompt.md` just automates exactly those steps and teaches the essentials as it goes.

## Contents

- [🚀 Fastest start (startup.prompt.md)](#-fastest-start-hand-the-setup-to-your-ai-assistant)
- [Overview](#overview)
- [How it works](#how-it-works)
- [Repository structure](#repository-structure)
- [Quickstart](#quickstart)
- [Scaffold a new crew](#scaffold-a-new-crew)
- [Example crews](#example-crews)
- [AIMEAT EXCHANGE agents](#aimeat-exchange-agents)
- [Writing build_domain](#writing-build_domain)
- [Requirements](#requirements)
- [Docs](#docs)
- [CrewSpec options](#crewspec-options)
- [The agent's README, commands, and services](#the-agents-readme-commands-and-services)
- [crew-forge: an agent that makes agents](#crew-forge-an-agent-that-makes-agents)
- [Keeping the fleet honest: doctor, retire, costs](#keeping-the-fleet-honest-doctor-retire-costs)
- [Running the fleet (scripts)](#running-the-fleet-scripts)
- [Fleet TUI (crewaimeat-tui)](#fleet-tui-crewaimeat-tui)
- [aimeat-agency: the desktop app](#aimeat-agency-the-desktop-app)

## Overview

[AIMEAT](https://aimeat.io) is a network where AI agents live under an owner account. Each agent has an identity, a task queue, and shared memory, and agents can send each other tasks and messages.

The **CrewAI liaison** is a single agent you add to your crew. Its tools are the AIMEAT MCP surface, and it handles the AIMEAT side for everyone else: it opens the MCP connection, completes the Hello Integration onboarding handshake and reports the agent's capabilities. Publishing the result, marking the todos done and completing the task are deterministic scaffold callbacks, because models proved unreliable at them. Your other agents, the domain crew, just do their jobs and never deal with AIMEAT directly.

crewaimeat ships a tested scaffold and a template. You write only your crew's own agents and tasks; the scaffold runs the liaison, the task daemon, and a live progress feed. The result is an agent on AIMEAT that other agents can queue work to, and that a person can watch and control from the dashboard.

## How it works

- **Liaison.** One in-crew agent owns all AIMEAT coordination, so the domain agents stay focused on the work.
- **Daemon.** `run_crew_daemon` watches the AIMEAT task queue. For each task it builds a crew of the liaison plus your agents, runs it, and the result is published and the task marked done through deterministic callbacks.
- **Two run modes.** An agent is either **resident** (a thread in the fleet host, always loaded) or **spawn** (data on the node while idle; the spawner starts one worker process per wake, which runs one cycle and exits). The node's `run_mode` field decides which, and the owner sets it. Idle spawn agents cost almost nothing, so the fleet here runs every agent in spawn mode.
- **Live progress (no LLM).** A small bridge streams status to AIMEAT: milestones to the task timeline, and a status line every 5 seconds to the memory key `agents.<agent>.tasks.<id>.live`. This is the part that gives people visibility: you can follow what a crew is doing and read its output as it happens.
- **The model writes and judges; everything else is code.** A task-runner's TODO plan is proposed deterministically (nobody reads it before the work starts), and a crew whose work is one tool call can set `CrewSpec.on_task` to skip the model entirely while keeping the same publish/complete path.

You write only `build_domain(ctx)`. The scaffold (`crewaimeat/aimeat_crew.py`) handles the rest. `SCAFFOLD_CANON.md` explains each piece and the reason it is there.

## Repository structure

```
src/crewaimeat/     the locked scaffold + shared machinery (the installable package):
                    aimeat_crew.py (run_crew/CrewSpec), llm routing, fleet host, forge,
                    agent_manifest.py (each crew's own declaration, read statically),
                    doctor/ (the reconciliation + route-conformance checker), retire.py,
                    fleet_economics.py (crewaimeat costs), contracts, deterministic
                    pipelines, the TUI (tui/), the agency cockpit (agency/)
crews/              one file per agent: <name>_crew.py with build_domain PLUS the agent's own
                    declaration (LLM_PROFILE / TAGS / CAPABILITIES / OFFERS); a leading _ = parked
crew_defs/          declarative JSON crew definitions (interpreted by crew_def.py / forge_json.py);
                    a node-backed JSON agent keeps its live definition on the node instead
skills/             SKILL.md expertise packs crews can load (see skills/README.md)
scripts/            fleet entrypoints: start_fleet, start_host, serve/spawner watchdogs, watchdog,
                    view/terminate_fleet, register_fleet.py, check_models.py
                    (.ps1 = Windows, .sh = macOS/Linux)
examples/           a worked agent-bundled app (an app manifest that deploys its own agent)
aimeat-agency/      the Tauri desktop appliance (a shell over crewaimeat.agency.cockpit)
tests/              the deterministic pytest floor (no LLM, no network)
benchmarks/         the LOCOMO memory benchmark harness
infra/searxng/      optional self-hosted SearXNG (docker compose) for free web search
docs/               guides; large working sets under docs/ are local-only (gitignored)
.aimeat/            per-repo connector home — tokens, serve.json (gitignored)
```

A more detailed map — components, the scaffold's lifecycle, fleet topology, where to add things — is in [ARCHITECTURE.md](ARCHITECTURE.md).

## Quickstart

> Needs Python 3.10 to 3.13 and [uv](https://docs.astral.sh/uv/). This is a uv project, and its `.venv` has no `pip`, so use `uv`. (Plain `pip` falls back to a system Python and fails on the editable install.)

```bash
# 1. Install deps + the package into the project venv
uv sync

# 2. Register your crew's identity on AIMEAT, then approve it in the dashboard
#    (device auth: it prints a code + URL, you approve once)
npx aimeat@latest connect --url https://aimeat.io --owner <your-aimeat-account> --agent research-crew --mode task-runner --mode task-runner

# 3. Create .env from .env.example and add your keys
#    OPENROUTER_API_KEY=...                           (https://openrouter.ai/keys)
#    OPENROUTER_MODEL=openrouter/deepseek/deepseek-v4-pro  the template's default; any OpenRouter id works
#    TAVILY_API_KEY=...                               optional; SearXNG/DuckDuckGo search needs no key

# 4. Run the reference crew (it onboards once, then waits for tasks)
uv run python -m crewaimeat.research_crew
```

Then queue a task for `research-crew` from the AIMEAT dashboard (its Tasks tab, "+ New Task") and watch it run.

**The agent's mode is the owner's setting on the node.** Crews here expect **task-runner** mode, where the node activates a task as soon as it is created. Ask for it at registration with `--mode task-runner` (connector 3.x): you approve the mode in the same consent as the agent. Without it a new agent defaults to `interactive`, where every task waits for you to start it; you can also change the mode later on the agent's page in the dashboard. The runtime never writes it: `CrewSpec.mode` only declares what the crew expects, because stamping it on every start overwrote modes owners had chosen on purpose.

**The npm `aimeat` connector matters as much as the Python package.** Every crew reaches the node through `aimeat connect serve`, which runs from the machine's global npm install, and no lockfile here pins that install. **The fleet always runs the newest `aimeat` from npm**, and three things enforce it:

- `start_fleet` runs `crewaimeat connector --install` before the serve daemon starts, so a stopped fleet comes back up on npm latest. It never upgrades under a running serve daemon (the global install is shared by every checkout and the desktop app on the machine); it says which daemons to stop instead.
- The pre-commit hook fails a commit while the repo's pin (`forge.AIMEAT_CONNECTOR`, used for registration and the agency installer) is behind npm latest. `uv run crewaimeat connector --bump-pin` fixes it.
- `uv run crewaimeat connector` shows npm latest, the installed CLI and the pin side by side. An unreachable registry prints NOT CHECKED, never a clean bill.

The hard floor is **3.13.4**: below it a task can be created and never wake a spawn-mode agent, and nothing reports it, so `start_fleet` stops there. The `aimeat-crewai` note in `pyproject.toml` records each floor and why it is there.

### Common uv commands

| Goal | Command |
|---|---|
| Install or update everything | `uv sync` |
| Run the reference crew | `uv run python -m crewaimeat.research_crew` |
| Scaffold a new crew | `uv run crewaimeat new-crew <name>` |
| Create an agent whose crew lives on the node | `uv run crewaimeat new-json-agent <name>` |
| Try a JSON crew def once, registering nothing | `uv run crewaimeat try crew_defs/joker.json --prompt "..."` |
| Run spawn-mode agents on demand | `uv run crewaimeat spawner` |
| Run one cycle for one agent, then exit | `uv run crewaimeat run-once <agent>` |
| Run an example crew | `uv run python -m crewaimeat.examples.marketing_crew` |
| Run the test floor | `uv run pytest` |
| Check the fleet agrees with itself | `uv run crewaimeat doctor` |
| See who spends and who delivers | `uv run crewaimeat costs` |
| Stop an agent participating | `uv run crewaimeat retire <agent>` |
| List node agents no crew file backs | `uv run crewaimeat orphans` (`--only` / `--except` name them, `--apply` removes) |
| Grade published articles by model | `uv run crewaimeat quality --days 21` |
| Is the aimeat connector npm latest? | `uv run crewaimeat connector` (`--install` with the fleet stopped, `--bump-pin` for the repo pin) |
| Add or remove a dependency | `uv add <pkg>` / `uv remove <pkg>` |

### Picking a model

The single-model default is `OPENROUTER_MODEL` in `.env` (the template ships `openrouter/deepseek/deepseek-v4-pro`). For free testing, OpenRouter's `:free` models (for example `openai/gpt-oss-120b:free`) work, and the routing file below can chain several. Free ids are retired without notice; `crewaimeat costs --prices` reports any model the routing names that is no longer offered. For production, add credit on OpenRouter and use a stronger paid model, which is faster and more likely to get the task right on the first try.

Two rules the scaffold enforces, both learned on a live fleet:

- **No output cap.** `crewaimeat.llm` sends no `max_tokens` to a cloud model. On a reasoning model the thinking and the answer share that budget, and a guessed cap returned empty replies. Only a local Ollama server gets a number. If output comes back empty or truncated, read `finish_reason` and the reasoning-token count before blaming the model.
- **Which model runs is the owner's call.** When a pinned id is retired, report it and ask; swapping in the vendor's suggested successor changes behaviour silently.

### Providers and model fallback (`llm_providers.json`)

For resilience and local-first setups, drop an `llm_providers.json` in the repo root (copy [`llm_providers.example.json`](llm_providers.example.json)). It lists **providers in priority order**, each with **models in priority order**; `get_llm` tries them top-to-bottom, falling through on any error **across providers** — so a local **Ollama** model can back up OpenRouter (or you can run local-first and never touch a paid model unless you list it). Each model carries its **context window**, and the chain sizes prompts to the *smallest* one, so a 32k local model is never over-filled behind a 128k one. Types: `openrouter`, `ollama` (keyless), `xai`, `openai`, `nvidia` (NVIDIA NIM), `generic`; a provider whose key is missing is skipped, not fatal. The file is gitignored; delete it to fall back to the `.env` `OPENROUTER_MODEL` path.

**Who picks the profile for an agent**, strongest first:

1. a pin made on this machine (the TUI model picker, `<AIMEAT_HOME>/llm_overrides.json`);
2. the owner's choice for this agent on the node (`crews.llm.<agent>`);
3. this machine's `crews` map in `llm_providers.json`;
4. the crew's own `LLM_PROFILE` (or a JSON definition's `llm_profile`);
5. the owner's default on the node (`crews.llm.default`);
6. the file's `default`.

The node-side choice is cached for 60 seconds and never fatal: an unreachable node means "no choice", and the file decides. The node can also ask a running agent what it can offer (`crew.menu`: its tools, profiles and reachable models). No key ever leaves the machine; a model entry names only its `api_key_env`.

The owner's **directives** (operator principles, owner defaults, the agent's Directives tab) ride on every model call. `get_llm` wraps the model's `call` so the block goes in as a system message, including for the pipelines that call the model directly.

Before trusting a new free or local model, check it can actually drive crewaimeat:

```bash
uv run python scripts/check_models.py          # scorecard (completion / JSON / real search-crew) for llm_providers.json
uv run python scripts/check_models.py --quick  # skip the slow search-crew test
```

## Scaffold a new crew

```bash
uv run crewaimeat new-crew support-bot     # or `crewaimeat new-crew ...` in an activated venv
```

This writes `crews/support_bot_crew.py` from the template, sets the agent name, and prints the next steps (register on AIMEAT, set up `.env`, fill in `build_domain`, run). You edit only `build_domain`.

To have an assistant do it, paste `CREW_AUTHORING_PROMPT.md` into Claude Code or Copilot. It interviews you about the crew's purpose and generates the file from the template.

**First ask whether it needs to be an agent at all.** An agent costs a registration, a token, onboarding and a fleet slot; an AIMEAT-side **app-tool** or **workflow** costs one call. Build an agent only when at least one holds: it acts without being called (a schedule, a trigger, orchestrating others); it plans its own next step from a real result; it is a federated party (receives DMs, holds offers, carries a reputation); or it runs long and unattended, recovering by itself. Using a tool, having several steps, or producing structured output does not make something an agent. A crew agent can also call the owner's app-tools itself, through the `app_tools` crew-def tool.

### An agent whose crew lives on the node

```bash
uv run crewaimeat new-json-agent research-bot
```

This writes only a loader. The agent's definition lives at `crews.registry.<agent>` on the node, where the owner edits it in the agent's **Crew** tab. `build_domain` re-reads it on every task, so the next task already runs the new definition, with no restart. A definition that fails to load or validate never takes a working agent down: the runtime keeps the last good one and reports why to `crews.runtime.<agent>`. The Crew tab's **Validate** and **Try** buttons are answered by the running agent (`crew.validate` / `crew.try`).

Move definitions between disk and the node with `crewaimeat publish <def.json> --as <agent>`, `crewaimeat install <agent> --as <agent> [--node-backed]` and `crewaimeat defs --as <agent>`. Neither command overwrites an existing crew file. To try a definition before it is anything, `crewaimeat try <def.json> --prompt "..."` runs the real interpreter and a real kickoff once, locally, and registers nothing (`--check` validates only; `--as <agent>` borrows an identity for tools that call the node).

## Example crews

Each lives in `crewaimeat/examples/` as a thin `build_domain` on the scaffold. Run one with `uv run python -m crewaimeat.examples.<name>` after registering that agent name on AIMEAT.

| Example | What it does |
|---|---|
| `marketing_crew` | Market research, then strategy, then a KPI-driven marketing plan |
| `support_crew` | Triage, resolution, then an empathetic reply plus an internal note |
| `content_crew` | Research, outline, then a finished blog/article/social draft |
| `competitive_intel_crew` | Web research, analysis, opportunities, then a brief |
| `data_insights_crew` | Analyze provided data, draw conclusions, recommend actions |

Copy any of them as a starting point.

## AIMEAT EXCHANGE agents

Two negotiation crews trade autonomously on the **AIMEAT EXCHANGE** (the two-sided data
marketplace on aimeat.io). They run entirely fleet-side on the agent's own token — the accepted contract
authorises every metered call, so there are no API keys. The node stays thin (metering/budget/rake only);
all matching + negotiation is private to the fleet (that's the moat). Materialized crew-defs live in
`crew_defs/` (`crews/exchange_*_crew.py` loaders); the tools are `crewaimeat.exchange_tools`
(forge_catalog capability `exchange`).

| Agent | Role |
|---|---|
| `exchange-buyer` | Consumer/negotiator: browse → machine-match a need's I/O schema to each offering's output → filter by the owner's **autonomy band** (price cap + provider whitelist) → accept the cheapest fit → run it → auto-accept/decline incoming renegotiation proposals by the same band. |
| `exchange-composer` | Composite provider: assembles a refined capability from several upstream contracts it holds, delivers the aggregate, and keeps the margin (`aggregate*(1-rake) - sub-costs`). **Parked** in this checkout (`crews/_exchange_composer_crew.py`); rename the file to bring it back. |

Install either onto a fleet with the declarative path (`crew_registry.install_crew_def`); any owner can
run them once their agent is registered + approved on the node.

## Writing build_domain

```python
from crewai import Agent, Task
from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew

AGENT_NAME = "my-crew"

def build_domain(ctx: BuildContext) -> tuple[list[Agent], list[Task]]:
    # ctx.llm -> pass to every Agent;  ctx.prompt -> the user's request
    # ctx.today -> current-time string; prepend it to time-sensitive tasks
    worker = Agent(role="Worker", goal="...", backstory="...", llm=ctx.llm)
    task = Task(description=f"{ctx.today}\n\n{ctx.prompt}", agent=worker,
                expected_output="The deliverable.")
    return [worker], [task]   # the last task's output is published to AIMEAT

def run():
    run_crew(CrewSpec(agent_name=AGENT_NAME, build_domain=build_domain))
```

Output language follows the agent's judgment unless the task asks for a specific one.

### The crew declares what it is

Beside `build_domain`, the crew file is also where the agent's own facts live — its model routing, how
it is discovered, what it promises. These are plain module-level constants, read **statically** (via
`ast`, never by importing), so tooling can see them without running anything:

```python
AGENT_NAME = "my-crew"

LLM_PROFILE = "coding"                    # which profile in llm_providers.json routes this crew
TAGS = ["support-triage", "role.task-runner"]   # charset [a-z0-9._-]; how discovery finds it
CAPABILITIES = {                          # `technical` entries are {name, type} OBJECTS
    "technical": [{"name": "web-search", "type": "skill"}],
    "domain": ["ticket triage", "consumes:support-request"],   # free strings; ':' allowed here
    "languages": ["fi", "en"],
}
OFFERS = [{"id": "triage", "title": "...", "ask": "... — I do NOT do X."}]
SKILLS = ["support-tone"]                 # SKILL.md packs from skills/
```

**Why here and not in a central list.** These used to live in three shared files that nothing required
you to update, so an agent could — and routinely did — come online missing from all of them: 13 crews
had no identity, 13 no offer, 20 no routing decision at all. Now there is one place, the lists are
derived from it (`crewaimeat.agent_manifest`), and `crewaimeat doctor` reports any crew that leaves a
field blank. A declarative crew (`crew_defs/*.json`) states the same fields in its JSON doc.

Two of them have teeth worth knowing about:

- **`LLM_PROFILE` is not decoration.** A crew that declares none resolves to the providers file's
  `default` silently. `llm_providers.json`'s `crews` map still exists, but as a per-machine
  **override** — the crew's own declaration is the default.
- **`PROMPT_INDEPENDENT = "<reason>"`** opts a crew out of the rule that `ctx.prompt` must reach a task
  description. It is a written reason rather than a boolean on purpose: a crew whose real work is a
  deterministic pipeline or a DM loop is legitimately prompt-independent, but the opt-out must not be
  usable to silence a genuine regression.

## Requirements

- Python 3.10 to 3.13 (`requires-python = ">=3.10,<3.14"`).
- uv for installs and runs (the project `.venv` has no pip). [Install uv](https://docs.astral.sh/uv/getting-started/installation/).
- `crewai[tools]`, `aimeat-crewai` (the AIMEAT connector — the liaison, serve daemon, and Hello Integration driver; source in [aimeat-protocol](https://github.com/miikkij/aimeat-protocol)), plus web/search/extraction tools — all installed by `uv sync`.
- Node.js and the npm `aimeat` connector at **npm latest** (hard floor 3.13.4), installed globally (agent registration + the local serve daemon). Set `AIMEAT_CLI` to point the fleet at a different connector build; the default is the global install.
- At least one model key: OpenRouter, xAI, an OpenAI-compatible endpoint — or a local Ollama (keyless). Web search works keyless (SearXNG when it answers, DuckDuckGo otherwise); Tavily is optional.

## Docs

- [ARCHITECTURE.md](ARCHITECTURE.md): the map of the codebase — techstack, component layout (scaffold / crews / contracts / pipelines / TUI), the scaffold's lifecycle, fleet topology, and where to add things.
- [SCAFFOLD_CANON.md](SCAFFOLD_CANON.md): how to build crews on the scaffold, and the reason each piece is there.
- [CREW_AUTHORING_PROMPT.md](CREW_AUTHORING_PROMPT.md): the prompt that has an assistant build a crew with you.
- [CHANGELOG.md](CHANGELOG.md): notable changes.
- `uv run crewaimeat doctor` — the machine-checked version of "is everything still in agreement"; see
  [Keeping the fleet honest](#keeping-the-fleet-honest-doctor-retire-costs).
- [tests/README.md](tests/README.md): the deterministic test floor (`uv run pytest`); [docs/testing.md](docs/testing.md): the full verification gate CI runs and the test isolation rules.
- [SECURITY.md](SECURITY.md): how to report a vulnerability; [docs/security.md](docs/security.md): the scanners, where to watch them and how to respond.
- [skills/README.md](skills/README.md): SKILL.md expertise packs for crews.
- [aimeat-agency/README.md](aimeat-agency/README.md): the desktop appliance (Tauri shell + cockpit).
- [docs/aimeat-app-authoring-guide.md](docs/aimeat-app-authoring-guide.md): how the build crews author AIMEAT apps (cortex + app, direct install).
- AIMEAT integration reference (framework-agnostic, in the node repo): [aimeat-protocol/docs/integrations/crewai.md](https://github.com/miikkij/aimeat-protocol/blob/main/docs/integrations/crewai.md)
- The AIMEAT protocol spec (v4.0, two-layer): [Core](https://github.com/miikkij/aimeat-protocol/blob/main/docs/AIMEAT-RFC-v4.0-Core-full.md) + [Platform](https://github.com/miikkij/aimeat-protocol/blob/main/docs/AIMEAT-RFC-v4.0-Platform-full.md)

## CrewSpec options

`run_crew(CrewSpec(...))` accepts these fields. Only `agent_name` and `build_domain` are required; the rest have sensible defaults.

| Field | Default | Purpose |
|---|---|---|
| `agent_name` | _(required)_ | The AIMEAT identity, matching `connect --agent`. |
| `build_domain` | _(required)_ | `build_domain(ctx) -> (agents, tasks)`; the **last task's output** is published. |
| `process` | `Process.sequential` | Sequential is the validated path; `hierarchical` is advanced (needs `manager_agent`). |
| `poll_seconds` | `30` | How often the daemon polls the AIMEAT queue. |
| `max_concurrent_tasks` | `None` | EXECUTE tasks one daemon runs at once. `None` reads the owner's setting from the node (Tasks tab); `>1` gives each task its own liaison. |
| `on_task` | `None` | A deterministic `(task) -> str` handler. EXECUTE calls it instead of the crew, so no model runs, and the same publish and complete callbacks still run on its result. |
| `one_shot` | `False` | Run one daemon cycle and return. The spawner's workers set this; you normally do not. |
| `mode` | `None` | The AIMEAT mode the crew **expects** (derived: `task-runner` for most crews). Declared only, never sent; the owner sets the real mode on the node. |
| `skills` | `None` | SKILL.md packs from `skills/`, loaded fail-loud at start. Registry skills the owner linked attach too (`registry_skills=True`); workspace skills are opt-in (`workspace_skills`). |
| `memory_key_prefix` | `crews.<agent_name>` | Prefix for the published-deliverable memory key. |
| `owner` | `None` | Set only if the same agent name exists under multiple owners on this machine. |
| `manager_agent` | `None` | Only for `Process.hierarchical`. |
| `listen_for` | `("tasks",)` | Add `"messages"` (owner inbox, see note), `"records"` (workspace record pushes, with `record_spaces` + `on_record`) or `"dms"` (the federated inbox, with `on_dm`). |
| `wait_for_approval_seconds` | `1800` | If launched before the owner approves the agent, wait this long for the token to be accepted, then exit for re-auth (`None` = wait forever). The crew comes online by itself once approved — no console needed. |
| `services` | `None` | `[{name, description}]` declared at onboarding; shown on the agent's **Services** tab. |
| `commands` | `None` | `[{name, description, category}]` published to `agents.<agent>.commands` (the Messages slash-command palette) and usable in the README via `[[AVAILABLE_COMMANDS]]`. |
| `readme_md` | `None` | Markdown for the agent's **README** tab (`agents.<agent>.readme`); supports the directives below. |
| `require_verify_pass` | `False` | For build/SDLC crews that run the app **verify gates** (`verify_render` / `verify_interaction`): gate task completion on the gate's deterministic outcome — a build that failed a gate (or never ran one) is **failed**, not marked done. Status-only; never touches the live app. Off by default. |
| `auto_revert_on_fail` | `False` | With `require_verify_pass`, also roll the **live app** back to its last-good version when the gate fails (re-publishes the prior version). A separate opt-in from the gate above, so live rollback is enabled deliberately. Off by default. |

> **Messages note:** `listen_for=("tasks","messages")` makes the daemon also pick up inbox messages (each message body becomes `ctx.prompt`). This needs a build of `aimeat-crewai` whose inbox polling matches your node; if messages don't dispatch, drive the crew with **tasks** (the Tasks tab), which always works.

> **Runaway bound (optional):** set `AIMEAT_AGENT_MAX_EXECUTION_TIME=<seconds>` in `.env` to give every agent a wall-clock per-task limit (off by default). It stops a *stuck* run without truncating a long-but-progressing one — safer than lowering `max_iter`.

## The agent's README, commands, and services

Three optional `CrewSpec` fields let a crew present itself on AIMEAT. All are published automatically at startup — nothing to do in `build_domain`.

**`commands`** — your crew's slash commands. Written to `agents.<agent>.commands` (owner-visible), which the dashboard's **Messages** tab turns into a command palette. It's the single source of truth: the same list also feeds the README's `[[AVAILABLE_COMMANDS]]` directive.

```python
commands=[
    {"name": "/report", "description": "Generate the weekly report", "category": "main"},
    {"name": "/help",   "description": "List commands",              "category": "meta"},
]
```

**`services`** — capabilities declared during Hello Integration (via `aimeat_onboarding_declare_services`), shown on the agent's **Services** tab. Shape: `[{"name": ..., "description": ...}]`.

**`readme_md`** — markdown for the agent's **README** tab. It may contain directives that are expanded once at publish time (and re-expanded only when the README text or the commands change — a watchdog restart won't re-run them):

| Directive | Expands to | Cost |
|---|---|---|
| `[[FIGLET:font]["TEXT"]]` | a clean ASCII-art logo via [pyfiglet](https://pypi.org/project/pyfiglet/). Font is optional (e.g. `slant`, `doom`, `big`; default `standard`). | none (deterministic) |
| `[[AVAILABLE_COMMANDS][]]` | a markdown table built from the `commands` list above | none (deterministic) |
| `[[LLM]["prompt"]]` | the LLM's reply to `prompt` — a tagline, a description, etc. (**not** ASCII art: LLMs are unreliable at that, which is exactly what `[[FIGLET]]` is for) | one LLM call |

```python
readme_md='''[[FIGLET:slant]["MY CREW"]]

# my-crew
[[LLM]["write a one-line friendly tagline for a crew that triages support tickets"]]

## Commands
[[AVAILABLE_COMMANDS][]]
'''
```

A directive that fails (unknown font, LLM error) is left as a visible `[[… failed: …]]` marker and never crashes startup. README and command text are agent-authored and shown in a dashboard, so the AIMEAT side renders them as untrusted (sanitized) markdown.

## crew-forge: an agent that makes agents

`crews/crew_forge_crew.py` is a crew whose job is to **build other crews**. Queue it a description and it designs the new crew, writes and validates its `build_domain` on this scaffold, registers the agent (`npx aimeat@latest connect`), and launches it under the watchdog — then reports the one approval step you do in the dashboard. The new crew waits patiently for that approval (`wait_for_approval_seconds`) and comes online by itself.

It's driven by slash commands. Send them as a **task** (messages need the inbox fix in the note above):

| Command | Does |
|---|---|
| `/build <description>` | design, register, and launch a new agent |
| `/build-json <description>` | build it as a validated JSON crew definition (no generated code). Built node-backed, the agent publishes its own definition to the node on first start, and the owner edits it in the Crew tab from then on |
| `/publish <agent> [owner\|public]` | publish a built crew def to the AIMEAT registry |
| `/install <agent> [gaii]` | install a crew def from the registry (register + launch) |
| `/restart <agent>` | bring a stopped crew back online |
| `/reauth <agent>` | re-run authorization so you can approve it again |
| `/list` (or `/status`) | show your crews and which are running |
| `/startall` | launch any stopped crews; skip the running ones (also after a reboot) |
| `/help` | list the commands |

Plain text with no leading `/` is treated as a `/build`. Bring crew-forge online like any crew:

```bash
npx aimeat@latest connect --url https://aimeat.io --owner <you> --agent crew-forge
# approve it in the dashboard, then:
uv run python crews/crew_forge_crew.py        # or: ./scripts/watchdog.ps1 crews/crew_forge_crew.py
```

Set `AIMEAT_OWNER=<you>` in `.env` so crew-forge can register the agents it builds under your account.

### Surviving a reboot (the fleet supervisor)

> This is the **legacy per-process topology**. With `start_fleet` (host + spawner), crew-forge runs as an ordinary agent and its reconciliation is a no-op; see [Running the fleet](#running-the-fleet-scripts).

crew-forge doubles as a fleet supervisor. On startup it **reconciles the fleet**: it scans the live processes and launches any crew in `crews/` that is registered, approved, and *not* already running — skipping the ones that are. This is idempotent (it never double-launches) and reboot-safe (liveness is a live process scan, not stored PIDs). You can also trigger it any time with `/startall`.

That handles everything except the first link: after a reboot, something has to start crew-forge itself. Register it to start at logon:

```powershell
./scripts/install-autostart.ps1     # one-time; creates a Scheduled Task "crewaimeat-forge"
```

Then on every boot, crew-forge starts under the watchdog and brings the rest of the fleet back up on its own. (Remove with `Unregister-ScheduledTask -TaskName crewaimeat-forge -Confirm:$false`.) To start crew-forge **manually** instead (or right now), use `./scripts/start_fleet.ps1` — see below.

## Keeping the fleet honest: doctor, retire, costs

A fleet drifts. Not dramatically — an agent gets added and is missing from a registry, a crew's code is
deleted but its registration is not, a routing decision is never written down. Each one is invisible on
its own, and together they are how an agent with no code became the node's largest traffic source.

Three commands answer the three questions that drift produces.

### `crewaimeat doctor` — is everything still in agreement?

```bash
uv run crewaimeat doctor              # offline, ~1s
uv run crewaimeat doctor --live       # also ask the node (needs the fleet running)
uv run crewaimeat doctor --strict     # warnings fail too — what CI runs
```

Three lenses, deliberately different in kind, because the failures are different in kind:

| Lens | Question | Catches |
|---|---|---|
| **registries** | Do the crew files, `serve.json` and the node agree about which agents exist? | an agent registered with no code · a live crew registered nowhere · a crew that declares no identity, offer or model |
| **conformance** | Does the code reach the outside world by the sanctioned route? | a node call that skips the shared dispatcher (and so its auth + retries) · a crew building its own `LLM` and escaping routing · a swallowed failure on the deliverable path · a connector version stated in a second place |
| **liveness** (`--live`) | What does the NODE believe? | agents it has never seen · schedules firing at agents that no longer exist · agents with no tags, invisible to discovery |

The conformance lens is a **call-graph check, not a linter**. A linter reads one file and asks whether
a statement is well formed; these are statements about an *edge* — "does this function reach the node
without passing through `_aimeat_call`" — which no per-file rule can express.

The liveness lens never reports "fine" when it could not look. Connector tools answer *empty, not
error*, off-fleet, which is exactly the shape of a false green, so an unreachable node is a finding.

**`doctor-baseline.json` is a ratchet.** Existing findings are recorded and stop failing the build; a
*new* violation of the same rule still fails, and a recorded finding that stops firing is reported as
`baseline.stale` so the file only ever shrinks. That is what makes a strict check adoptable on a live
codebase instead of a two-week freeze. It runs in a pre-commit hook, in CI, and at fleet start (where
it warns and never blocks — a drifted registry is a reason to look, not a reason to be offline).

### `crewaimeat retire <agent>` — the opposite of forging one

```bash
uv run crewaimeat retire old-agent            # show the plan, change nothing
uv run crewaimeat retire old-agent --apply    # do it
```

`crew-forge` can create an agent with one command. Until there was a one-command removal, every
experiment became permanent by default — which is how 12 registered agents ended up with no code at
all. Retiring parks the crew file, drops the registration (with a dated backup, because `serve.json`
holds every agent's token), stashes the token, and cleans the routing entry. It is deliberately
conservative: it never deletes the crew file and **never touches memory** — a retired agent's
deliverables are still the owner's data.

### `crewaimeat costs` — who spends, and does anything come out?

```bash
uv run crewaimeat costs               # last 30 days
uv run crewaimeat costs --days 7 --all
```

Every model call is metered to the node's ledger with per-agent attribution. This reads it back and
crosses it with what the repo knows, to answer one question: *which agents cost money without
producing anything anyone reads?*

```
  agent                             cost    calls   verdict
  ai-news-archivist              $  0.47       35   <-- NO CODE — spends, but no crew file exists here
  ...
  $1.51 (15%) went to 8 agent(s) that produce nothing here
```

An agent that spends and delivers is fine at any price. An agent that spends and delivers nothing is a
bug with a monthly invoice.

It also asks OpenRouter what the routing costs **today**, because that is the one kind of drift no
test can catch — the code is correct and the comment is honest, and the world moved underneath both:

```bash
uv run crewaimeat costs --prices      # just this check; needs no node token
```

On 2026-08-12 the newspaper was pinned to `deepseek-v4-pro-0813` for a good, measured reason: same
Finnish prose as the unpinned base, faster, and **2.7x cheaper**. Eleven days later OpenRouter had
repriced it and the pin was **4.2x dearer than the model it beat** — $10.10 of an $11.39 month, 89% of
everything, for prose the cheaper sibling writes just as well. Nothing failed and nothing logged; the
profile's own note still argued for the pin using prices that no longer existed.

So the check asks two questions on every run, and stays quiet otherwise:

- does any chain reach an expensive **pinned snapshot** before a materially cheaper equivalent?
  (A costly model sitting *behind* a cheaper one is a fallback someone chose — never flagged.)
- is every model the routing names **still on offer**? Three `:free` ids had been retired; every call
  to them fell through to the next model silently, so the fallback chain was shorter than it looked.

It never reports a price it could not read: an unreachable catalogue prints `NOT CHECKED — <reason>`,
never a clean bill. A `warn` sets a non-zero exit, so it can run unattended and be noticed when it
stops being quiet.

## Running the fleet (scripts)

Run one crew at a time, or manage the whole fleet with the scripts in `scripts/`. Each has a `.ps1` (Windows) and a `.sh` (macOS/Linux).

| I want to… | Use | What it does |
|---|---|---|
| Run / develop a single crew | `uv run python crews/<x>_crew.py` | Runs one crew in the foreground (Ctrl+C stops it). |
| Keep one crew alive (auto-restart) | `./scripts/watchdog.ps1 crews/<x>_crew.py` | Re-launches that crew if it ever exits. The building block the others use. |
| Start the **whole fleet** now | `./scripts/start_fleet.ps1` | Syncs dependencies and starts the shared serve daemon, its supervisor and the spawner (under `spawner_watchdog.ps1`). The host runs resident agents; the spawner handles agents whose node `run_mode` is `spawn`. With an all-spawn roster the host exits at once while the detached services keep running, and the window then follows the spawner log (Ctrl+C stops watching, not the fleet). |
| The same on macOS/Linux | `./scripts/start_fleet.sh` | The same sequence: serve daemon + supervisor, the spawner under `spawner_watchdog.sh`, the host, then the spawner log. |
| Run a **subset** in the host (or preview) | `./scripts/start_host.ps1 -Agents a,b` | The same host, but lets you pick a subset (`-Agents`) or preview (`-List`). |
| Start the fleet **per-process** (legacy) | `./scripts/watchdog.ps1 crews/crew_forge_crew.py` | The old model: crew-forge reconciles and launches one watchdog+daemon per crew. Heavier; use only if you need per-crew process isolation. |
| Start the legacy fleet at **logon** | `./scripts/install-autostart.ps1` | Registers crew-forge under its watchdog. This is the legacy per-process topology, not an autostart wrapper for `start_fleet.ps1`. |
| See **what's running** | `uv run crewaimeat-tui` | The fleet TUI (one-time `uv sync --extra tui`): every agent's state — running, parked under the spawner, host thread, down — plus logs, test runs and the model picker. Same on Windows, macOS and Linux. |
| **Stop everything** | `./scripts/terminate_fleet.ps1` / `.sh` | Stops the spawner first (it would revive workers), then the watchdogs, host, crew daemons and this home's serve daemon. Only this checkout's processes; another checkout's or the desktop app's fleet is left alone. `-DryRun` / `--dry-run` lists first. |
| Re-reconcile while crew-forge is up | crew-forge `/startall` (send as a task) | Brings stopped crews back without restarting crew-forge. |

For day-to-day development, run one crew with `uv run python crews/<x>_crew.py`. Use `start_fleet`
for the host and spawner topology, `crewaimeat-tui` to inspect it, and `terminate_fleet` to stop it.
`install-autostart` applies only to the legacy crew-forge topology.

The host and spawner own their respective rosters and use per-agent locks. Legacy crew-forge uses
idempotent reconciliation to launch missing per-process crews. These are separate launch paths.

### Spawn mode (idle costs nothing)

The fleet host holds every resident agent as a thread forever; measured with 49 agents, that was
about 3 GB committed and 12.6% of a core burned while nothing happened. A **spawn** agent is data on
the node while idle. The spawner (`uv run crewaimeat spawner`, about 30 MB idle) waits for a tunnel
wake and starts **one worker process** (`crewaimeat run-once <agent>`, i.e. `run_crew(one_shot=True)`),
which runs one PROPOSE → EXECUTE → messages → records → DMs cycle and exits, handing its memory back.

- **The node's roster decides.** The spawner reads `GET /v1/agents?run_mode=spawn` every 30 seconds
  and checks every row, because an older node ignores the filter and would return everything. The
  host reads the same source and skips those agents, so one runtime owns each agent. An agent that
  asks for spawn (`RUN_MODE = "spawn"`) but is not spawn on the node stays a thread in the host.
  When the node cannot be asked, every crew runs in the host. A failed roster read in a running
  spawner keeps the agents it already serves; it never retires the fleet.
- **One worker per agent, always.** The node offers no task lease, so the OS lock on
  `logs/.locks/<agent>.lock` is the only duplicate guard. A wake that arrives mid-run sets a dirty
  flag and the worker runs again afterwards; nothing is dropped, because the work lives on the node.
  Parallelism belongs inside a worker (`max_concurrent_tasks`).
- **Agents with no crew file here** are served from their node definition (`crews.registry.<agent>`),
  so an agent created from the node's basic-agents button joins a running spawner without a restart.
- `crewaimeat spawner --list` shows the roster; `--agents a b` serves a subset.

### Fleet host (one process, memory-light)

The host shares one CrewAI import across resident agent threads, reducing the repeated memory cost
of the legacy per-process fleet. `start_host.ps1` (or `uv run python -m crewaimeat.fleet_host`) runs
just that host, with subset and preview options. It does not launch spawn-mode workers; use
`start_fleet.ps1` for those too. Memory use depends on the active agents and their workloads.

```powershell
./scripts/start_host.ps1                       # every approved crew, one process
./scripts/start_host.ps1 -Agents joker,image-maker   # just these
./scripts/start_host.ps1 -List                 # show what would run, then exit
```

`start_fleet` starts the host alongside the spawner. The node's `run_mode=spawn` agents are excluded
from the host so one runtime owns each agent. Resident `crew-forge` runs in the host too; its fleet
reconciliation becomes a no-op there to prevent duplicate processes. Ctrl+C stops resident host
threads, while detached services and spawn workers can remain. Use `terminate_fleet.ps1` to stop
the whole checkout's fleet. Avoid combining the legacy per-process launcher with this topology.
The TUI identifies host threads with `host` in the wd/dae column.

**Two fleets at once (e.g. dev + prod).** Run a second checkout (a `git clone`) against a different node: each clone has its own `AIMEAT_HOME`, serve daemon, logs and locks, so process detection (reconcile, the TUI, `terminate_fleet`) is scoped per-checkout and the two never collide. Mass-register the second node's agents with `uv run python scripts/register_fleet.py --owner <owner> --url http://localhost:40050`, then `start_host` there.

## Fleet TUI (crewaimeat-tui)

A lazydocker-style terminal UI to watch and drive the whole fleet from one screen — the one status view for Windows, macOS and Linux (it replaced the read-only `view_fleet` scripts, which predated the fleet host and the spawner). It runs as a full-screen app, so it works the same in PowerShell and bash.

```powershell
uv sync --extra tui      # one-time: installs textual
uv run crewaimeat-tui
```

What you see:
- **Status bar** — the serve daemon (pid:port), watchdog/lock counts, running vs stale, any DUPLICATE/zombie warnings, and — when the fleet runs via the [host](#fleet-host-one-process-memory-light) — `host pid N (K threaded)`. Plus a **versions line**: the installed `aimeat-crewai` (PyPI) and `aimeat` CLI (npm) versions, flagged when a newer one is available.
- **Agent table** — every crew with a color-coded status: `running` · `down` · `orphan` (no watchdog) · `DUPLICATE` · `zombie` (running, no crew file) · **`stale-heartbeat`** (locally up but the node hasn't heard from it — the "connector up, daemon not polling" case) · **`parked`** (the spawner holds it and starts a worker on the next wake: the normal resting state of a spawn-mode agent) · `running N` (N spawn workers) · `attached (no runtime)` (on the tunnel, but nothing on this machine would pick up its work) · `down (stale lock)`. A host-threaded agent reads `running` with `host` in the wd/dae column.
- **Detail tabs** for the selected agent — **Overview** (status + the crew's README), **Test** (fire a real task at the running agent and watch its deliverable), **Config** (LLM profile + provider→model chain + any pinned override + offers, contract schemas, capabilities, and the workflows the agent is a step in), **Logs** (watchdog log tail). Switch with `o` / `t` / `c` / `l`.

Refresh is two-tier and off the UI thread: local state (~2 s, no network) and a cached node poll (~13 s, one read-only `agents_list`) — never a tight-loop AIMEAT call. `g` forces a node refresh.

Actions (each behind a y/n confirm, run off the UI thread):

| Key | Action |
|---|---|
| `s` / `x` / `r` | Start / stop / restart the **selected crew** |
| `a` | Re-auth the selected crew |
| `m` | Pick a model for the selected crew (from `llm_providers.json`) and restart it |
| `S` / `X` / `R` | Start / stop / restart the **whole fleet** |
| `d` | Reap stray serve daemons (enforce exactly one) |
| `j` / `k`, ↑/↓ | Navigate · `q` quit |

Actions are safety-routed: stop kills the watchdog first (so it can't respawn) then the daemon, matched by crew filename only — the serve daemon is never touched; fleet stop uses `terminate_fleet.ps1`; reap uses the single-instance `ensure_single_serve`.

## aimeat-agency: the desktop app

[`aimeat-agency/`](aimeat-agency/) is a downloadable desktop appliance for **non-developers** to run an agency of agents on AIMEAT: install → connect your account → pick a brain → run → watch it work. It is a thin Tauri shell over the local Python cockpit (`crewaimeat.agency.cockpit`, FastAPI) — all product logic lives in the cockpit, which reuses this repo's read models (brains, fleet, memory, offerings). A guided wizard walks through account → AI brain (local Ollama by default, OpenRouter as the advanced path) → first agent → device-auth approval → start.

Developers can skip the shell and run the cockpit directly:

```powershell
uv run --extra agency python -m crewaimeat.agency.cockpit    # then open the printed http://127.0.0.1:<port>/
```

See [aimeat-agency/README.md](aimeat-agency/README.md) for the shell, first-run provisioning, and build instructions.

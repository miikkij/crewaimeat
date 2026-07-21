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
- [Writing build_domain](#writing-build_domain)
- [Requirements](#requirements)
- [Docs](#docs)
- [CrewSpec options](#crewspec-options)
- [The agent's README, commands, and services](#the-agents-readme-commands-and-services)
- [crew-forge: an agent that makes agents](#crew-forge-an-agent-that-makes-agents)
- [Running the fleet (scripts)](#running-the-fleet-scripts)
- [Fleet TUI (crewaimeat-tui)](#fleet-tui-crewaimeat-tui)
- [aimeat-agency: the desktop app](#aimeat-agency-the-desktop-app)

## Overview

[AIMEAT](https://aimeat.io) is a network where AI agents live under an owner account. Each agent has an identity, a task queue, and shared memory, and agents can send each other tasks and messages.

The **CrewAI liaison** is a single agent you add to your crew. Its tools are the AIMEAT MCP surface, and it handles the AIMEAT side for everyone else: it opens the MCP connection, completes the Hello Integration onboarding handshake, reports the agent's capabilities, publishes results to memory, and runs the task lifecycle (pick up a task, do it, complete it). Your other agents, the domain crew, just do their jobs and never deal with AIMEAT directly.

crewaimeat ships a tested scaffold and a template. You write only your crew's own agents and tasks; the scaffold runs the liaison, the task daemon, and a live progress feed. The result is an agent on AIMEAT that other agents can queue work to, and that a person can watch and control from the dashboard.

## How it works

- **Liaison.** One in-crew agent owns all AIMEAT coordination, so the domain agents stay focused on the work.
- **Daemon.** `run_crew_daemon` watches the AIMEAT task queue. For each task it builds a crew of the liaison plus your agents, runs it, and the liaison publishes the result and marks the task done.
- **Live progress (no LLM).** A small bridge streams status to AIMEAT: milestones to the task timeline, and a status line every 5 seconds to the memory key `agents.<agent>.tasks.<id>.live`. This is the part that gives people visibility: you can follow what a crew is doing and read its output as it happens.

You write only `build_domain(ctx)`. The scaffold (`crewaimeat/aimeat_crew.py`) handles the rest. `SCAFFOLD_CANON.md` explains each piece and the reason it is there.

## Repository structure

```
src/crewaimeat/     the locked scaffold + shared machinery (the installable package):
                    aimeat_crew.py (run_crew/CrewSpec), llm routing, fleet host, forge,
                    contracts, deterministic pipelines, the TUI (tui/), the agency cockpit (agency/)
crews/              one file per agent: <name>_crew.py with build_domain (a leading _ = parked)
crew_defs/          declarative JSON crew definitions (interpreted by crew_def.py / forge_json.py)
skills/             SKILL.md expertise packs crews can load (see skills/README.md)
scripts/            fleet entrypoints: start_fleet, start_host, watchdog, view/terminate_fleet,
                    register_fleet.py, check_models.py (.ps1 = Windows, .sh = macOS/Linux)
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
npx aimeat@latest connect add --agent research-crew --mode task-runner --url https://aimeat.io --owner <your-aimeat-account>

# 3. Create .env from the template and add your keys
#    OPENROUTER_API_KEY=...                  (https://openrouter.ai/keys)
#    OPENROUTER_MODEL=openrouter/owl-alpha   free, good for testing
#    TAVILY_API_KEY=...                      optional, adds web search

# 4. Run the reference crew (it onboards once, then waits for tasks)
uv run python -m crewaimeat.research_crew
```

Then queue a task for `research-crew` from the AIMEAT dashboard (its Tasks tab, "+ New Task") and watch it run.

### Common uv commands

| Goal | Command |
|---|---|
| Install or update everything | `uv sync` |
| Run the reference crew | `uv run python -m crewaimeat.research_crew` |
| Scaffold a new crew | `uv run crewaimeat new-crew <name>` |
| Run an example crew | `uv run python -m crewaimeat.examples.marketing_crew` |
| Run the test floor | `uv run pytest` |
| Add or remove a dependency | `uv add <pkg>` / `uv remove <pkg>` |

### Picking a model

`openrouter/owl-alpha` is free and fine for testing; the scaffold already copes with its occasional empty responses. Another free option is **NVIDIA NIM** (https://build.nvidia.com, OpenAI-compatible, frontier-class models like `z-ai/glm-5.2` at ~40 req/min) — set `NVIDIA_KEY` in `.env` and use provider type `nvidia` in `llm_providers.json` below. For production, add credit on OpenRouter and switch to a stronger paid model, which is faster and more likely to get the task right on the first try. The single-model default is set in `.env` via `OPENROUTER_MODEL`.

### Providers and model fallback (`llm_providers.json`)

For resilience and local-first setups, drop an `llm_providers.json` in the repo root (copy [`llm_providers.example.json`](llm_providers.example.json)). It lists **providers in priority order**, each with **models in priority order**; `get_llm` tries them top-to-bottom, falling through on any error **across providers** — so a local **Ollama** model can back up OpenRouter (or you can run local-first and never touch a paid model unless you list it). Each model carries its **context window**, and the chain sizes prompts to the *smallest* one, so a 32k local model is never over-filled behind a 128k one. Types: `openrouter`, `ollama` (keyless), `xai`, `openai`, `nvidia` (NVIDIA NIM), `generic`; a provider whose key is missing is skipped, not fatal. The file is gitignored; delete it to fall back to the `.env` `OPENROUTER_MODEL` path.

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

Two ready-made negotiation crews trade autonomously on the **AIMEAT EXCHANGE** (the two-sided data
marketplace on aimeat.io). They run entirely fleet-side on the agent's own token — the accepted contract
authorises every metered call, so there are no API keys. The node stays thin (metering/budget/rake only);
all matching + negotiation is private to the fleet (that's the moat). Materialized crew-defs live in
`crew_defs/` (`crews/exchange_*_crew.py` loaders); the tools are `crewaimeat.exchange_tools`
(forge_catalog capability `exchange`).

| Agent | Role |
|---|---|
| `exchange-buyer` | Consumer/negotiator: browse → machine-match a need's I/O schema to each offering's output → filter by the owner's **autonomy band** (price cap + provider whitelist) → accept the cheapest fit → run it → auto-accept/decline incoming renegotiation proposals by the same band. |
| `exchange-composer` | Composite provider: assembles a refined capability from several upstream contracts it holds, delivers the aggregate, and keeps the margin (`aggregate*(1-rake) - sub-costs`). |

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

## Requirements

- Python 3.10 to 3.13 (`requires-python = ">=3.10,<3.14"`).
- uv for installs and runs (the project `.venv` has no pip). [Install uv](https://docs.astral.sh/uv/getting-started/installation/).
- `crewai[tools]`, `aimeat-crewai` (the AIMEAT connector — the liaison, serve daemon, and Hello Integration driver; source in [aimeat-protocol](https://github.com/miikkij/aimeat-protocol)), plus web/search/extraction tools — all installed by `uv sync`.
- Node.js, for the `npx aimeat` CLI (agent registration + the local serve daemon).
- At least one model key: OpenRouter, NVIDIA NIM (free), xAI — or a local Ollama (keyless). Optional Tavily key for web search.

## Docs

- [ARCHITECTURE.md](ARCHITECTURE.md): the map of the codebase — techstack, component layout (scaffold / crews / contracts / pipelines / TUI), the scaffold's lifecycle, fleet topology, and where to add things.
- [SCAFFOLD_CANON.md](SCAFFOLD_CANON.md): how to build crews on the scaffold, and the reason each piece is there.
- [CREW_AUTHORING_PROMPT.md](CREW_AUTHORING_PROMPT.md): the prompt that has an assistant build a crew with you.
- [CHANGELOG.md](CHANGELOG.md): notable changes.
- [tests/README.md](tests/README.md): the deterministic test floor (`uv run pytest`).
- [skills/README.md](skills/README.md): SKILL.md expertise packs for crews.
- [aimeat-agency/README.md](aimeat-agency/README.md): the desktop appliance (Tauri shell + cockpit).
- [docs/aimeat-app-authoring-guide.md](docs/aimeat-app-authoring-guide.md): how the build crews author AIMEAT apps (cortex + app, direct install).
- AIMEAT integration reference (framework-agnostic, in the node repo): [aimeat-protocol/docs/integrations/crewai.md](https://github.com/miikkij/aimeat-protocol/blob/main/docs/integrations/crewai.md)
- The AIMEAT protocol spec (v4.0, two-layer): [Core](https://github.com/miikkij/aimeat-protocol/blob/main/docs/AIMEAT-RFC-v4.0-Core-full.md) + [Platform](https://github.com/miikkij/aimeat-protocol/blob/main/docs/AIMEAT-RFC-v4.0-Platform-full.md)

## CrewSpec options

`run_crew(CrewSpec(...))` accepts these fields. Only `agent_name` and `build_domain` are required; the rest have sensible defaults.

| Field | Default | Purpose |
|---|---|---|
| `agent_name` | _(required)_ | The AIMEAT identity, matching `connect add --agent`. |
| `build_domain` | _(required)_ | `build_domain(ctx) -> (agents, tasks)`; the **last task's output** is published. |
| `process` | `Process.sequential` | Sequential is the validated path; `hierarchical` is advanced (needs `manager_agent`). |
| `poll_seconds` | `30` | How often the daemon polls the AIMEAT queue. |
| `memory_key_prefix` | `crews.<agent_name>` | Prefix for the published-deliverable memory key. |
| `owner` | `None` | Set only if the same agent name exists under multiple owners on this machine. |
| `manager_agent` | `None` | Only for `Process.hierarchical`. |
| `listen_for` | `("tasks",)` | Add `"messages"` to also act on inbox messages (see note). |
| `wait_for_approval_seconds` | `900` | If launched before the owner approves the agent, wait this long for the token to be accepted, then exit for re-auth (`None` = wait forever). The crew comes online by itself once approved — no console needed. |
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

`crews/crew_forge_crew.py` is a crew whose job is to **build other crews**. Queue it a description and it designs the new crew, writes and validates its `build_domain` on this scaffold, registers the agent (`npx aimeat@latest connect add`), and launches it under the watchdog — then reports the one approval step you do in the dashboard. The new crew waits patiently for that approval (`wait_for_approval_seconds`) and comes online by itself.

It's driven by slash commands. Send them as a **task** (messages need the inbox fix in the note above):

| Command | Does |
|---|---|
| `/build <description>` | design, register, and launch a new agent |
| `/restart <agent>` | bring a stopped crew back online |
| `/reauth <agent>` | re-run authorization so you can approve it again |
| `/list` (or `/status`) | show your crews and which are running |
| `/startall` | launch any stopped crews; skip the running ones (also after a reboot) |
| `/help` | list the commands |

Plain text with no leading `/` is treated as a `/build`. Bring crew-forge online like any crew:

```bash
npx aimeat@latest connect add --agent crew-forge --mode task-runner --url https://aimeat.io --owner <you>
# approve it in the dashboard, then:
uv run python crews/crew_forge_crew.py        # or: ./scripts/watchdog.ps1 crews/crew_forge_crew.py
```

Set `AIMEAT_OWNER=<you>` in `.env` so crew-forge can register the agents it builds under your account.

### Surviving a reboot (the fleet supervisor)

crew-forge doubles as a fleet supervisor. On startup it **reconciles the fleet**: it scans the live processes and launches any crew in `crews/` that is registered, approved, and *not* already running — skipping the ones that are. This is idempotent (it never double-launches) and reboot-safe (liveness is a live process scan, not stored PIDs). You can also trigger it any time with `/startall`.

That handles everything except the first link: after a reboot, something has to start crew-forge itself. Register it to start at logon:

```powershell
./scripts/install-autostart.ps1     # one-time; creates a Scheduled Task "crewaimeat-forge"
```

Then on every boot, crew-forge starts under the watchdog and brings the rest of the fleet back up on its own. (Remove with `Unregister-ScheduledTask -TaskName crewaimeat-forge -Confirm:$false`.) To start crew-forge **manually** instead (or right now), use `./scripts/start_fleet.ps1` — see below.

## Running the fleet (scripts)

Run one crew at a time, or manage the whole fleet with the scripts in `scripts/`. Each has a `.ps1` (Windows) and a `.sh` (macOS/Linux).

| I want to… | Use | What it does |
|---|---|---|
| Run / develop a single crew | `uv run python crews/<x>_crew.py` | Runs one crew in the foreground (Ctrl+C stops it). |
| Keep one crew alive (auto-restart) | `./scripts/watchdog.ps1 crews/<x>_crew.py` | Re-launches that crew if it ever exits. The building block the others use. |
| Start the **whole fleet** now | `./scripts/start_fleet.ps1` | `uv sync`, ensures the serve daemon + supervisor, then runs the **fleet host** — every approved agent as a thread in ONE process (memory-light, ~20× less RAM; default since 0.5.0). Stays in that terminal; Ctrl+C stops the whole fleet. See [Fleet host](#fleet-host-one-process-memory-light). |
| Run a **subset** in the host (or preview) | `./scripts/start_host.ps1 -Agents a,b` | The same host, but lets you pick a subset (`-Agents`) or preview (`-List`). |
| Start the fleet **per-process** (legacy) | `./scripts/watchdog.ps1 crews/crew_forge_crew.py` | The old model: crew-forge reconciles and launches one watchdog+daemon per crew. Heavier; use only if you need per-crew process isolation. |
| Start the fleet on **every boot** | `./scripts/install-autostart.ps1` | One-time: registers crew-forge to start at logon, so the fleet returns by itself after a reboot. |
| See **what's running** | `./scripts/view_fleet.ps1` | Read-only: each crew's state (running / down) and the live-daemon count. Kills nothing. |
| **Stop everything** | `./scripts/terminate_fleet.ps1` | Kills all watchdogs, crew daemons, and connectors (in that order). `-DryRun` lists first. |
| Re-reconcile while crew-forge is up | crew-forge `/startall` (send as a task) | Brings stopped crews back without restarting crew-forge. |

**Which and when, in short:** for day-to-day dev, run one crew with `uv run python crews/<x>_crew.py`. To bring everything up in one go (or after `terminate_fleet`), use `start_fleet`. To have the fleet survive reboots unattended, run `install-autostart` once. Use `view_fleet` to check state, `terminate_fleet` to stop, and crew-forge's `/startall` to re-reconcile while it's running.

**Why there's no "launch every crew" loop:** starting the fleet is crew-forge's *idempotent reconcile* (in code) — it skips crews already running and never double-launches. `start_fleet` and `install-autostart` only bootstrap crew-forge; it brings up the rest (see [Surviving a reboot](#surviving-a-reboot-the-fleet-supervisor)). `terminate_fleet` is the blunt inverse (kill all).

### Fleet host (one process, memory-light)

The **legacy** model runs **one OS process per crew**. Each imports `crewai` + `litellm` independently (~150–250 MB resident), so a large fleet costs several GB of pure import bloat — wasteful for I/O-bound work (poll, shuffle text, call an LLM API). Since 0.5.0 **`start_fleet` runs the host by default**; **`./scripts/start_host.ps1`** (or `uv run python -m crewaimeat.fleet_host`) is the same thing with `-Agents`/`-List`. It runs every agent as a **thread in ONE process**: `crewai` is imported once, and because the work is network-bound the GIL is released on every poll/LLM call so the agents run concurrently. Measured: **~800 MB for ~38 agents** (≈20× less RAM), and two full fleets (prod + a dev clone) fit in ~2 GB together.

```powershell
./scripts/start_host.ps1                       # every approved crew, one process
./scripts/start_host.ps1 -Agents joker,image-maker   # just these
./scripts/start_host.ps1 -List                 # show what would run, then exit
```

The host is the **default** (`start_fleet` runs it since 0.5.0); the per-process model remains available for per-crew process isolation. Pick **one** model per checkout (host *or* per-process): the per-agent single-instance lock makes whichever starts second exit. A crashed agent is restarted (bounded) without touching the others; `crew-forge` is excluded (its job is launching the per-process fleet, redundant here). Ctrl+C stops the whole host. The TUI shows host-threaded agents as `running` with `host` in the wd/dae column.

**Two fleets at once (e.g. dev + prod).** Run a second checkout (a `git clone`) against a different node: each clone has its own `AIMEAT_HOME`, serve daemon, logs and locks, so process detection (reconcile, the TUI, `terminate_fleet`) is scoped per-checkout and the two never collide. Mass-register the second node's agents with `uv run python scripts/register_fleet.py --owner <owner> --url http://localhost:40050`, then `start_host` there.

## Fleet TUI (crewaimeat-tui)

A lazydocker-style terminal UI to watch and drive the whole fleet from one screen — the cross-platform (Windows/Linux), interactive successor to `view_fleet.ps1`. It runs as a full-screen app, so it works the same in PowerShell and bash.

```powershell
uv sync --extra tui      # one-time: installs textual
uv run crewaimeat-tui
```

What you see:
- **Status bar** — the serve daemon (pid:port), watchdog/lock counts, running vs stale, any DUPLICATE/zombie warnings, and — when the fleet runs via the [host](#fleet-host-one-process-memory-light) — `host pid N (K threaded)`. Plus a **versions line**: the installed `aimeat-crewai` (PyPI) and `aimeat` CLI (npm) versions, flagged when a newer one is available.
- **Agent table** — every crew with a color-coded status: `running` · `down` · `orphan` (no watchdog) · `DUPLICATE` · `zombie` (running, no crew file) · **`stale-heartbeat`** (locally up but the node hasn't heard from it — the "connector up, daemon not polling" case). A host-threaded agent reads `running` with `host` in the wd/dae column.
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

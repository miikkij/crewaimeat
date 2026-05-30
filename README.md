# crewaimeat: CrewAI crews on AIMEAT

The idea behind crewaimeat is to make building a CrewAI crew fast (an AI assistant does the wiring for you), connect it to the AIMEAT platform so it works alongside other agents there that come from other platforms, and keep people able to see and steer what the agents produce.

## Contents

- [Overview](#overview)
- [How it works](#how-it-works)
- [Quickstart](#quickstart)
- [Scaffold a new crew](#scaffold-a-new-crew)
- [Example crews](#example-crews)
- [Writing build_domain](#writing-build_domain)
- [Requirements](#requirements)
- [Docs](#docs)
- [CrewSpec options](#crewspec-options)
- [The agent's README, commands, and services](#the-agents-readme-commands-and-services)
- [crew-forge: an agent that makes agents](#crew-forge-an-agent-that-makes-agents)

## Overview

[AIMEAT](https://aimeat.io) is a network where AI agents live under an owner account. Each agent has an identity, a task queue, and shared memory, and agents can send each other tasks and messages.

The **CrewAI liaison** is a single agent you add to your crew. Its tools are the AIMEAT MCP surface, and it handles the AIMEAT side for everyone else: it opens the MCP connection, completes the Hello Integration onboarding handshake, reports the agent's capabilities, publishes results to memory, and runs the task lifecycle (pick up a task, do it, complete it). Your other agents, the domain crew, just do their jobs and never deal with AIMEAT directly.

crewaimeat ships a tested scaffold and a template. You write only your crew's own agents and tasks; the scaffold runs the liaison, the task daemon, and a live progress feed. The result is an agent on AIMEAT that other agents can queue work to, and that a person can watch and control from the dashboard.

## How it works

- **Liaison.** One in-crew agent owns all AIMEAT coordination, so the domain agents stay focused on the work.
- **Daemon.** `run_crew_daemon` watches the AIMEAT task queue. For each task it builds a crew of the liaison plus your agents, runs it, and the liaison publishes the result and marks the task done.
- **Live progress (no LLM).** A small bridge streams status to AIMEAT: milestones to the task timeline, and a status line every 5 seconds to the memory key `agents.<agent>.tasks.<id>.live`. This is the part that gives people visibility: you can follow what a crew is doing and read its output as it happens.

You write only `build_domain(ctx)`. The scaffold (`crewaimeat/aimeat_crew.py`) handles the rest. `SCAFFOLD_CANON.md` explains each piece and the reason it is there.

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
| Add or remove a dependency | `uv add <pkg>` / `uv remove <pkg>` |

### Picking a model

`openrouter/owl-alpha` is free and fine for testing; the scaffold already copes with its occasional empty responses. For production, add credit on OpenRouter and switch to a stronger paid model, which is faster and more likely to get the task right on the first try. The model is set in `.env` via `OPENROUTER_MODEL`.

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
- `crewai[tools]`, `aimeat-crewai`, `tavily-python`, `python-dotenv`, `tzdata` (installed by `uv sync`).
- An OpenRouter API key (or xAI via `USE_XAI=1`); an optional Tavily key for web search.

## Docs

- `SCAFFOLD_CANON.md`: how to build crews on the scaffold, and the reason each piece is there.
- `CREW_AUTHORING_PROMPT.md`: the prompt that has an assistant build a crew with you.
- AIMEAT integration reference: https://aimeat.io/docs/integrations/crewai

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

> **Messages note:** `listen_for=("tasks","messages")` makes the daemon also pick up inbox messages (each message body becomes `ctx.prompt`). This needs a build of `aimeat-crewai` whose inbox polling matches your node; if messages don't dispatch, drive the crew with **tasks** (the Tasks tab), which always works.

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

Then on every boot, crew-forge starts under the watchdog and brings the rest of the fleet back up on its own. (Remove with `Unregister-ScheduledTask -TaskName crewaimeat-forge -Confirm:$false`.)

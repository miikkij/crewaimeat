# crewfive: CrewAI crews on AIMEAT

The idea behind crewfive is to make building a CrewAI crew fast (an AI assistant does the wiring for you), connect it to the AIMEAT platform so it works alongside other agents there that come from other platforms, and keep people able to see and steer what the agents produce.

## Overview

[AIMEAT](https://aimeat.io) is a network where AI agents live under an owner account. Each agent has an identity, a task queue, and shared memory, and agents can send each other tasks and messages.

The **CrewAI liaison** is a single agent you add to your crew. Its tools are the AIMEAT MCP surface, and it handles the AIMEAT side for everyone else: it opens the MCP connection, completes the Hello Integration onboarding handshake, reports the agent's capabilities, publishes results to memory, and runs the task lifecycle (pick up a task, do it, complete it). Your other agents, the domain crew, just do their jobs and never deal with AIMEAT directly.

crewfive ships a tested scaffold and a template. You write only your crew's own agents and tasks; the scaffold runs the liaison, the task daemon, and a live progress feed. The result is an agent on AIMEAT that other agents can queue work to, and that a person can watch and control from the dashboard.

## How it works

- **Liaison.** One in-crew agent owns all AIMEAT coordination, so the domain agents stay focused on the work.
- **Daemon.** `run_crew_daemon` watches the AIMEAT task queue. For each task it builds a crew of the liaison plus your agents, runs it, and the liaison publishes the result and marks the task done.
- **Live progress (no LLM).** A small bridge streams status to AIMEAT: milestones to the task timeline, and a status line every 5 seconds to the memory key `agents.<agent>.tasks.<id>.live`. This is the part that gives people visibility: you can follow what a crew is doing and read its output as it happens.

You write only `build_domain(ctx)`. The scaffold (`crewfive/aimeat_crew.py`) handles the rest. `SCAFFOLD_CANON.md` explains each piece and the reason it is there.

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
uv run python -m crewfive.research_crew
```

Then queue a task for `research-crew` from the AIMEAT dashboard (its Tasks tab, "+ New Task") and watch it run.

### Common uv commands

| Goal | Command |
|---|---|
| Install or update everything | `uv sync` |
| Run the reference crew | `uv run python -m crewfive.research_crew` |
| Scaffold a new crew | `uv run crewfive new-crew <name>` |
| Run an example crew | `uv run python -m crewfive.examples.marketing_crew` |
| Add or remove a dependency | `uv add <pkg>` / `uv remove <pkg>` |

### Picking a model

`openrouter/owl-alpha` is free and fine for testing; the scaffold already copes with its occasional empty responses. For production, add credit on OpenRouter and switch to a stronger paid model, which is faster and more likely to get the task right on the first try. The model is set in `.env` via `OPENROUTER_MODEL`.

## Scaffold a new crew

```bash
uv run crewfive new-crew support-bot     # or `crewfive new-crew ...` in an activated venv
```

This writes `crews/support_bot_crew.py` from the template, sets the agent name, and prints the next steps (register on AIMEAT, set up `.env`, fill in `build_domain`, run). You edit only `build_domain`.

To have an assistant do it, paste `CREW_AUTHORING_PROMPT.md` into Claude Code or Copilot. It interviews you about the crew's purpose and generates the file from the template.

## Example crews

Each lives in `crewfive/examples/` as a thin `build_domain` on the scaffold. Run one with `uv run python -m crewfive.examples.<name>` after registering that agent name on AIMEAT.

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
from crewfive.aimeat_crew import BuildContext, CrewSpec, run_crew

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

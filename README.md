# crewfive — AIMEAT × CrewAI integration

Connect a **CrewAI** crew to the **[AIMEAT.io](https://aimeat.io)** agent network with a
small, **validated scaffold** that handles the parts that are hard to get right —
the onboarding handshake, the task daemon, the AIMEAT liaison, live progress, and
current-date injection — so you only write your crew's own agents and tasks.

> Built and verified end-to-end against aimeat-crewai 0.3.4 / aimeat CLI 1.14.3 /
> crewai 1.14.6. The scaffold turns a CrewAI crew into a **reachable target** on
> AIMEAT: other agents queue tasks to it; your crew picks them up, does the work,
> publishes the result to AIMEAT memory, and completes the task — autonomously.

## How it works (the pattern)

- **Liaison** — one in-crew agent whose tools are the AIMEAT MCP surface. It handles
  *all* AIMEAT coordination. Your **domain agents** stay focused on the work; the
  liaison handles every AIMEAT touchpoint.
- **Daemon** — `run_crew_daemon` polls the AIMEAT queue; for each task it builds a
  crew of `[liaison, *your agents]` with tasks `[*your tasks, finalize]`, runs it,
  and the liaison publishes + completes.
- **Live progress** — a deterministic (no-LLM) bridge streams status to AIMEAT:
  milestones to the task timeline, plus a 5-second live status to the memory key
  `agents.<agent>.tasks.<id>.live`.

You write **only** `build_domain(ctx)`. Everything above lives in the locked
scaffold `crewfive/aimeat_crew.py`. See **[SCAFFOLD_CANON.md](SCAFFOLD_CANON.md)** for
why each piece is locked (each was a real bug we fixed).

## Quickstart

> **Requires Python 3.10–3.13** and **[uv](https://docs.astral.sh/uv/)**. This is a uv
> project — its `.venv` has no `pip`, so use `uv` (plain `pip` would fall back to a system
> Python and fail). No uv? [Install it](https://docs.astral.sh/uv/getting-started/installation/)
> or use a venv that has pip ≥ 21.3.

```bash
# 1. Install deps + the package into the project venv
uv sync                     # equivalently: uv pip install -e .

# 2. Register your crew's identity on AIMEAT, then approve it in the dashboard
npx aimeat@latest connect add --agent research-crew --mode task-runner --url https://aimeat.io --owner <YOUR_OWNER>

# 3. Configure .env  (copy .env.example)
#   OPENROUTER_API_KEY=...                  # https://openrouter.ai/keys
#   OPENROUTER_MODEL=openrouter/owl-alpha   # free, for testing; paid model for speed/reliability
#   TAVILY_API_KEY=...                      # optional — enables web search

# 4. Run the reference crew (onboards once, then waits for tasks)
uv run python -m crewfive.research_crew
```

Then queue a task for `research-crew` from the AIMEAT dashboard (its **Tasks** tab →
**+ New Task**) and watch it run.

### Common uv commands

| Do | Command |
|---|---|
| Install / update everything | `uv sync` |
| Run the reference crew | `uv run python -m crewfive.research_crew` |
| Scaffold a new crew | `uv run crewfive new-crew <name>` |
| Run an example crew | `uv run python -m crewfive.examples.marketing_crew` |
| Add / remove a dependency | `uv add <pkg>` / `uv remove <pkg>` |

### Picking a model

- **`openrouter/owl-alpha`** — free; great for testing. The scaffold already tolerates
  its occasional hiccups (e.g. transient empty responses).
- **A paid model** (a strong Opus/Sonnet tier on OpenRouter) — faster and more likely
  to nail the task on the first try. Switch to this for quality/production.

## Scaffold a new crew

```bash
uv run crewfive new-crew support-bot     # or just `crewfive new-crew ...` in an activated venv
```

Creates `./support_bot_crew.py` from the template, sets the agent name, and prints
the exact next steps (register on AIMEAT, set up `.env`, edit `build_domain`, run).
You only edit `build_domain`.

Prefer an assistant to do it? Paste **[CREW_AUTHORING_PROMPT.md](CREW_AUTHORING_PROMPT.md)**
into Claude Code / Copilot — it interviews you about the crew's purpose and generates
it from the template, reusing the AIMEAT wiring for you.

## What's in the box

| Path | What |
|---|---|
| `crewfive/aimeat_crew.py` | **The scaffold** — `run_crew(CrewSpec(...))`, `BuildContext`. Reuse as-is. |
| `crewfive/research_crew.py` | **Canonical example** — Researcher → Analyst → Writer. The reference to copy. |
| `crewfive/templates/example_crew.py` | Blank template with `CUSTOMIZE` markers. |
| `crewfive/examples/` | Ready-made example crews (see below). |
| `crewfive/progress.py` | The deterministic live-progress bridge. |
| `crewfive/llm.py` | LLM factory (OpenRouter default; `USE_XAI=1` for xAI). |
| `crewfive/scaffold.py` | `crewfive new-crew <name>`. |
| `CREW_AUTHORING_PROMPT.md` | Paste-into-assistant prompt (interview → generate). |
| `SCAFFOLD_CANON.md` | Why the scaffold is built this way; the pitfalls it covers. |

### Example crews (`crewfive/examples/`)

Run any with `uv run python -m crewfive.examples.<name>` (after `aimeat connect add` for
that agent name):

- **marketing_crew** — market research → strategy → a KPI-driven marketing plan.
- **support_crew** — triage → resolution → an empathetic customer reply + internal note.
- **content_crew** — research → outline → a polished blog/article/social draft.
- **competitive_intel_crew** — web research → analysis → opportunities → a CI brief.
- **data_insights_crew** — analyze provided data → conclusions → an insights summary.

Each is a thin `build_domain` on the scaffold — copy one as a starting point.

## Writing `build_domain`

```python
from crewai import Agent, Task
from crewfive.aimeat_crew import BuildContext, CrewSpec, run_crew

AGENT_NAME = "my-crew"

def build_domain(ctx: BuildContext) -> tuple[list[Agent], list[Task]]:
    # ctx.llm -> pass to every Agent;  ctx.prompt -> the user's request
    # ctx.today -> current-time string; prepend to time-sensitive tasks
    worker = Agent(role="Worker", goal="...", backstory="...", llm=ctx.llm)
    task = Task(description=f"{ctx.today}\n\n{ctx.prompt}", agent=worker,
                expected_output="The deliverable.")
    return [worker], [task]   # last task's output is published to AIMEAT

def run():
    run_crew(CrewSpec(agent_name=AGENT_NAME, build_domain=build_domain))
```

The output language is the agent's choice unless the task asks for a specific one.

## Also in this repo

`crewfive/crew.py` + `config/*.yaml` is a separate **hierarchical C-suite** CrewAI
example (CEO delegates to CTO/CMO/CFO/COO), runnable standalone via `crew "<directive>"`
or as an AIMEAT task-runner via `crewfive.runner`. It's a CrewAI-process demo, not part
of the AIMEAT scaffold.

## Requirements

- **Python 3.10–3.13** (`requires-python = ">=3.10,<3.14"`)
- **uv** for installs/runs (the project `.venv` has no pip). [Install uv](https://docs.astral.sh/uv/getting-started/installation/).
- `crewai[tools]`, `aimeat-crewai`, `tavily-python`, `python-dotenv`, `tzdata` (installed by `uv sync`)
- An OpenRouter API key (or xAI via `USE_XAI=1`); optional Tavily key for web search

## Docs

- **[SCAFFOLD_CANON.md](SCAFFOLD_CANON.md)** — the canon: how to build crews right, and why.
- **[CREW_AUTHORING_PROMPT.md](CREW_AUTHORING_PROMPT.md)** — the assistant prompt.
- Full AIMEAT × CrewAI integration docs: https://aimeat.io/docs/integrations/crewai

# crewaimeat ↔ AIMEAT integration

How to connect crewaimeat crews to an **AIMEAT** node. Two ways; the **Liaison Agent** is recommended.

> Deeper theory and a framework-agnostic mapping: AIMEAT's own
> [`docs/integrations/crewai.md`](https://github.com/miikkij/aimeat-protocol/blob/main/docs/integrations/crewai.md).

## crewaimeat structure

| Crew | Entrypoint | Process | Agents |
|------|-----------|---------|--------|
| **Company** | `crewaimeat.runner` | hierarchical | CEO (manager) + CTO, CMO, CFO, COO |
| **Light** | `crewaimeat.demo` | sequential | researcher → analyst → writer |

Roles: [src/crewaimeat/config/agents.yaml](../src/crewaimeat/config/agents.yaml). LLM:
OpenRouter/xAI ([src/crewaimeat/llm.py](../src/crewaimeat/llm.py)). Web search: Tavily.

## Option A — AIMEAT Liaison Agent (recommended)

The `aimeat-crewai` package provides a **liaison agent** that you add to a crew. It handles
ALL AIMEAT communication (Hello Integration, capabilities, task lifecycle, memory, telemetry)
through the MCP surface — the other agents only do their domain work.

```python
from crewai import Crew, Task
from aimeat_crewai import create_liaison_agent, stdio_params
from crewaimeat.llm import get_llm

params = stdio_params(agent_name="company-crew")   # spawns `aimeat connect serve`
with create_liaison_agent(mcp_server_params=params, agent_name="company-crew",
                          llm=get_llm(), verbose=True) as liaison:
    crew = Crew(agents=[liaison, ...], tasks=[...])
    crew.kickoff()
```

Working example: [try_liaison.py](../try_liaison.py).

### Running from a clean install

```powershell
# 1) crewaimeat dependencies + liaison package
python -m uv sync
python -m uv pip install aimeat-crewai

# 2) AIMEAT connector (global)
npm install -g aimeat@latest

# 3) Register the crew as an AIMEAT agent in task-runner mode
aimeat connect add --agent company-crew --mode task-runner --url https://aimeat.io --owner <owner>
#   -> approve in the browser: <node>/v1/agents/verify  (Profile -> Agents)

# 4) Fill in the keys
copy .env.example .env   # OPENROUTER_API_KEY, TAVILY_API_KEY

# 5) Run the liaison demo
uv run python try_liaison.py
```

Verify from the server: `aimeat connect call aimeat_onboarding_status --agent company-crew --json '{}'`
→ `status: completed`, 7 steps passed.

### What the liaison writes to AIMEAT

| Key / target | Content |
|---------------|---------|
| `agents.config.<agent>.runtime` | `{ runtime: "crewai", version: <crewai-version> }` (publish_config step) |
| Onboarding test task | marked `done` (accept + complete) |
| (capabilities, telemetry) | reported during onboarding |

> `crewaimeat.runner` (task-runner subprocess, see Option B) additionally writes
> best-effort notes to the keys `crews/company/tasks/<task_id>/started`
> and `.../result` ([src/crewaimeat/aimeat.py](../src/crewaimeat/aimeat.py)).

### Recommendations (production)
- **`tool_filter`**: by default the liaison gets ~95 MCP tools (incl. wallet/admin/consent).
  Narrow it to what you need: `create_liaison_agent(..., tool_filter=[...])`.
- Mark one agent `primary: true` (`~/.aimeat/agents/<agent>/config.yaml`) to remove the
  server's "no primary" warning.

## Option B — task-runner subprocess

`aimeat connect serve` launches the crewaimeat crew as a subprocess when a task arrives;
the crew reads env variables, runs, and prints a JSON deliverable to stdout. Per-agent
config + env contract: see [examples/aimeat/](../examples/aimeat/). This suits simple
fire-and-forget cases; for LLM-based crews the Liaison (Option A) is better.

## CrewAI ↔ AIMEAT concept mapping (concise)

| CrewAI | AIMEAT |
|--------|--------|
| Tools | action MCP tools (task_*, message_*, board_*, capabilities_invoke, action_execute …) |
| Knowledge (RAG) | memory_read/search, knowledge_get/list, storage_download, handbook_get (custom `BaseKnowledgeSource`) |
| Memory | `memory_*` (persistent shared memory) |
| Skills (SKILL.md) | skill bundle `~/.aimeat/<agent>/SKILL.md` + handbook (see status note below) |
| Crew / Flow | task and work lifecycle (`task_*`, `work_*`) |
| Delegation | `capabilities_invoke`, `organism_*`, `catalogue_*` |

## Status
- ✅ The Liaison pattern works end to end (AIMEAT 1.13.2+, aimeat-crewai 0.1.2+).
- 🚧 Native CrewAI **Skills** support (`Agent(skills=[SKILL.md])`) awaits compatibility with
  AIMEAT's skill-bundle frontmatter and directory structure (in progress, aimeat-crewai 0.2.0).

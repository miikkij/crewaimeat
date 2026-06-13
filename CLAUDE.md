# CLAUDE.md — crewaimeat

This repo is **crewaimeat**: a toolkit + patterns for running CrewAI agents on the AIMEAT substrate
(aimeat.io). Crews live in `crews/`; the locked scaffold in `src/crewaimeat/`.

---

## ⭐ The shared source of truth lives in an AIMEAT organism — use it and keep it current

This project is coordinated through an **AIMEAT organism workspace**, not only this repo. It is the
shared, versioned memory: goals, plans, roadmap, tasks, research, learnings, decisions, feedback.
**Read it when you start a work session, and update it as you work.** Treat it as mandatory: the
workspace is the shared record — log your work there and the next agent (or human) picks it up.

- **Organism:** `crewaimeat` — id `b784641b-a4dd-4d69-adb6-9954dc813e1e`
- **Workspaces:**
  - **Open Source** — `ws-mq5vuq0hicp` (adoption, examples, public-facing)
  - **Internal** — `ws-mq5vvdgsjwp` (commercial + experimental; not public)
- **Tools:** the **AIMEAT APPDEV MCP** — `aimeat_workspace_read` / `_list` / `_write_draft` /
  `_publish` / `_add_document`, and `aimeat_organism_get`.

### On starting work
1. `aimeat_workspace_read` the relevant workspace.
2. Read these first — they tell you what's done, where we aim, and the pitfalls to steer around:
   `00-start-here` (wiki) · `goals-and-plan` (plan) · `state-of-play` (wiki) · `pitfalls` (Internal wiki).
3. Read open `feedback` and `task` records — see what's requested / in-progress / free.

### While working — keep it up to date
- **Tasks:** take a `task`, update its `status` as you progress (todo → in-progress → in-review → done).
  Discover new work → **create a new `task` record** so it's captured for the next agent.
- **If you do something in this repo that isn't reflected in the workspace, log it there too:**
  research → a `research` doc, a learning/how-to → a `wiki` doc, a roadmap shift → a `roadmap-item`.
- **Decisions** (architecture/strategy) → a `decision` record (gated — the owner approves publishing).
- **Feedback** you give or receive → `feedback` records.
- Write a **draft → publish**. Re-read `.latest` before overwriting so you build on the newest version.
  Pass `value` as a **JSON object**.

### If you don't have access yet
You need an AIMEAT account + membership to read/write the workspace. To join the guided development:
1. Create an account at **https://aimeat.io**.
2. Find the **`crewaimeat`** organism (it is *listed*).
3. **Request access** (join → the owner approves). Once a member you can read the workspace, pick up
   tasks, and propose work via Claude Code.

---

## Conventions (full details live in the workspace docs)
- Package management: use **uv** (`uv run`, `uv sync`).
- One crew = `crews/<name>_crew.py`; `build_domain(ctx) -> ([agents], [tasks])`; `AGENT_NAME` matches
  the name used in `aimeat connect add --agent`.
- LLM routing (`llm_providers.json`): route content crews → grok; route code/app crews →
  owl-alpha → gpt-oss-120b → minimax (grok is for content only — strong at prose, weak at code).
- **Fail loud** — surface the real cause: reject at the boundary, or raise from one shared dispatcher.
- Full how-to + architecture: read the **Open Source** workspace (`how-to-use`, `architecture`,
  `agent-workflows`, `fleet-reliability`). Hard-won gotchas: read the **Internal** `pitfalls` +
  `pitfalls-workflows-fleet` docs before touching a related system.

---
name: aimeat-sync
description: >
  The mandatory crewaimeat organism-workspace ritual. Use at the START of a work session to
  read the shared source of truth, and whenever asked to "log to the workspace" / record
  research, a task update, a learning, a decision, or feedback. The AIMEAT organism workspace
  — not just this repo — is the versioned shared memory; read it when you start and update it
  as you work, so the next agent or human picks it up.
allowed-tools: mcp__claude_ai_AIMEAT_Appdev__aimeat_workspace_read, mcp__claude_ai_AIMEAT_Appdev__aimeat_workspace_list, mcp__claude_ai_AIMEAT_Appdev__aimeat_workspace_write_draft, mcp__claude_ai_AIMEAT_Appdev__aimeat_workspace_publish, mcp__claude_ai_AIMEAT_Appdev__aimeat_workspace_add_document, mcp__claude_ai_AIMEAT_Appdev__aimeat_organism_get
---

# aimeat-sync — keep the shared source of truth current

crewaimeat is coordinated through an **AIMEAT organism workspace**, not only this repo. Treat it
as mandatory: read it when you start, log your work there as you go.

- **Organism:** `crewaimeat` — id `b784641b-a4dd-4d69-adb6-9954dc813e1e`
- **Workspaces:**
  - **Open Source** — `ws-mq5vuq0hicp` (adoption, examples, public-facing)
  - **Internal** — `ws-mq5vvdgsjwp` (commercial + experimental; not public)
- **Tools:** the AIMEAT APPDEV MCP — `aimeat_workspace_read` / `_list` / `_write_draft` /
  `_publish` / `_add_document`, and `aimeat_organism_get`.

## On starting work
1. `aimeat_workspace_read` the relevant workspace (default to **Open Source** unless the task is
   internal/commercial).
2. Read these first — they say what's done, where we aim, and the pitfalls to steer around:
   `00-start-here` (wiki) · `goals-and-plan` (plan) · `state-of-play` (wiki) ·
   `pitfalls` (Internal wiki).
3. Read open `feedback` and `task` records — see what's requested / in-progress / free.

## While working — keep it current
- **Tasks:** take a `task`, move its `status` (todo → in-progress → in-review → done). Discover
  new work → create a new `task` record so it's captured for the next agent.
- **If you did something in this repo that isn't reflected in the workspace, log it there too:**
  research → a `research` doc · a learning/how-to → a `wiki` doc · a roadmap shift → a
  `roadmap-item`.
- **Decisions** (architecture/strategy) → a `decision` record (gated — the owner approves publishing).
- **Feedback** given or received → `feedback` records.
- Write a **draft → publish**. **Re-read `.latest` before overwriting** so you build on the newest
  version. Pass `value` as a **JSON object**.

## If access is missing
Reading/writing needs an AIMEAT account + membership. If the tools 403 / aren't connected, say so
plainly and stop — do not fabricate workspace contents. The MCP server may be absent in headless /
cron runs; that's expected.

## Related
- Memory: `crewaimeat-dogfood-organism` (the organism + two workspace ids).
- This ritual is also stated in the repo `CLAUDE.md` ⭐ section — keep the two in agreement.

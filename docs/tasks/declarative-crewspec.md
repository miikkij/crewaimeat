# Task handoff — Declarative CrewSpec-as-JSON (Phase 1)

Hand this to a fresh Claude Code session opened in this repo. It stands alone (the new session has the
codebase + CLAUDE.md but none of the originating conversation). Scope is **Phase 1 only**: schema +
interpreter + validator + tests + one converted crew as proof. crew-forge and AIMEAT install are Phase 2/3.

---

Task: Declarative CrewSpec-as-JSON — a data-driven crew definition + interpreter (Phase 1)

You are working in the crewaimeat repo (CrewAI agents on the AIMEAT substrate). Read CLAUDE.md first
for conventions (uv for everything; fail-loud, no silent fallbacks; per-repo AIMEAT_HOME; one crew =
crews/<name>_crew.py with build_domain(ctx) -> ([agents],[tasks]) and an AGENT_NAME).

GOAL
Today every crew is Python: crews/<name>_crew.py defines build_domain(ctx) that constructs CrewAI
Agent/Task objects in code. crew-forge (the agent that builds agents) and the agency generator emit that
Python as a STRING and exec it — fragile and impossible to validate before it runs live.

Build the foundation for defining a crew as DATA instead of code: a JSON crew-definition schema plus a
single generic interpreter build_domain_from_json(doc, ctx) that constructs the same CrewAI agents/tasks
from the doc — with NO exec of generated Python. This is Phase 1 only (schema + interpreter + validator +
tests + convert one real crew as proof). Do NOT touch crew-forge or AIMEAT install yet (those are later
phases).

ORIENT FIRST (read these before designing — verify the real field names, don't trust this brief blindly)
- src/crewaimeat/aimeat_crew.py — CrewSpec, run_crew(spec), and the BuildContext (ctx) passed to
  build_domain. Note exactly what ctx exposes (llm, task, prompt, today, directives, …) and every field
  CrewSpec accepts (agent_name, build_domain, readme_md, temperature, listen_for, record_spaces,
  on_record, idle_hook, idle_hook_seconds, mode, tags, capabilities, chat_commands, discover, …).
- A few real crews to see the shape build_domain produces: crews/joker_crew.py (simple),
  crews/web_researcher_crew.py (records-mode + signals), crews/workflow_manager_crew.py (tools + delegation).
- src/crewaimeat/workflow_spec.py — AGENT_SIGNALS and the Sig.* helpers (count_nonempty / nonempty /
  json_field, NONE). Reuse this exact signal shape (required_to_function / success_signal /
  deliverable_location) in the schema; do not invent a new one.
- Tool factories the schema's "tools" must be able to name: src/crewaimeat/memory_tools.py
  (make_memory_tools), plus make_dm_tools, make_workflow_tools, make_schedule_tools (grep for make_*_tools).
- src/crewaimeat/fleet_identity.py (tags/capabilities registry) and src/crewaimeat/offers.py — the schema
  should carry tags/capabilities/offers so a crew-def is self-describing for discovery.
- Memory context: memory/ MEMORY.md notably [[aimeat-direct-build-pattern]] (author manifest → install via
  REST, no generator — this task is the crew analog) and [[crew-forge-agent-that-makes-agents]].

DELIVERABLE (Phase 1)
1. A JSON schema (proposed starting shape — refine after reading the code) for a self-contained crew def:
   {
     "agent_name": "...", "readme_md": "...", "temperature": 0.3,
     "llm_profile": "content|coding|content-free",         # maps to get_llm(agent_name=...) routing
     "tags": [...], "capabilities": {...}, "offers": [...], # for discovery/federation
     "agents": [ {"role","goal","backstory","tools":["memory","dm",...],"skills":[...],"allow_delegation"} ],
     "tasks":  [ {"description","expected_output","agent","context":[taskRefs],"async"} ],
     "process": "sequential|hierarchical",
     "signals": {"required_to_function":{...}, "success_signal":{...}, "deliverable_location":{...}}
   }
   Support {{ctx.prompt}} / {{ctx.today}} style injection into task descriptions (CLAUDE.md note:
   build_domain MUST inject ctx.prompt or the agent drifts — see [[crew-builddomain-must-inject-ctx-prompt]]).
2. build_domain_from_json(doc, ctx) -> ([Agent...],[Task...]) — a pure interpreter. Resolve "tools" names
   to the real make_*_tools factories, "llm_profile" to get_llm, task "context" refs to earlier Task
   objects. Fail LOUD on an unknown tool/agent/profile — never silently drop.
3. A validator validate_crew_doc(doc) -> [errors] that catches problems BEFORE construction (unknown tool,
   missing agent for a task, bad signal shape, non-DAG task context) so a bad def is rejected at the
   boundary, not at runtime.
4. Prove it: pick ONE existing simple crew (e.g. joker), express it as a JSON doc, and show
   build_domain_from_json(doc, ctx) produces an equivalent crew. Keep the Python build_domain as the
   escape hatch for exotic crews — this is additive, not a rewrite.
5. Tests under tests/ for the interpreter + validator (uv run pytest). NOTE: there are ~40 PRE-EXISTING
   test_build_domain failures from disabled _aimeat_* crews — ignore those, don't try to fix them; only
   your new tests must pass.

CONSTRAINTS
- No exec/eval of generated code — the whole point is data, not code.
- Additive and backward-compatible: existing Python crews keep working untouched.
- Match the surrounding code's style/idioms; reuse existing helpers (signals, tool factories, get_llm).
- Do NOT wire crew-forge to emit JSON yet, and do NOT add AIMEAT install/registry — those are Phase 2/3.

WORKFLOW
Explore the code, then present a short plan (the finalized schema + interpreter approach + which crew you'll
convert + test plan) for the owner to approve BEFORE implementing. Package management is uv. Commit to main
only when the owner asks.

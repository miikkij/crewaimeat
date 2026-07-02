# CLAUDE.md — crewaimeat

This repo is **crewaimeat**: a toolkit + patterns for running CrewAI agents on the AIMEAT substrate
(aimeat.io). Crews live in `crews/`; the locked scaffold in `src/crewaimeat/`.

---

## AIMEAT organism workspace (opt-in, only when explicitly asked)

The project has a coordination workspace on aimeat.io (organism `crewaimeat`,
id `b784641b-a4dd-4d69-adb6-9954dc813e1e`; Open Source `ws-mq5vuq0hicp`, Internal `ws-mq5vvdgsjwp`).
**Do NOT read or sync it at session start** — it is large and reading it burns tokens for little value.
Touch it only when the owner explicitly asks to read or log something there.

---

## Conventions
- Package management: use **uv** (`uv run`, `uv sync`).
- **Connector home is per-repo** (`aimeat-crewai>=0.6.0`): the home holding `serve.json`, tokens, agent
  configs is `AIMEAT_HOME` (env wins) → else `<cwd>/.aimeat`. The fleet **pins `AIMEAT_HOME=<repo>/.aimeat`**
  in every entrypoint (`start_fleet.ps1`/`serve_watchdog.ps1`/`watchdog.ps1` → inherited by crew-forge →
  every detached crew), so all processes share ONE serve.json regardless of cwd — isolated from other
  projects' fleets (no global `~/.aimeat` collision). Resolve it via `crewaimeat._home.aimeat_home()`,
  never re-derive the path. `.aimeat/` is gitignored (it holds tokens).
- One crew = `crews/<name>_crew.py`; `build_domain(ctx) -> ([agents], [tasks])`; `AGENT_NAME` matches
  the name used in `aimeat connect add --agent`.
- **New agent? Give it a real identity** — don't ship the generic Hello-Integration defaults. Add an
  entry to the central registry `src/crewaimeat/fleet_identity.py` (charset-safe `tags` `[a-z0-9._-]`
  + specific `capabilities` {technical, domain, languages}, derived from the agent's purpose); the
  scaffold sets tags (`aimeat_agent_tags_set`) + reports capabilities (`aimeat_agent_capabilities_report`)
  on every start. A crew may instead set `CrewSpec.tags`/`.capabilities` inline (overrides the registry).
  Discovery/matching reads tags + capabilities + README + offers — so ALSO keep the crew's `README`
  constant accurate and add an `offers.py` entry. Versioned ids (`consumes:x@1`) go in `capabilities`
  /`offers`, never tags (tags reject `:`/`@`).
- LLM routing (`llm_providers.json`): route content crews → grok; route code/app crews →
  owl-alpha → gpt-oss-120b → minimax (grok is for content only — strong at prose, weak at code).
- **Two messaging channels — keep them distinct.** (1) dashboard/owner chat (`aimeat_message_*`): the
  agent ↔ its OWN owner, private, NOT federated — the daemon already triggers crews from it. (2) the
  **federated inbox** (`aimeat_dm_*`, AIMEAT "Postilaatikko", v1.30.1+): the agent → ANYONE on the
  federation. Use `src/crewaimeat/dm.py` for sending: `dm_reply` (in-thread / to a requester — consented,
  auto-sends), `dm_initiate` (a NEW contact — **owner-gated**, never cold-DMs), `dm_attach` (presigned file),
  `make_dm_tools` (LLM-crew tools: reply + read only). Inbound (a DM → a crew) is the daemon's `dm.inbound`
  tunnel-push drain (Phase 2, aimeat-crewai). **Scopes:** agents need `messages:send` + `messages:read`
  (both in the `coordinator` profile; grant explicitly for task-runner agents at device-auth).
- **Fail loud** — surface the real cause: reject at the boundary, or raise from one shared dispatcher.

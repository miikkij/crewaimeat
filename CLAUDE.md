# CLAUDE.md — crewaimeat

This repo is **crewaimeat**: a toolkit + patterns for running CrewAI agents on the AIMEAT substrate
(aimeat.io). Crews live in `crews/`; the locked scaffold in `src/crewaimeat/`.

---

## AIMEAT organism workspace — our coordination surface (dogfooding)

The project has a coordination workspace on aimeat.io (organism `crewaimeat`,
id `b784641b-a4dd-4d69-adb6-9954dc813e1e`; Open Source `ws-mq5vuq0hicp`, Internal `ws-mq5vvdgsjwp`).
**Do NOT read or sync the WHOLE workspace at session start** — it is large and burns tokens. But it IS
where we now coordinate multi-step / cross-repo work: **we dogfood the substrate for our own development.**

**Process for work that spans crewaimeat + the AIMEAT platform (aimeat-protocol repo):** the living spec
AND the handoff prompts live in the **Internal workspace** (`ws-mq5vvdgsjwp`, `plan` space) — NOT as repo
files. One doc is the source of truth (feature spec + shared contract + phases + status + open questions); a
per-repo handoff doc points at it. Both repo sessions **read AND update** the spec as they learn (flip
status cells, append decisions), so the two sides stay synced without drifting prompts.
- **Write:** `aimeat_workspace_write(space="plan", value={title, markdown})` → then
  `aimeat_workspace_publish(namespace="docs.plans", id=…)` (a draft is NOT live until published). Workspace
  tools are on the **appdev** MCP surface; access as owner `happydude500001`.
- **Reference in a handoff prompt** by organism_id + ws + doc id; a fresh session opens its handoff doc,
  reads the spec, does the work, updates the spec. Node feature requests for aimeat-protocol also live here.
- Entry point today: the **Skills** feature — spec `doc-sdie0se` (+ handoffs `doc-4lqxvl3` crewaimeat,
  `doc-hvgkpju` aimeat-protocol). Keep touches **targeted** — read/write the feature doc at hand, never a
  full sync.

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
- **Skills** = portable SKILL.md expertise packs in `skills/<name>/` (see `skills/README.md`; contract
  shared with the AIMEAT registry, spec doc-sdie0se). `CrewSpec.skills=["name"]` loads them FAIL-LOUD at
  daemon start (`crewaimeat.skills.load_skills`); agents take them like ctx.llm: `Agent(skills=ctx.skills)`
  (`ctx.skills` is None when the crew declares none — crewai rejects an empty list). JSON crew-defs: a
  top-level `"skills": [...]` applies to every agent. Live proof: `crews/joker_crew.py`. Crews ALSO
  fetch owner-LINKED skills from the node's skills registry per build (`crewaimeat.skills_registry`;
  union, local wins; opt-out `registry_skills=False`; unreachable registry = loud note + local-only).
  Workspace skills (2c) are OPT-IN: `workspace_skills=True` derives targets from record_spaces (or pass
  explicit `[{"organism_id","ws"}]`); precedence local > linked > workspace-auto. Default OFF — a
  workspace is a shared surface; any member's skill would ride into the crew's prompts.
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

---

## Fleet & daemon — when a crew-agent actually comes ONLINE
- The fleet host **auto-discovers every `crews/*_crew.py`** (that has a `run()`) — no roster to edit.
  A leading underscore parks a crew: `crews/_foo_crew.py` is skipped (`forge._crew_files`, why the
  `_aimeat_*` crews are dormant). So "add a crew file" ≠ "agent is live".
- Two things make it live, not just present: (1) **register once** —
  `npx aimeat@latest connect add --agent <name> --mode task-runner --url https://aimeat.io --owner <owner>`
  — and approve the one-time device flow (its token lands in the shared `serve.json`); (2) **restart the
  fleet** (`scripts/start_fleet.ps1` → `fleet_host`) so it attaches as a THREAD to the ONE shared loopback
  serve daemon (all agents in one process, crewai imported once). Only APPROVED agents come online; an
  unapproved one waits and joins itself once approved.
- **Connector tools (`aimeat_workspace_*`, `aimeat_memory_*`, `memory_read_public`, task poll/push) work
  ONLY while the agent is attached / running in-fleet.** Off-fleet (a bare `uv run … -c` one-liner, a
  background loop) those reads fail quietly — `manifest=null`, empty lists — which is exactly what the
  some-listener / mroom code means by "works once attached". Run cross-organism reads in-fleet.
- **Restart the fleet** after changing an agent's identity (`fleet_identity.py` tags/capabilities), LLM
  routing (`llm_providers.json`), or after adopting a new contract — none of it takes hold until re-attach.

## Cross-organism display — a different org shows another's data
- A different organism (even the SAME owner) reads another's data via a **public memory key** +
  `aimeat_memory_read_public(gaii, key)` — NOT by reaching into the other org's workspace (same-owner
  sub-agents get `manifest=null` there; a known connector gap). Reciprocal move: **EXPOSE** the data as a
  `visibility:"public"` memory key, then the reader uses its existing public-read path (the one M-ROOM
  already uses for its `ext:mroom.*` feeds). Address a public key by the writer's **GAII**
  (`<agent>#<owner>@<node>`), not the owner GHII.
- Live bridge (crewaimeat → M-ROOM): `some.radar.public.latest` (some-listener) +
  `mail.morning.public.latest` (postman). NB: the morning digest's `## Kilpailijakatsaus` is otherwise
  **not persisted** — it lives only inside the sent email until mirrored to that key.

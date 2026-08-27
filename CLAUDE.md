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
- **READ THE DOC IMMEDIATELY BEFORE YOU WRITE IT — `aimeat_workspace_write` on an existing id is a full
  OVERWRITE, not a merge.** A living spec is living because the OTHER side edits it too: on 2026-08-28 I
  sent my own local copy as an update and erased aimeat-dev's answers to two open questions, which they
  then had to merge back by hand. `aimeat_workspace_read(ids=[…])` first, edit THAT text, write, publish —
  and keep the gap between read and write short. When you only have something to add, add a section rather
  than resending the whole document; there is nothing to clobber in a paragraph that did not exist before.
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
  the name used in `aimeat connect --agent`.
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
- **The crew file is the ONE source for what an agent is.** Beside `build_domain`, a crew declares at
  module level: `LLM_PROFILE` (which `llm_providers.json` profile routes it), `TAGS` (charset-safe
  `[a-z0-9._-]`), `CAPABILITIES` ({technical: [{name,type}] OBJECTS, domain: [str], languages: [str]}),
  `OFFERS` (a list), `SKILLS`. A JSON crew states the same keys in its `crew_defs/*.json` doc.
  `crewaimeat.agent_manifest` reads them STATICALLY (ast, never by importing); `fleet_identity`,
  `offers` and the routing map are DERIVED. This replaced three central lists that nothing required
  you to update — which is how 13 crews ended up with no identity, 13 with no offer and 20 with no
  routing decision (audit 2026-08-22). Never re-add an agent to a central list; put it in the crew.
- **New agent? Give it a real identity** — don't ship the generic Hello-Integration defaults. The
  scaffold sets tags (`aimeat_agent_tags_set`) + reports capabilities (`aimeat_agent_capabilities_report`)
  on every start, and `run_crew` REJECTS a malformed capabilities payload at the boundary (a bare string
  in `technical` is accepted by the node and silently makes the agent unmatchable). Discovery reads tags
  + capabilities + README + offers, so keep the crew's `README` constant accurate too. Versioned ids
  (`consumes:x@1`) go in `capabilities`/`offers`, never tags (tags reject `:`/`@`).
- LLM routing: the crew's own `LLM_PROFILE` is the default; `llm_providers.json` → `crews` is a
  per-machine OVERRIDE list (empty by default). Content crews → grok is for PROSE only (strong at
  prose, weak at code and weak in Finnish); Finnish prose → the `news` profile (DeepSeek V4 Pro);
  code/app crews → `coding`. A crew that declares no profile falls to `default` silently; doctor
  reports it and the fleet host names it at start-up.
- **NO OUTPUT LIMITS. Never cap `max_tokens` on a cloud model.** Not "generously", not "just to be
  safe" — a number nobody measured is a guess, and the failure is SILENT: the reply stops mid-JSON,
  or never starts. `max_tokens` is a CEILING, not a spend; a model that writes 800 tokens costs 800
  whatever it says, so a low cap saves nothing and loses runs. Measured on one real prompt
  (2026-08-27): `max_tokens=16384` → `finish='length'`, **content 0 characters**, 16 385 reasoning
  tokens; uncapped → 11 247 characters, `finish='stop'`. On a reasoning model the thinking and the
  answer share the budget, and the thinking wanted 29 k on its own. `crewaimeat.llm` therefore sends
  a ceiling it never expects to reach (the endpoint's declared `context`, else 131072) purely to stop
  a provider imposing its own thrifty default — OpenRouter applies 2048 when sent nothing. The ONE
  exception is a **local** Ollama server, which allocates against the number. If output comes back
  empty or truncated, read `finish_reason` and `completion_tokens_details.reasoning_tokens` before
  blaming the model. The same rule governs every other invented ceiling in this repo — character
  counts, post lengths, retry caps: if a limit is not the vendor's documented one or the owner's
  stated one, it does not get to throw work away.
- **Which model runs is the owner's call.** When a pinned id dies (404, retired, renamed), report it
  and ASK — never substitute the vendor's suggested successor. `stealth/ox-alpha` → `z-ai/glm-5.3-flash`
  was a 1:1 id swap that kept the POSITION and changed the BEHAVIOUR: the replacement was a reasoning
  model, and it broke three separate things across the fleet before anyone connected them to a routing
  change nobody had approved.
- **`crewaimeat doctor` before you claim anything is fine.** Three lenses: registries (do the crew
  files, serve.json and the node agree), conformance (a call-graph route check — node calls go through
  `_aimeat_call`/`_aimeat_rest`, a crew's model comes from routing not a constructor, a failure on the
  deliverable path is visible, the connector version exists in one place), liveness (`--live`, what the
  node believes). Runs in pre-commit + CI; `doctor-baseline.json` only ever shrinks.
  `crewaimeat retire <agent>` is the opposite of forging one; `crewaimeat costs` shows who spends
  without producing.
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

## Working on a LIVE fleet — the night of 2026-08-28, written down so it is not repeated

One 404 on a retired model became: a reasoning model at the head of every profile, truncated JSON,
empty replies, a 35-minute run, and finally every call on every endpoint refused at once with the
fleet crash-looping. Root cause of the whole chain: **an obstacle in front of the task was treated as
part of the task.** Five rules came out of it.

- **When something blocks you and it is not yours to change, REPORT it — do not remove it.** A dead
  model id, a missing permission, a config that belongs to the owner: say what is blocking, what it
  would take, and stop. Removing the blocker to keep going is how a one-agent verification turned
  into a fleet-wide routing change nobody approved. (See the two rules above on `max_tokens` and on
  who chooses the model.)
- **NEVER edit shared infrastructure in increments while a fleet can restart.** The committed code
  was correct both times; the WORKING TREE between edits was not, and a restart loaded exactly that
  half-finished version — `llm.py` sending the whole context window as `max_tokens`, so every
  endpoint 400ed, the chain fell through to a local Ollama that was not running, and news-fetcher
  and space-weather-writer crash-looped. Finish the change, run the tests, and only then let it
  exist on disk in a state something can load. `llm.py`, `aimeat_crew.py`, `llm_providers.json` and
  the workflow definitions are all shared: a mistake there is not one agent's, it is 53.
- **Inference is not measurement — not mine, and not another agent's.** I called an empty reply "the
  same truncation" (it was `finish='length'` with `content=0`, a different fault), and
  `workflow-inspector` filed a confident report blaming `interactive` mode when the node showed
  `task-runner`. Read the actual field — `finish_reason`, `completion_tokens_details`, the agent's
  real `mode` — before forming a theory. **A subagent's report is a claim, not evidence**; verify it
  against the node before acting on it.
- **Fix the cause, not the shape of the symptom.** A truncating response was answered by splitting
  one prompt into three passes. The split was worth keeping (a failed call now costs one clip, not
  the run) but it was chosen for the wrong reason: the cause was a cap, and one measurement would
  have shown that before the restructuring.
- **A retry must never end worse than its best attempt, and WRITTEN beats EMPTY.** Attempt 2 produced
  a complete result one word over a limit; attempt 3 returned prose and overwrote it with `None`,
  failing a run whose work was already in hand. And when ranking attempts, a MISSING field violates
  once exactly like a written one that bends a rule — compare what is filled first, only then the
  violation count, or the loop keeps nothing over something a person could fix in five seconds.

**Every workflow step needs `retry`.** A step without one is a single transient failure away from
being permanently red, and a red step takes everything that waits on it down too: on 2026-08-28
`space-weather` hit one connection error, and `editorial` — which waits on it — was skipped, so the
edition shipped with 21 articles and no front page. `write-a`/`write-b`/`editorial` had retries;
`fetch`/`space-weather`/`features` did not. All six do now, in `workflow_spec.py` AND on the node.

---

## Fleet & daemon — when a crew-agent actually comes ONLINE
- The fleet host **auto-discovers every `crews/*_crew.py`** (that has a `run()`) — no roster to edit.
  A leading underscore parks a crew: `crews/_foo_crew.py` is skipped (`forge._crew_files`, why the
  `_aimeat_*` crews are dormant). So "add a crew file" ≠ "agent is live".
- Two things make it live, not just present: (1) **register once** —
  `npx aimeat@latest connect --url https://aimeat.io --owner <owner> --agent <name>`
  — and approve the one-time device flow (its token lands in the shared `serve.json`); (2) **restart the
  fleet** (`scripts/start_fleet.ps1` → `fleet_host`) so it attaches as a THREAD to the ONE shared loopback
  serve daemon (all agents in one process, crewai imported once). Only APPROVED agents come online; an
  unapproved one waits and joins itself once approved.
- **`task-runner` mode is load-bearing, not boilerplate — and the scaffold sets it, not the CLI.**
  Device auth no longer takes a `--mode` flag (removed in connector v1.33), so `run_crew` sets the mode
  on EVERY start via `aimeat_agent_mode_set`, before onboarding; `CrewSpec.mode=None` derives
  `task-runner` for every crewaimeat crew. A task is auto-activated ONLY when the agent's mode is
  `task-runner` (`autoActivated = queued && mode === 'task-runner'`); every other mode
  (`interactive`/`autonomous`/`coordinator`) follows `queued → (OWNER starts it) → active → done`. There
  is NO `aimeat_task_start` tool, and the REST `/start` is owner-only — so an interactive agent has no
  route out of `queued` and `aimeat_task_complete` answers *"Only active tasks can be completed"*. That
  is a SAFETY BOUNDARY: an interactive agent is an open-ended model in a conversation (talk-into-able,
  prompt-injectable), so nothing it decides reaches the world until a person says yes; a task-runner is
  narrow and largely deterministic (the loop is code, the model writes only prose), which is what the
  owner pre-authorised. Breadth of capability trades against freedom to act. Full explanation for
  agents: `skills/aimeat-agent-modes/`.
- **Connector tools (`aimeat_workspace_*`, `aimeat_memory_*`, `memory_read_public`, task poll/push) work
  ONLY while the agent is attached / running in-fleet.** Off-fleet (a bare `uv run … -c` one-liner, a
  background loop) those reads fail quietly — `manifest=null`, empty lists — which is exactly what the
  some-listener / mroom code means by "works once attached". Run cross-organism reads in-fleet.
- **Restart the fleet** after changing a crew's declaration (`TAGS`/`CAPABILITIES`/`LLM_PROFILE`/
  `OFFERS`), the providers file, or after adopting a new contract — none of it takes hold until
  re-attach. Tags and capabilities are pushed to the node ON START, so an identity edit is invisible
  on the node until the agent re-attaches.

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

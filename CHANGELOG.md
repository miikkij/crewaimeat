# Changelog

Notable changes to crewaimeat. Format loosely follows [Keep a Changelog](https://keepachangelog.com/).
Dates are the working dates; entries are **uncommitted and take effect on the next fleet restart**
(the daemons import the modules at start).

## [Unreleased] — 2026-08-22

### Changed
- **An agent's facts live in ONE place: its own crew file.** Until now the same agent was described in
  four hand-kept lists — `fleet_identity.py` (what it can do), `offers.py` (what it promises),
  `llm_providers.json` (which model it runs on), `tests/crew_fixtures.py` (whether it is tested) — and
  nothing required an entry in any of them. An agent came online missing from all four, which is
  exactly what happened: 13 crews had no identity, 13 no offer, 20 no routing.

  A crew now declares `LLM_PROFILE`, `TAGS`, `CAPABILITIES`, `OFFERS` (and `SKILLS`) at module level;
  a JSON crew declares the same keys in its doc, which it already had. `crewaimeat.agent_manifest`
  reads them STATICALLY — via `ast`, never by importing, so `doctor` can run from a pre-commit hook on
  a broken tree and the routing resolver does not pay for 46 modules of imports to learn six literals.
  - `fleet_identity.py` 490 -> 69 lines; `FLEET_IDENTITY` is empty and stays a library fallback.
  - `offers.py` 1479 -> 658 lines; the 858-line authored dict is derived from the crews, served
    lazily through a module `__getattr__` so `CREW_AGENTS` / `_CREW_OFFERS` still read as attributes.
  - `llm_providers.json`'s `crews` map went 38 entries -> 0. It is now an OVERRIDES list: the crew's
    own `LLM_PROFILE` is the default and an entry there overrides it for one machine.
  - `llm_profile` in a JSON crew was previously DECLARATIVE ONLY — validated, never used to route — so
    it had quietly rotted: all three JSON crews said "content" while routing had run them "content-free"
    for weeks. It is authoritative now, and set to what has actually been live.
  - Proven, not assumed: every one of the 45 agents' tags, capabilities, offers and resolved model
    profile was snapshotted before the move and compared after, THROUGH the real consumers
    (`identity_for`, `crew_offers`, `_select_chain`). Zero differences.
- **crew-forge emits the same declaration language.** A forged crew wrote `_TAGS` / `_CAPABILITIES` /
  `_OFFER` — private by name, passed only through `CrewSpec` — so a forged agent's identity was
  invisible to every tool that does not actually run the crew. It now emits the public constants plus
  `LLM_PROFILE`, so a new agent is complete the moment it is written.
- 12 ghost agents retired (`crewaimeat retire`), serve.json 61 -> 49. `daily-brief` was a forge output
  that was never registered while `daily-briefing-crew` did the same job live; it is parked.
- `image-maker` had no identity and so carried no tags on the node. Its entry is deliberately distinct
  from `image-scout`'s: one MAKES an image, the other FINDS and curates existing ones, and without that
  in the tags the picker hands a generation request to the crew that only searches the web.

### Added
- **`crewaimeat quality` — did the output get better when the model changed?** That question had no
  answer: the paper's prose was routed to DeepSeek V4 Pro on 2026-08-12 because the previous chain's
  Finnish was judged unusable, and the evidence for it working was an impression. This measures the
  properties a bad run actually degrades — grounding (how many sources a piece cites), completeness
  (articles per edition) and length — attributed per ARTICLE to the model that wrote it, from the
  node's provenance record. It does NOT judge prose, and says so: style is what a deterministic check
  cannot see, and an LLM scoring an LLM agrees with itself for reasons nobody can audit.

  It took three tries to stop it inventing findings, which is the point of the entry:
  1. A rate-limited provenance read returned the same shape as a missing one, so **40 well-sourced
     articles were reported as "100% ungrounded"**. Unknown is not zero: the three outcomes (read ok /
     no provenance record / read failed) are kept apart, and reads are paced under the node's limit.
  2. The per-model comparison then said DeepSeek was **five times worse at grounding than the free
     router**. It was an artifact: `koodaus`, `matikka` and `prompt-niksi` are generated feature
     sections with no news sources BY DESIGN, ~3 of every ~23 articles. A category that has never
     carried a source is now classified as generated and excluded — derived from the data, never a
     hardcoded list that would go stale and start hiding regressions.
  3. That classification needs evidence. Below a floor of 3 observations a category is JUDGED, not
     excused: wrongly calling one "generated" hides a failure silently, wrongly judging one produces a
     finding someone can check in a minute.

  With those fixed, the real answer over 20 editions: **every model, every sourced category, 0%
  ungrounded** — the switch neither helped nor hurt grounding. What it did surface is the fallback:
  `openai/gpt-oss-120b` wrote 28 articles at a median **813 characters and 2 sources**, against
  DeepSeek's **3,837 and 6**. When the chain falls through, the paper silently ships stubs.
- **`crewaimeat costs` — model spend per agent, and who spends without producing.** The fleet has
  metered every model call to the node's ledger since aimeat-crewai 0.16.1, with per-agent
  attribution. Nobody read it back, which is why `crypto-weekly-reporter` — an agent whose code had
  been deleted — went on burning calls until it became the node's largest traffic source and had to be
  found by hand. The report is built around one question, not around totals: *which agents cost money
  without producing anything anyone reads?* It crosses the ledger against what the repo knows (is
  there a crew file, is it registered, is it parked) and names the answer. First run on the live
  fleet: **$1.51 of $10.19 over 30 days — 15% — went to 8 agents with no crew file at all.**
  - Reads `GET /v1/ledger/usage?group_by=agent`, NOT the MCP tool: `aimeat_usage_report` is on the MCP
    surface but not in the connector's shell-callable set, so `_aimeat_call` answers "Unknown
    shell-callable tool". The REST route works off-fleet too, so the question is answerable without
    the fleet running.
  - The owner's own GHII appears in the ledger beside the agents; it is a person, not a broken agent,
    and is never accused of having no code.
- **`crewaimeat doctor` — continuous reconciliation of what this repo DECLARES against what it DOES.**
  The August audit took hours and almost every finding was a mechanical disagreement between six
  hand-kept lists. That is a program, not an afternoon. Three lenses, deliberately different in kind:
  - **registries** (set reconciliation): the crew file, `fleet_identity`, `offers`,
    `llm_providers.json`, `serve.json` and the node must agree about which agents exist. It found 12
    agents registered with no crew file, one live crew registered nowhere, 13 with no identity, 13 with
    no offer, and 20 resolving to a routing profile nobody chose.
  - **conformance** (a call-graph route check, not a lint): does a call reach the NODE through the
    shared dispatcher, does a crew get its model from routing rather than a constructor, is a failure
    on the deliverable path visible, does the connector version exist in exactly one place. A linter
    reads one file and asks whether the statement is well formed; these are statements about an EDGE.
    Precision was the whole design problem — the first draft matched any `.get(` and produced 95
    "findings" of which ~30 were real, so HTTP clients are now resolved from the imports.
  - **liveness** (opt-in `--live`): what the node believes — stale agents, orphaned schedules, agents
    the repo has never heard of. It NEVER reports "fine" when it could not look: connector tools answer
    empty (not an error) off-fleet, which is the exact shape of a false green, so an unreachable node
    is a finding and the lens is recorded as SKIPPED.
  Runs from the CLI, from CI (`--strict`), from a pre-commit hook, and at fleet start (warn, never
  block). `doctor-baseline.json` is a RATCHET: today's findings stop failing the build, nothing new may
  be added, and an entry that no longer fires is reported as `baseline.stale` so the file only shrinks.
  Findings are keyed by `rule::file::function`, never by LINE — a line number breaks the moment anyone
  inserts a line above it, so every refactor would churn the baseline and the ratchet would be switched
  off within a month. Rules that came out of using it: `registry.skill.missing` (a declared skill that
  is not on disk — the crew fails at start), `registry.routing.note_contradicts_order` (a profile whose
  `_note` claims a lead its provider array does not have), and `guard.removed_connect_command`.
- **`crewaimeat retire <agent>` — the missing half of an agent's lifecycle.** crew-forge could create an
  agent with one command and nothing could remove one, which is the direct cause of 12 ghost
  registrations, ~20 near-duplicate experiments on the node and 6 schedules nobody dares delete. Parks
  the crew file, drops the registration (with a dated backup — serve.json holds every agent's token),
  stashes the token, cleans the routing entry, and reports what must be removed from source by hand.
  Deliberately conservative: it never deletes the crew file and never touches memory.

### Fixed
- **The crew contract floor covered 17 of 46 crews and had been red for two months.** `CREW_MODULES`
  was hand-kept: it still named four crews that had been PARKED (16 permanently failing tests) while 29
  live crews had no `build_domain` test at all. It is now derived from disk using the same parking rule
  as the fleet host, so a new crew is contract-tested the day it lands. A crew that is not driven by the
  task prompt declares `PROMPT_INDEPENDENT = "<reason>"` — a written reason, not a boolean, so the
  opt-out cannot be used to silence a real regression. **846 tests / 21 red → 993 tests / 0 red.**
- **Malformed capabilities were reported to the node on every start, and accepted.** `datapkg-analyst`
  declared `technical` as bare strings where the contract is `{name, type}` objects; the node takes it
  without complaint and the agent silently stops matching in discovery. Fixed, and `run_crew` now
  REJECTS a malformed payload at the boundary instead of sending it.
- **A failed deliverable publish could report success.** The task callback chain (publish → library →
  verify-score → self-monitor) had each link wrapping the previous in a bare `except: pass`, so with two
  features enabled a failed publish vanished. One shared `_chain()` helper now LOGS every link and
  RE-RAISES the critical ones (the publish itself, and the finalize that closes the task).
- **The connector version existed in four places at once** — pinned `2.0.0` in code, documented as
  `>=2.6.1` in pyproject, `3.3.2` installed, `1.34.0` asserted in a test — and the registration pin sat
  BELOW the documented floor, so every agent crew-forge registered went through a connector that drops
  the provenance block silently. One constant now (`forge.AIMEAT_CONNECTOR`, raised to `3.5.0`), read by
  every docstring, message and test, and enforced by a doctor rule.
- **Two routing profiles described a chain that had not been live for weeks.** `content-free` and
  `coding` both claimed in their `_note` that the free meta-router LEADS while their provider arrays
  already led with the paid `openai/gpt-oss-120b`. The order was left as-is (it is the live quality
  decision); the prose was corrected, and doctor now fails a note that contradicts its own order.
- **20 crews had no routing entry** and resolved to `default` silently — including `sanomat-desk` (the
  paper's editor) and `workflow-manager` (which routes work to other crews). All 20 are now mapped
  explicitly; the fleet host names any unrouted crew at start-up, so the fallback can no longer be
  reached by accident.
- Four tests were pinned to a machine's own `llm_providers.json` / `AIMEAT_HOME` and to behaviour that
  had deliberately changed (task-runner default, the OpenRouter embedder tier, the connector pin). They
  now assert the RULE against a fixture, not one developer's model choices.
- **The editorial voice lived in two places, and only one of them ran.**
  `skills/sanomat-editorial-style/SKILL.md` — a versioned, shareable, registry-publishable pack — was a
  parallel copy of two prompt strings in `editorial_pipeline`, and nothing read it. The prompts now
  load the skill (new `skills.skill_body()`, for a prompt built in CODE rather than on a CrewAI Agent —
  the deterministic pipelines never build one, so `Agent(skills=...)` never reached them). Editing the
  house voice is now editing one file, and it travels through the skills registry. Loaded lazily, so a
  missing skill fails the editorial STEP loudly instead of taking the fleet down at import.
- **Every onboarding path taught a command the connector had removed.** `connect add` and its `--mode`
  flag went away in connector v1.33; 142 places still used them — including the first command in the
  README's Quickstart and the one `startup.prompt.md` hands to an AI assistant, so onboarding's very
  first step could not work. All rewritten to `aimeat connect --url <node> --owner <owner> --agent
  <name>`; the agent's MODE is set by the scaffold at start (`aimeat_agent_mode_set`), which is why the
  flag could be dropped. A doctor rule (`guard.removed_connect_command`) now fails on the old form —
  documentation had no test, which is exactly why this rotted for months.
- **doctor printed "PASS" above a non-zero exit.** A stale baseline entry fails under `--strict`, but
  the verdict line counted only errors and warnings. Found by the pre-commit hook itself, on a real
  event: retiring an agent made its finding stop firing. A gate that says one thing and does another is
  how people learn to ignore the gate.
- `embedder_cascade`'s module docstring contradicted its own function: "privacy" drops the free cloud
  tier but keeps OpenRouter as the universal last-resort fallback, i.e. it means "no FREE cloud", not
  "no cloud". Documented where the distinction is actually made.

## [Unreleased] — 2026-08-09

### Changed
- **Sanomat evening pipeline: 44 memory keys per edition → ~25, plus a shared status record.**
  Measured on aimeat.io 2026-08-09: `news.*` held 2,993 keys over 68 edition-days and grew 44 a run
  with nothing ageing it out. The shipped per-principal ceiling is 1000 (`AIMEAT_MEMORY_MAX_KEYS`);
  aimeat.io runs 100,000, which no other node does — so at 44 keys/run a default-configured node
  exhausted itself in 23 edition-days. One value holds 1024 kB and a day's whole raw measures 457 kB
  median / 515 kB at the worst observed day, so the 21-way split was a habit, not a requirement.
  - **One raw key.** `news.<date>.<edition>.raw.<category>` ×21 → `news.<date>.<edition>.raw` with
    `{fetchedAt, categories:{…}}`. Categories sit under a FIELD so the success signal can point at
    exactly the payload and metadata can live beside it. A single plain write (news-fetcher is the
    only writer of that key), guarded: over 800 kB it says so LOUDLY at 17:00 rather than surprising
    the run with a 413. Measured in UTF-8 bytes — Finnish raw is full of two-byte characters, so a
    character count under-reports the thing being capped.
  - **Retention.** The raw key is written with `ttl_hours = 14 days`, so the node expires it itself.
    Raw is scraped source nobody reads after the edition ships; it only became expirable once it was
    one key a day. **Nothing else expires** — articles, editorial, quiz and front-page index are the
    published newspaper.
  - **A shared status record** (`news.<date>.<edition>.status`, new `crewaimeat.edition_status`).
    Six steps patch ONLY their own field through `PATCH /v1/memory/:key` (RFC 7386 merge patch, the
    node compare-and-swaps and retries a lost swap). `owner_scope` is baked in, not a caller's
    argument: memory is keyed by the WRITER, so six agents patching "one key" without it produce SIX
    records in six namespaces and nothing merges — with every write returning ok. Verified against
    aimeat.io: three agents, distinct fields, version 1→2→3, one record under the owner GHII.
  - **Offer descriptors moved in the same deploy**, because the workflow gates on them: descriptors
    ahead of the fetcher = every run input-red, behind it = output-red. `fetch-edition-raw` and both
    desks' `required_to_function` now use `count_nonempty` with `path: "categories"` (count entries
    INSIDE one record instead of matching keys). Signals that glob `article.*` are untouched — the
    articles are not changing, and each keeps its own `ai_provenance_id` behind the app's per-article
    AI label.
  - Forward-only: the 68 published editions are NOT rewritten. The desks read the new key and fall
    back per category to the old ones, which also covers `lukijoilta` — written by sanomat-desk as
    reader tips arrive by DM, long after the 17:00 fetch, so it keeps its own key.
  - New: `scripts/verify_edition_consolidation.py` (read-only, works off-fleet) checks the key count,
    the raw record, that the status record is ONE owner-held record, and every step's signals.

## [Unreleased] — 2026-07-13

### Added
- **Lukijoilta-deski (`sanomat-desk`) — reader news for (L)AIMEAT Sanomat.** One new DM agent, three
  flows: (1) a daily scheduled **owner interview** ("mitä tänään tapahtui?", photos welcome — same
  schedule→kickoff-marker→DM wiring as social-briefing); (2) **news tips** from any logged-in
  federation user; (3) the **oikaisukanava** (below). Tips append to
  `news.<date>.evening.raw.lukijoilta` in the fetcher's raw shape, so the EXISTING desk-A write loop
  produces the article (new persona **Vilma Vinkki**; tip photos are re-published public and embedded
  as markdown images). Late tips (after 17:30) roll to the next day's edition. New modules:
  `reader_desk.py`; crew `crews/sanomat_desk_crew.py`; chat-command palette + offers entries.
- **Legal screen (`legal_screen.py`) — EXTERNAL material only** (owner's scoping: our own production
  is not screened). One strict-JSON LLM pass against fixed satire-relevant criteria (private persons,
  defamation-as-fact, personal data, image rights, illegal content) at INTAKE — flagged material never
  becomes raw; the sender gets a decline and the owner a dashboard note. An unavailable/unparseable
  screen REJECTS (fail loud, never a silent pass).
- **Oikaisukanava (`corrections.py`) — the formal correction channel.** "OIKAISU: ..." DMs file into
  the PUBLIC index `sanomat.oikaisut.index` (statuses vastaanotettu → aiheeton |
  odottaa-hyvaksyntaa → oikaistu | hylatty). The Lakiosasto arbiter rules with a pompous public
  justification; **aiheeton is final immediately**, oikaistaan is HITL-gated to the owner (public
  content changes only after a human approves) and then published into the next edition's
  `article.oikaisut` (the front-page index auto-includes it). The Sanomat app (v27) renders the
  status list ("Oikaisut ja oikaisupyynnöt"), a "Toimitus palvelee" box, the Lukijoilta/Oikaisut
  category labels + provenance badges, and markdown-image support for tip photos.



### Added
- **Opt-in CrewAI crew memory (`CrewSpec.memory`, OFF by default).** A crew that must REMEMBER across
  runs gets CrewAI's built-in persistent memory: the **embedder cascade** (`embedder_cascade.py`)
  probes **ollama → nvidia-free → qwen** in bias order (the `privacy` default drops the free-but-cloud
  nvidia tier; `EMBEDDER_BIAS`/`CrewSpec.embedder_bias="cost"` promotes it — testers value money over
  privacy), LOGS the tier used, and FAILS LOUD when none is reachable. Storage is scoped
  **owner/agent/principal** under `AIMEAT_HOME/crew_memory/` — a federation DM sender gets a memory of
  their own (`memory_scope="principal"`), never another caller's; `"agent"` = one deliberate shared
  brain; `"session"` = ephemeral. The memory's analysis LLM rides the crew's own `get_llm` chain (never
  the OpenAI default) and is capped at `max_tokens=2048` (an observed gemma4 runaway burned 64k tokens
  ≈ 10 GPU-minutes per encode; now it fails 30× faster into the same use-defaults path). crew-forge is
  memory-aware: the Architect decides `MEMORY: yes/no` per order, `write_and_validate_crew` emits the
  CrewSpec toggle and surfaces the embedder prerequisite (never gates), and the behavioral eval grades it.
- **`pipeline_memory.py` — semantic-memory primitives for the DETERMINISTIC pipelines** (open_store /
  remember / recall / dedup_check / prior_art_block; semantic-only scoring so thresholds hold; loud
  degradation to None — the paper ships even with the embedder host down). Wired across the fleet:
  - **editorial**: recalls its most similar past columns before drafting (continuity — reference by
    date, never rerun an angle) and remembers each published Finnish column;
  - **tidbits** (koodaus/prompt-niksi/matikka): generate → semantic dedup → ONE retry with the
    near-duplicate as a negative example → publish regardless (logged, never a hole in the paper);
  - **news desks**: a resurfacing story gets an "AIEMMIN JULKAISTUA" block and is written as its DELTA;
  - **crew-forge precedent**: every VALID build is remembered as ORDER → DESIGN and similar past builds
    (bar 0.5, live field rating fetched fresh from the reputation keys) are injected into the Architect
    prompt as priors — the forge starts learning from its own field-rated work;
  - **joker v1+v2** ("already told" sets injected + lineups remembered; the A/B stays design-only),
    **social-briefing** (reports deltas vs past digests), **some-listener** (drops CROSS-day resurfaced
    HN stories; same-day rescans stay idempotent).
  - `scripts/backfill_sanomat_memory.py` seeds the stores from the node's published history (read-only
    on the node, idempotent ≥0.97 skip) — 597 historical editorials/sections seeded on the dev box.
- **LOCOMO proof harness (`benchmarks/locomo/`, opt-in, offline-first, $0 on local models).** The
  long-term-conversational-memory benchmark mem0 markets against: mem0-faithful J-score judge
  (categories 1–4), keyword-floor / CrewSpec-memory / mem0 arms on identical models. Sample verdict
  (conv-26, 20 QA, gemma4): **keyword 15% / crewai 40% / mem0 60%** — the embedder buys real recall
  over the floor; mem0 leads via ingest-time fact distillation (n=20, not significant). No further
  runs planned; the harness stays as the reproducible artifact.
- **`local_memory` full-text search (SQLite FTS5).** The durable local tier could only recall by id or
  browse by facets; now an FTS5 index (topic+body+tags, sync triggers, a pre-existing DB migrates
  itself with a one-time rebuild) powers `search()` (BM25 best-first, agent-scoped) and a
  `search_memory` tool every local-memory crew gets. Query syntax is disarmed (an LLM string can't
  break MATCH); a build without FTS5 keeps storage and fails only search, loudly.
- **crew-forge capability catalog + real identities for generated crews** (`forge_catalog.py`): the
  Architect designs against a preflight-checked tool catalog (an unavailable tool is never offered);
  generated crews ship real tags/capabilities/offers/discover instead of Hello-Integration defaults;
  a behavioral eval (`forge_eval.py`) grades orders end-to-end (dry-runs into `.candidates`, never the
  live fleet).
- **agency owns the full ollama lifecycle it participates in.** The setup wizard pulls the **embed
  model** (nomic-embed-text) right after the chat model and `/api/setup/status` reports it — crew
  memory works on a fresh appliance out of the box. Status now distinguishes **installed vs running**:
  not installed → the download step (as before); **installed but not running** (a fresh install's
  first session, or autostart off) → a *Start Ollama* button (`POST /api/ollama/start`) that spawns
  `ollama serve` as an **agency-owned child with a recorded pid**. Appliance **shutdown unloads the
  ollama models** the fleet had loaded (`ollama stop` per model — the 10+ GB of GPU-backed memory
  frees immediately instead of waiting out the keep-alive) and then **stops the ollama server too,
  but ONLY if the agency started it** (the pidfile); a user's own/autostart ollama is never touched.

### Changed
- `CrewSpec.offer` — a crew can pin its offer inline; task-runner registration passes `--mode` so tasks
  auto-activate (no manual "Start this task" in the dashboard).
- The repo `.claude` layer was slimmed on owner order: the mandatory read-the-workspace-on-session-start
  ritual is gone from `CLAUDE.md` (the organism workspace is opt-in, on explicit ask) and the bundled
  agents/skills (convention-reviewer, fleet-doctor, aimeat-sync, release-prep) are removed.

### Fixed
- **Onboarding mode-race safety net**: a task-runner could stick at 4/7 when the daemon read a stale
  `completable=true` mid `mode_set` → `_finish_pending_onboarding` drives the pending api_call steps;
  loopback pool sized for the fleet host.
- `serve_watchdog` must never spawn under pytest (it leaked detached serve daemons onto the machine).
- The LOCOMO mem0 arm neutralizes `OPENROUTER_API_KEY` for local runs — mem0 silently prefers
  OpenRouter over an explicit `openai_base_url` (mem0/llms/openai.py), which sent local model ids to
  OpenRouter and 400'd every add.

## [0.6.0] — 2026-06-22

### Added
- **Event-driven contract agents — workspace-record PUSH instead of idle polling** (requires
  **aimeat-crewai >= 0.7.0**, the platform side of the tunnel-push work). A workspace-contract agent
  now subscribes to its served record spaces and the node PUSHes a `workspace.record` wake over the
  existing per-agent tunnel — so the agent runs its deterministic handler only on a real record (or a
  one-time catch-up scan per space on connect), making **zero periodic node calls** when idle.
  - `CrewSpec.record_spaces` (a list of `{organism_id, ws, space}`, or a 0-arg callable resolved at
    daemon start) + `CrewSpec.on_record(event)` + `listen_for=("records",)`, passed through to
    `run_crew_daemon`. `contract_record_spaces(agent, *contracts)` builds the subscription list from a
    contract's record namespaces × the agent's member workspaces (discovered once).
  - Wired **image-scout**, **image-maker** and **web-researcher** (all three of its contracts —
    research / market-scan / company-research) to records; their idle-poll `idle_hook`s are removed.
    Clock-based hooks (editorial / features / postman-07:00 / workflow-inspector / activity-reporter)
    and the stats-driven feedback-wisdom keep their `idle_hook` — only request-record scanners moved.

### Changed
- **The idle 2-4 Mbit/s tunnel storm is fixed at the root (aimeat-crewai >= 0.7.1).** Prod access logs
  showed ~596 MB over 240 `/v1/connect/tunnel` frames in 5 min — the crew **daemon re-listing tasks
  every poll cycle** (queued/active/stalled, full payloads, ×3), which ride the tunnel as request/response
  frames even when idle. 0.7.1's daemon re-lists **only on a push wake** (+ a rare safety-net) and adds a
  `task.cancelled` push (`/local/cancelled`) so cancellation no longer needs an owner-scope memory scan
  per dispatch. An idle agent on a live tunnel now makes ~zero periodic node calls. (CLI bumped to 1.29.0.)
- On the crew side, `on_record` scopes each scan to the event's **own workspace** (`record_event_targets`
  → `process_*(targets=[(org,ws)])`) instead of re-discovering and re-scanning all member workspaces per
  event; **feedback-wisdom** (whose trigger is a memory key, not a record — so it stays a poll) is now
  conditional (skips the derive/mirror pass when the stats are unchanged), quiet on expected NOT_FOUND/
  ACCESS_DENIED, and polls every 30 min instead of 5; and the reputation rollup's `aimeat_agent_statistics`
  call is quiet on a "no stats yet" NOT_FOUND.
- **Idle traffic trimmed at the source.** The periodic `_auth_alive` probe is **gone** — the 0.7.0
  daemon self-exits on a revoked token (`auth_revoked` push → connector `auth_failed` → exit), and the
  supervisor re-auths on exit (`watchdog.ps1/.sh` treat exit code 2 like 78; the host handles it via
  `SystemExit`). The reputation rollup now reads stats over the **`aimeat_agent_statistics` tunnel tool**
  instead of a direct owner-only GET, and stays conditional (writes only when the score moved). (An
  interim throttle of the probe shipped first, in 0.5.x, then was removed once 0.7.0 landed.)

## [0.5.0] — 2026-06-19 → 2026-06-21

### Added
- **Fleet host — run the WHOLE fleet in ONE Python process (`crewaimeat.fleet_host` / `scripts/start_host.ps1`).**
  One process per crew imports crewai+litellm independently (~150–250 MB each), so a 39-agent fleet costs
  ~8 GB of pure import bloat — absurd for I/O-bound work. The host imports the heavy stack **once** and runs
  each agent as a supervised thread; the work is network-bound, so the GIL is released on every poll/LLM call
  and agents run concurrently. **Measured: ~800 MB for 38 agents (≈20× less RAM); two full fleets — prod +
  a dev clone — fit in ~2 GB together.** Opt-in and additive: the per-process model (`start_fleet`) is
  unchanged and stays the default. A crashed agent is restarted (bounded) without touching the others;
  `--agents a,b,c` runs a subset, `--list` previews. Guards that make CrewAI thread-safe in the host:
  CrewAI's telemetry registers a SIGINT handler (`signal.signal`, main-thread-only) — telemetry is opted out
  and `signal.signal` is a no-op off the main thread; and `reconcile_fleet` no-ops when `AIMEAT_FLEET_HOST`
  is set so crew-forge can't spawn a shadow per-process fleet inside the host.
- **TUI: a live Test tab, an expanded Config tab, and a per-agent model picker.** **Test** (`t`) fires a REAL
  task at the selected running agent and polls its deliverable — exercising the live daemon, its real model
  and the tunnel — and shows the agent's own “How to task me” hint (so a contract agent that wants a request
  record, not a free-text brief, says so). **Config** now also shows the agent's offers, contract input/output
  spaces + schema field names, curated tags/capabilities, the workflows it has a step in, and any pinned model
  override. **Model picker** (`m`) lists every model from `llm_providers.json`, pins one agent to it
  (`<AIMEAT_HOME>/llm_overrides.json`, honored first by `get_llm`) and restarts the agent.
- **TUI host-awareness.** The host heartbeats `logs/.host_status.json`; `fleet_state` reads it and shows
  host-threaded agents as `running` with **`host`** in the wd/dae cell and **`host pid N (K threaded)`** in the
  status bar — so the TUI works whether the fleet runs per-process or in the host.
- **`register_fleet` — one-command mass registration against a SECOND node** (`scripts/register_fleet.py`,
  `forge.register_fleet(owner, url)`). Registers every crew (or `--agents` subset) as a task-runner against a
  node, surfacing each device-approval code — the way to stand the same fleet up on a local dev node from a
  separate clone (isolated `AIMEAT_HOME`/serve/logs/locks).
- **Quality tooling — Ruff, pre-commit, CI, and an architecture map.** `[tool.ruff]` (lint + format, line-length
  120) wired into a `.pre-commit-config.yaml` and a GitHub Actions workflow (`ruff` + `pytest`); **`ARCHITECTURE.md`**
  documents the techstack, component map (scaffold / crews / contracts / pipelines / TUI), the scaffold's
  lifecycle, fleet topology, and where to add things.

### Changed
- **`start_fleet` / `terminate_fleet` default to the fleet host.** `start_fleet.ps1`/`.sh` now bring up
  the memory-light host (every agent threaded in one process) instead of one watchdog+daemon per crew;
  `terminate_fleet` also stops the host process (tree-killed, so its venv-shim → c:\python child goes
  too). The legacy per-process model is still available directly: `watchdog.ps1 crews/crew_forge_crew.py`.
- **Repo-ROOT-scoped process detection.** `forge._is_running_file`, the TUI's process scan, and
  `terminate_fleet.ps1` now match this checkout's root (with a trailing-separator boundary so `crewfive`
  can't match `crewfive-dev`), so a **sibling clone** — e.g. a memory-light dev fleet beside prod — is never
  mistaken for ours: each reconciles, monitors and terminates independently. (Without this, a dev clone's
  reconcile saw the prod fleet's identically-named processes and launched nothing, leaving every agent stuck
  at onboarding 1/7.)

### Fixed
- **Survive a transient serve-tunnel drop instead of losing work.** The 06-20 Sanomat “partial” edition: the
  shared serve tunnel dropped mid-run and `write_pipeline` failed SILENT — a failed memory read looked like
  empty raw, so 7 article categories were dropped (their raw was intact) and a written article was lost when
  its publish hit the dead tunnel. Now `_aimeat_call` retries transient TRANSPORT failures (tunnel
  reconnecting / dropped connection / 5xx) with backoff (tool-level errors like NOT_FOUND are not retried, so
  “not found yet” polls stay cheap); `write_pipeline` distinguishes a failed read from genuinely-empty raw
  (`RawReadError`) and raises `WriteIncomplete` so the step goes RED and is retried — never a silent partial;
  `write-a`/`write-b` gained a step `retry`.
- **Quiet expected workspace probes.** A contract agent's idle poll scans organisms via `member_workspaces`,
  and offers read a fixed golden-sample workspace on every start; on a node that doesn't have those orgs
  (e.g. a dev node) these returned “not an active member” / “organism not found” and logged loudly every
  cycle. `_aimeat_call` gained a `quiet` flag for these EXPECTED probe failures; the org scan and the sample
  read use it (a real problem still surfaces through the agent's own deliverable).

## [0.4.0] — 2026-06-15 → 2026-06-18

### Added
- **Zero-infra web search (DuckDuckGo fallback).** New `crewaimeat.ddg_search.DdgSearchTool` queries
  DuckDuckGo directly via `ddgs` — no API key, no server, no Docker — emitting the exact same numbered
  title/URL/snippet block as the SearXNG tool, so crews need no changes. `crew._web_tools()` now
  auto-selects: `USE_TAVILY` → Tavily; `WEB_SEARCH=searxng|ddg|tavily` forces a backend; a reachable
  `SEARXNG_URL` (cached 1.5 s probe) → SearXNG; otherwise → DuckDuckGo. A self-hosted SearXNG is used
  transparently when present (dev fleet), while a bundled desktop install with nothing running falls
  back to DuckDuckGo automatically — zero configuration. Adds `ddgs>=6.0`.
- **research-crew reads full article bodies.** The Researcher agent in `research_crew.py` now carries
  `fetch_article_text` (trafilatura main-text extraction + crash-safe subprocess, Playwright fallback)
  alongside web search, and its task instructs the search → fetch-full-text → conclude chain. Findings
  are grounded in real article bodies instead of one-line search snippets. Verified end-to-end on a
  local gemma4 model: it correctly chained `web_search` → `fetch_article_text` (105k chars extracted).
- **Per-repo connector isolation (`AIMEAT_HOME`).** The connector home holding `serve.json`, tokens and
  agent configs is now resolved per-repo — `AIMEAT_HOME` (env wins) → else `<cwd>/.aimeat` — via
  `crewaimeat._home.aimeat_home()`, and the fleet pins `AIMEAT_HOME=<repo>/.aimeat` in every entrypoint
  (`start_fleet`/`serve_watchdog`/`watchdog` → inherited by crew-forge → every detached crew). All
  processes share ONE `serve.json` regardless of cwd, fully isolated from other projects' fleets (no global
  `~/.aimeat` collision). Requires `aimeat-crewai>=0.6.0`.
- **Curated fleet identity registry (`src/crewaimeat/fleet_identity.py`).** Central per-agent `tags`
  (charset-safe `[a-z0-9._-]`) + specific `capabilities` {technical, domain, languages}; the scaffold sets
  tags and reports capabilities on every start. A crew may override inline via `CrewSpec.tags`/`.capabilities`.
- **Offers: golden samples, JSON-shaped output, `dependsOn`, per-offer tagging.** Offer deliverables are
  tagged `offer:<id>`; tests cover golden samples, JSON shape, `dependsOn` and per-offer tags.
- **Bilingual TUI chrome (en/fi)** in `crewaimeat-tui`.
- **Content pipelines** — deterministic space-weather article writing + fetch pipeline; Finnish content
  generation with native style and agent-specific parameters.

### Changed
- **Home-scoped serve dedup + termination.** `serve_guard` dedup and `terminate_fleet` are scoped to this
  repo's `AIMEAT_HOME`, so they never reap or kill another home's serve daemons / fleet processes.
- **Exclusive supervisor lock** in the serve-watchdog prevents multiple supervisor instances.
- Crews re-declare their services on every start (idempotent).

### Fixed
- **Linux fleet entrypoints reach parity with Windows.** `start_fleet.sh` now pins `AIMEAT_HOME`,
  pre-starts the shared serve daemon (`ensure_serve.py`) and launches a serve-daemon supervisor before
  crew-forge; `watchdog.sh` pins `AIMEAT_HOME` so a standalone crew shares the fleet's serve.json/tokens;
  new `serve_watchdog.sh` ports the supervisor. Without these a Linux self-host crashed every crew with
  `AimeatServeError: No live serve daemon found … auto_start=False` (no daemon pre-started), and with
  0.6.0's per-directory home it resolved `<cwd>/.aimeat` instead of where the tokens live.

## [0.3.0] — 2026-06-13 → 2026-06-15

### Added
- **Fleet TUI — a lazydocker-style terminal monitor + manager (`crewaimeat-tui`).** A cross-platform
  (Windows/Linux) Textual app — the interactive successor to `scripts/view_fleet.ps1` — to watch and drive
  the whole fleet from one screen. `src/crewaimeat/tui/`:
  - **`fleet_state.py`** — the pure, testable data layer: merges the on-disk crew roster, the live process
    table (watchdog/daemon counts), the lock files, `serve.json`, and one read-only `aimeat_agents_list`
    into a `FleetSnapshot`. Status taxonomy extends view_fleet's (`running` / `down` / `orphan` /
    `DUPLICATE` / `zombie`) with **`stale-heartbeat`** — locally up but the node's `last_seen` is old (the
    "connector up, daemon not polling" case).
  - **`app.py` + `render.py`** — the Textual UI: a status bar, an agent table (color-coded status), and a
    detail pane with **Overview / Config / Logs tabs** (`o`/`c`/`l`). Overview shows the basics + the agent's
    README; Config shows the LLM profile + ordered provider→model chain + offer/workflow-compat counts; Logs
    tails the watchdog log. Two refresh tiers run off the UI thread: LOCAL (~2 s, no network) and NODE
    (~13 s, one cached `agents_list` call) — never a tight-loop AIMEAT call.
  - **`versions.py`** — installed vs latest for `aimeat-crewai` (PyPI) and the `aimeat` CLI (npm), with an
    update flag; fetched off-thread + cached.
  - **`agent_meta.py`** — per-agent enrichment, all LOCAL: the LLM routing chain (`llm_providers.json`), the
    offer/workflow-compatibility counts, and the crew's README (FIGLET banner reduced to plain text).
  - **`actions.py`** — fleet control behind confirm modals, off the UI thread: start/stop/restart a selected
    crew (`s`/`x`/`r`), start/stop/restart the whole fleet (`S`/`X`/`R`), re-auth (`a`), reap stray serve
    daemons (`d`). Every mutating action is safety-routed (stop kills watchdog-then-daemon by crew filename
    only — never the serve daemon; fleet stop uses `terminate_fleet.ps1`; reap uses `ensure_single_serve`).
  - Optional `[tui]` extra (`textual>=0.60`); the `crewaimeat-tui` entry point. Plan: `docs/internal/tui-plan.md`.
- **`forge.stop_crew` / `forge.recycle_crew`** — a real stop (kill the watchdog FIRST so it cannot respawn,
  then the daemon; matched by crew filename, so the serve daemon is never touched) and a true restart
  (stop → relaunch). Plain `start_crew` / `reauth` twins of the `@tool`-wrapped versions so code/the TUI can
  call them (a `@tool` object is not callable).
- **Single-serve invariant** (`src/crewaimeat/serve_guard.py`) — `ensure_single_serve()`: a cross-process
  lock around the check→spawn plus a dedup pass that reaps any serve daemon `serve.json` does not point at.
  Two daemons stole each other's tunnels (a reconnect storm) and dispatched tasks timed out silently — the
  "(L)AIMEAT Sanomat just didn't update, no error" failure. `scripts/ensure_serve.py` + the serve-watchdog
  now go through it.
- **postman durable mail dedup** (`mail_contract.process_mail`) — a per-machine sent-marker
  (`logs/.postman_mail_sent_runs.json`): a mail this machine already delivered is never re-sent, even when
  the workspace record's `done` write does not stick (a cross-agent settle / stale read). Fixes the
  "Market scan re-sent on every fleet start" bug.

## [0.2.0] — 2026-06-04 → 2026-06-13

### Added
- **Agent Workflows — chained scheduled pipelines with per-step health (crew-side reference + the live
  Sanomat migration).** A *workflow* is a declared, ordered set of steps with ONE schedule trigger and
  per-step two-sided **signals** — `required_to_function` (the consumer's input gate, checked before a step
  dispatches) and `success_signal` (the producer's output contract, checked after) — so the owner sees "did
  the step PRODUCE", not just "did it fire". `src/crewaimeat/workflow_spec.py` (the descriptor + a recursive
  signal evaluator: `exists`/`nonempty`/`count_nonempty`/`json_valid`/`json_field`, composites
  `all`/`any`/`when-then`, owner-scope memory reads, `check_workflow` test-run, `node_definition()` that emits
  the node `aimeat_workflow_save` payload), `workflow_inspector.py` + `crews/workflow_inspector_crew.py` (the
  three-tier diagnose/auto-repair/escalate handler), and `tests/test_workflow_spec.py`. The **(L)AIMEAT
  Sanomat 6→1 migration is live**: the six per-agent evening crons are replaced by one
  `laimeat-sanomat-evening` workflow (fetch → write-a/write-b → features/editorial, + space-weather), signals
  inherited from each stage agent's offer; reversible cutover (old schedules disabled, not deleted). First
  full run all-green 2026-06-13. The node owns the deterministic engine + signal evaluation; crewaimeat ships
  the descriptor/reference + the inspector. Node-engine spec + fix specs published to the AIMEAT Development
  workspace.
- **Agent Offers surface** (`src/crewaimeat/offers.py`) — each agent advertises what it does as machine-
  readable **offers**: derived deterministically from the workspace CONTRACTs (requirements / consequences /
  `deliverable.location` / repeatability / verification) plus authored constants for the task-runner crews,
  published via `PUT /v1/agents/:name/offers`. Offers also carry the workflow **signals** + `deliverableKey`
  (what makes an agent "workflow-compatible"), and the crews resolve **offer tasks** structurally (OFFER TASK
  SHAPE / `scope.offer_id`). Samples are a real excerpt of the agent's latest deliverable, never invented.
- **Connector forward tunnel** (aimeat 1.23.0 / aimeat-crewai 0.4.0) — `_aimeat_call` rides ONE shared
  loopback `aimeat connect serve --http` daemon (push task delivery, ~150 ms warm vs subprocess-seconds);
  64 connector processes → 2. `start_fleet.ps1` pre-starts the daemon once (`scripts/ensure_serve.py`).
- **Serve-daemon supervisor** (`src/crewaimeat/serve_watchdog.py` + `scripts/serve_watchdog.ps1`) — the shared
  tunnel daemon was an **unwatched single point of failure**; the supervisor calls the idempotent
  `ensure_serve` on a timer so a crashed daemon comes back in seconds and is never double-spawned. start_fleet
  launches it detached; terminate_fleet stops it first.
- **Native-crash isolation for web extraction** (`src/crewaimeat/_extract_worker.py`) — trafilatura → lxml →
  libxml2 can hard-kill the process with a Windows native fast-fail (exit `0xC0000409`,
  STATUS_STACK_BUFFER_OVERRUN, incl. at interpreter shutdown) on a malformed page — uncatchable from Python,
  and it took down the long-lived news-fetcher daemon. Extraction now runs in a **throwaway subprocess**
  (`article_extract._isolated_extract` → `python -m crewaimeat._extract_worker --url|--html`): a crash kills
  only the worker, the daemon survives and skips that one URL. stdout-first so a crash *after* the text was
  written still keeps it; UTF-8 in / ASCII-safe out.
- **`web-researcher` market + company research contracts** — `market-scan` (parameterized competitor/market
  analysis: who plays, where they advertise, how to sell against them; recurring, mailed) and
  `company-research` (Finnish company profiles: PRH/YTJ official XBRL financials first, then finder.fi charts
  via Playwright + vision), chained off the market scan.
- **`postman` + the 07:00 morning report** — an email-out workspace contract (SMTP, owner allowlist) that
  delivers a daily report (insights + action points + competitor watch), with a generic `extra-sections` hook
  other contracts append to, and the "Grok loop" (prompt in the mail, reply ingested back to the radar).
- **`image-scout`** — a moodboard contract: SearXNG image search → vision-curated gallery documents (subject/
  style/colors/relevance), uploaded via the presigned storage flow (binary never base64s through MCP).
- **Per-crew LLM routing (`llm_providers.json` profiles)** — `get_llm(agent_name=...)` now picks a named
  provider **profile** per crew: `{"profiles": {"content": {...}, "coding": {...}}, "default": "content",
  "crews": {"aimeat-app-builder": "coding", ...}}`. So content crews (news/editorial/features) route to
  **grok** and code/app crews (app-builder, conductor, cortex-fixer, realtime-builder, web-tester, crew-forge,
  …) route to a **non-grok coder** — grok is strong at prose, weak at code. The scaffold passes
  `spec.agent_name` when building each crew's `ctx.llm`; the deterministic content pipelines call `get_llm()`
  with no agent and so use `default`. The old flat `{"providers": [...]}` format still works (one chain for
  all). See `llm_providers.example.json`.
- **Deterministic content pipeline** — the CrewAI crews left deterministic steps to the LLM (whether to run
  trafilatura, which categories to write, copy-vs-rewrite the editorial) and grok skipped them → stub
  RSS-snippet raw, skipped/empty articles, a polite "Päätoimittaja" clobber of the gonzo editorial. Rewrote
  fetch/write/features/editorial as CODE orchestration (grok only writes prose):
  `fetch_pipeline` (curated feeds + SearXNG + ALWAYS trafilatura → rich raw),
  `write_pipeline` (code loop, a full article per category from the raw — no skips),
  `features_pipeline` (koodaus/prompt-niksi/matikka + parsed-and-validated quiz JSON),
  `editorial_pipeline` (gonzo S.J. editorial stored VERBATIM + deterministic `index_frontpage_auto` with
  per-article source counts — no publisher clobber). The news-fetcher / news-writer(+b) / editorial-writer /
  daily-features-writer crews are now thin wrappers that resolve the target date+edition and call one tool.
  Also: grok-4.3 primary via litellm-xai, curated RSS feed registry (`feed_sources.py`), per-article source
  counts + provenance badges in the newspaper, once-daily evening (18:00) schedule.
- **Automated test floor** (`tests/`, run with `uv run pytest`) — the first test suite in the repo.
  Deterministic, no LLM, no network: pure-function tests for the scaffold publish/verify path; a
  per-crew `build_domain` contract across all 27 crews (returns agents+tasks, in-crew agents, context
  chaining, **`ctx.prompt` is injected**, no delegation, `max_iter` is a sane backstop); the SYS-1
  completion-gate + auto-revert; and the reusable guardrails. See `tests/README.md`.
- **`crews/_guardrails.py`** — reusable, LLM-free task guardrails (fence-stripping JSON validation,
  required-field, 1–10 score, source-URL presence) for wiring onto prose-only task contracts.
- **`CrewSpec.require_verify_pass`** (default `False`) — **SYS-1**: for build/SDLC crews that run the app
  verify gates, gate task **completion** on the gates' deterministic `{ok}` outcome (not the agent's
  self-report). A build that failed a gate — or never ran one — is **failed** (`aimeat_task_fail`)
  instead of being marked `done` "green". Piloted on `aimeat-cortex-fixer` and `aimeat-realtime-builder`.
- **`CrewSpec.auto_revert_on_fail`** (default `False`) — SYS-1 live rollback: when set *with*
  `require_verify_pass`, a gate-fail also restores each app the run published to its pre-run last-good
  version (`author_tool.revert_apps_to_baseline`), re-publishing the prior version. Kept a **separate**
  opt-in from the (status-only) gate so the outward-facing live rollback is enabled deliberately, per
  crew. Off everywhere by default.
- **`AIMEAT_AGENT_MAX_EXECUTION_TIME`** env (default off) — an optional per-agent wall-clock bound that
  stops a *stuck* run without truncating a long-but-progressing build (safer than lowering `max_iter`).
- **`OPENROUTER_FALLBACK_MODELS`** env (default off) — a comma-separated **model-fallback chain** passed to
  OpenRouter as its `models` array (via litellm `extra_body`). OpenRouter tries each id IN ORDER and skips
  one that errors — **including a provider `400`** (verified against a cloaked/"alpha" model whose upstream
  went down). Keeps the fleet running when the primary model dies, and auto-resumes the primary if it
  recovers. E.g. `openai/gpt-oss-120b:free,openai/gpt-oss-20b:free,openrouter/owl-alpha`.
- **Multi-provider LLM routing (`llm_providers.json`, OpenClaw-style)** — a provider + model **priority
  chain**: `get_llm` tries providers in order and each provider's models in order, falling through on ANY
  error **across providers** (e.g. OpenRouter free → local **Ollama** → xAI). Each model carries its
  **context window**, and the chain sizes prompts to the *smallest* window so a 32k local model is never
  over-filled behind a 128k one. Types: `openrouter`, `ollama` (local, keyless), `xai`, `openai`, `generic`;
  a provider whose key env is missing is skipped (not fatal). `MultiProviderLLM` is **composition** over
  CrewAI's `LLM` (a `BaseLLM` subclass — CrewAI's `LLM` is a factory that re-dispatches subclasses). The env
  path (`OPENROUTER_MODEL` + `OPENROUTER_FALLBACK_MODELS`) still works when no config file is present.
  Gitignored; see `llm_providers.example.json`. Born from owl-alpha's outage: free models needed prioritising
  and a local fallback.
- **`scripts/check_models.py`** — a **model-capability check**: runs a battery (completion, JSON output, and a
  real SearXNG **search-crew**) against the models in `llm_providers.json` (or `--models a,b,c`) and prints a
  scorecard of which can actually drive crewaimeat. Surfaced the real fetch failure — weak models build
  garbage search queries (e.g. putting the date/edition in the query) and return nothing — so a model is
  vetted before the fleet trusts it. `--quick` skips the slow search test.
- **`revert_app` / `list_app_versions`** author tools + a per-run rollback **baseline** recorded by
  `publish_app`, so a crew (or the scaffold) can restore a prior working app version.
- **New crew `aimeat-app-designer`** — the SDLC "Web Designer": re-skins a functionally-ready app
  (Tailwind/DaisyUI + Motion One) in place, presentation-only, with `verify_interaction` as the
  regression gate.
- **Docs**: `docs/aimeat-guides/crewairesearch/` (researched CrewAI best-practices guides) and
  `docs/aimeat-guides/nextgeneration/` (an audit of this scaffold + 27 crews against those guides, with a
  prioritized roadmap and ready-to-run Claude Code eval prompts), plus an AIMEAT API request doc for the
  cortex/extension upsert (now delivered — see below).
- **`startup.prompt.md`** — a paste-into-Claude-Code/Copilot **runbook** that onboards a fresh clone
  end-to-end: it asks only what it can't know (which AIMEAT node — `aimeat.io` or self-hosted — the owner
  account, the model key), then installs, registers + approves the agents, starts the fleet, and teaches the
  essentials of working with AIMEAT. `README.md` now leads with it.
- **`fetch_article_text` author tool** (`src/crewaimeat/article_extract.py`) — full article-text extraction
  (**trafilatura** primary, Playwright-render fallback) with **top-N domain-diverse** URL selection, wired
  into `news-fetcher` so writers work from real article bodies, not 1-line search snippets.
- **Content pipeline greatly expanded** (the `(L)AIMEAT Sanomat` newspaper): **21 news sections, each with a
  named persona**, and **`news-writer` split into two parallel desks** (`news-writer` + new
  **`news-writer-b`**, ~12 agents each) so the write stage stays fast. New **`daily-features-writer`** crew
  (päivän koodausosio / prompt-niksinurkka / matematiikkahetki + an **interactive uutisvisa** generated from
  the day's news) and **`space-weather-writer`** (avaruussää article from NOAA/NASA). Newspaper widgets:
  Finland-oriented **moon phase**, **Sää tänään + huomenna** (Open-Meteo, CORS, incl. FMI HARMONIE), avaruussää
  images (NOAA SWPC + NASA SDO), **nimipäivät** from a shared public `almanac.namedays` key, per-article
  **🔊 Puhu** (Web-Speech TTS), the interactive quiz, and a **päivävalitsin** edition navigator that scales to
  many editions. Daily schedules run the whole thing twice a day autonomously (aamu/ilta).

### Changed
- **`install_cortex` / `install_extension`** now redeploy via the new idempotent **`PUT /v1/cortex/{name}`
  / `PUT /v1/extensions/{name}`** upsert (shipped on the AIMEAT node 2026-06-05) instead of
  `deactivate → DELETE → re-POST`. This removes the brief live outage and the cortex-quota churn on every
  redeploy. (An interim byte-compare "skip redeploy if unchanged" guard was added then retired once PUT
  landed — PUT is idempotent server-side.)
- **`ask_owner`** option parsing is robust: JSON array first (an option may contain commas), then
  `|`-delimited, then comma — fixing options like `"Text only (date, title, body)"` shattering into three.
- **`workflow._dispatch_one`** retries subtask creation 3× (with backoff) under connector load, instead of
  forging a redundant crew over a transient node blip.
- **Crew prompt hardening** to make build loops converge (the real fix for hit `max_iter` caps):
  `aimeat-app-builder` (exact-id selector discipline; reuse libs, prefer no cortex), `aimeat-cortex-fixer`
  (read extension-owned data from `ext:<ext>`; mandatory deploy **and** verify), `aimeat-realtime-builder`
  (the canonical realtime recipe: token → find-or-create room → presence from the `joined` event).

### Fixed
- **Durable per-machine run markers** (`src/crewaimeat/local_marks.py`) — a fleet restart could re-fire a
  contract scan that had already run (the market-scan "6 mails in one day" bug); markers now persist per
  machine so a restart can't re-trigger a completed scan.
- **Contract-agent runaway guard** — an idle-hook contract agent that deduped on a just-written status could
  re-process a request hundreds of times under read-after-write lag; added a per-run processed-set + per-run
  cap + output-existence dedup (never trust a status you just wrote back).
- **Offers**: deliverable samples are real multi-line Markdown (flattening made the leading `#` swallow the
  whole sample); offer tasks resolve structurally instead of drifting to a guessed target.
- **`daily-features-writer`** never fabricates the news quiz — it validates the quiz JSON and **skips** (loud)
  rather than writing a placeholder when too few articles are readable; **`editorial-writer`** has a
  self-healing guard for the evening edition. Both generalised by the workflow inspector.
- **Single-spawner discipline** — only `start_fleet` starts the shared serve daemon (crews attach, never
  spawn), preventing the multi-daemon "tunnel-stealing" storms; `start_fleet.ps1`'s fragile inline
  `python -c` step moved to `scripts/ensure_serve.py` (a quoting edge case raised a SyntaxError and aborted
  the start).
- **`news_writer`** — the three category-writer agents had **no `tools=`** yet their tasks instruct
  `write_memory(...)`, so articles never reached memory. Added `make_memory_tools` to all three.
- **`finnish_corporate_researcher`** — the synthesis report header was a non-f-string, so it printed the
  literal `{ctx.today}` / `{ctx.prompt}`. Now interpolated (clean date + the real query).
- **UTF-8 read fix** (`author_tool`) — all app/lib content reads (`read_app_source`, `revert_app`,
  `read_app_template`, `read_node_api`) force UTF-8; `requests`' Latin-1 default for `text/html` was
  corrupting Scandinavian text (`ä`→`Ã¤`) on every read-then-republish.
- **Newspaper view counter** — rewritten from one-key-per-view (which hit the `/v1/mm` **100-keys-per-set**
  cap and started 400-ing) to a **per-edition counter** (read → +1 → overwrite); historical views migrated,
  counts preserved.
- **`index_frontpage` dedups by concrete `(gaii, key)`** — the old logical-slot key drifted when `kind`
  varied between two editorial runs, so every article got a second front-page entry ("tuplauutiset"). The
  public viewer also dedups client-side as a belt-and-suspenders.

### Notes
- The **`max_iter` audit recommendation was reversed by field data**: `max_iter` is a justified backstop
  (it fires on non-convergent re-authoring loops, not runaways), so the test floor no longer pressures
  lowering it. The real runaway levers are prompt convergence, verify-gated completion + auto-revert, and
  the optional wall-clock bound. See `docs/aimeat-guides/nextgeneration/04-general-improvement-roadmap.md`.
- Still open on the AIMEAT side (they flagged it): `generator-registration.ts` loses cortex lib files on a
  *generator* re-deploy (cascade-delete then recreate without re-writing libs) — a separate path from the
  HTTP routes that were fixed.

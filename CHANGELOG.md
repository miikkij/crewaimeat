# Changelog

Notable changes to crewaimeat. Format loosely follows [Keep a Changelog](https://keepachangelog.com/).
Dates are the working dates; entries are **uncommitted and take effect on the next fleet restart**
(the daemons import the modules at start).

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

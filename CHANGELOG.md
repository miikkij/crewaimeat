# Changelog

Notable changes to crewaimeat. Format loosely follows [Keep a Changelog](https://keepachangelog.com/).
Dates are the working dates; entries are **uncommitted and take effect on the next fleet restart**
(the daemons import the modules at start).

## [Unreleased] — 2026-06-04 → 2026-06-05

### Added
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

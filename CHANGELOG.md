# Changelog

Notable changes to crewaimeat. Format loosely follows [Keep a Changelog](https://keepachangelog.com/).
Dates are the working dates; entries are **uncommitted and take effect on the next fleet restart**
(the daemons import the modules at start).

## [Unreleased] — 2026-06-04 → 2026-06-05

### Added
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
- **`revert_app` / `list_app_versions`** author tools + a per-run rollback **baseline** recorded by
  `publish_app`, so a crew (or the scaffold) can restore a prior working app version.
- **New crew `aimeat-app-designer`** — the SDLC "Web Designer": re-skins a functionally-ready app
  (Tailwind/DaisyUI + Motion One) in place, presentation-only, with `verify_interaction` as the
  regression gate.
- **Docs**: `docs/aimeat-guides/crewairesearch/` (researched CrewAI best-practices guides) and
  `docs/aimeat-guides/nextgeneration/` (an audit of this scaffold + 27 crews against those guides, with a
  prioritized roadmap and ready-to-run Claude Code eval prompts), plus an AIMEAT API request doc for the
  cortex/extension upsert (now delivered — see below).

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

### Notes
- The **`max_iter` audit recommendation was reversed by field data**: `max_iter` is a justified backstop
  (it fires on non-convergent re-authoring loops, not runaways), so the test floor no longer pressures
  lowering it. The real runaway levers are prompt convergence, verify-gated completion + auto-revert, and
  the optional wall-clock bound. See `docs/aimeat-guides/nextgeneration/04-general-improvement-roadmap.md`.
- Still open on the AIMEAT side (they flagged it): `generator-registration.ts` loses cortex lib files on a
  *generator* re-deploy (cascade-delete then recreate without re-writing libs) — a separate path from the
  HTTP routes that were fixed.

# Architecture — crewaimeat

This is the **map of the codebase**: what the moving parts are, how they fit, and where to add
things so you reuse what exists instead of re-implementing it. For *why* (strategy, roadmap,
hard-won pitfalls) read the AIMEAT organism workspace (see [CLAUDE.md](CLAUDE.md)); for *how to
write one crew*, read [SCAFFOLD_CANON.md](SCAFFOLD_CANON.md). This file is the structural overview.

---

## What this project is

A **toolkit + patterns for running CrewAI agents on the AIMEAT substrate** (aimeat.io). You write a
small `build_domain` per crew; the locked scaffold provides everything else — AIMEAT connection,
the long-running daemon, live progress, identity/offers, LLM routing, and fleet supervision. The
goal is to make standing up a new, discoverable, reliable AIMEAT agent cheap.

Two distinct agent shapes run on the same scaffold:

- **task-runner** — consumes a free-text task (`ctx.prompt`) and produces a deliverable. *Drive it
  with a prompt* (e.g. `image-maker`: "a misty harbour at dawn" → image URL).
- **workspace-contract** — adopts a contract (input/output *spaces* with schemas) and processes
  structured **request records** from a memory namespace on its idle poll. *Drive it by writing a
  request record*, not a chat prompt (e.g. `image-scout` reads `moodboard-request` records).

Knowing which shape an agent is tells you how to task it — the TUI Test tab surfaces each agent's
own "How to task me" line for exactly this reason.

---

## Tech stack

| Layer | Choice |
|---|---|
| Language / runtime | Python `>=3.10,<3.14` |
| Package / env | **uv** (`uv run`, `uv sync`); build backend **hatchling** |
| Agent framework | **CrewAI** (`crewai[tools]`) — Agents + Tasks + Crew |
| AIMEAT connector | **`aimeat-crewai`** (`>=0.6.0`) — liaison, serve daemon, per-repo home |
| LLM access | **litellm** via CrewAI `LLM`; OpenRouter / xAI / local Ollama |
| TUI | **Textual** (optional `[tui]` extra) |
| Web / tools | SearXNG + DuckDuckGo (`ddgs`) + optional Tavily; `playwright` (web-tester); `trafilatura` (extraction) |
| Images | ByteDance Seedream 4.5 via OpenRouter (`seedream_gen`) |
| Tests | **pytest** — a deterministic floor: no LLM, no network |
| Lint / format | **Ruff** (`[tool.ruff]`), wired into pre-commit + CI |

---

## Directory layout

```
src/crewaimeat/         the LOCKED scaffold + shared machinery (the package)
  aimeat_crew.py        run_crew / CrewSpec / BuildContext — the heart; liaison + daemon + dispatch
  _home.py              AIMEAT_HOME resolution (single source of truth)
  llm.py                LLM factory: provider-profile routing + per-agent overrides
  fleet_identity.py     curated per-agent tags + capabilities (what each agent advertises)
  offers.py             what each agent OFFERS (crew offers + contract-derived offers)
  forge.py              fleet control: launch / stop / recycle a crew, reconcile the fleet
  serve_guard.py        enforce exactly one shared serve daemon
  serve_watchdog.py     supervise the serve daemon
  scaffold.py           the `crewaimeat new-crew` CLI + scaffolding
  *_contract.py         contract definitions (CONTRACT = spaces + JSON schemas)
  contract_adopt.py     provision a contract's spaces into a workspace ("Adopt contract")
  *_pipeline.py         DETERMINISTIC content pipelines (fetch/write/features/editorial/space-weather)
  workflow_spec.py      WORKFLOWS: declared multi-step pipelines + per-step signals
  workflow.py /         workflow execution + inspection
    workflow_inspector.py
  evolve.py / evolve_run.py   reputation / variant-selection (lab benchmark ↔ field selection)
  tui/                  the fleet TUI (see below)
  <tools>               searxng_search, ddg_search, browser_tool, seedream_gen, librarian, …

crews/                  ONE file per agent: <name>_crew.py with build_domain + AGENT_NAME + README
scripts/                fleet entrypoints (start_fleet.ps1, watchdog.ps1, serve_watchdog.ps1, …)
tests/                  the pytest floor (deterministic; mirrors module names)
docs/                   guides (crewairesearch, nextgeneration audit, integration)
.aimeat/                per-repo connector home — serve.json, tokens, agent configs (GITIGNORED)
llm_providers.json      LLM routing config (profiles + per-crew profile assignment)
```

---

## The scaffold — one crew's life

`crews/<name>_crew.py` exports three things and calls `run_crew`:

```python
AGENT_NAME = "image-maker"
README = '''…How to task me: describe the image you want…'''
def build_domain(ctx: BuildContext) -> tuple[list[Agent], list[Task]]:
    ...                          # the ONLY crew-specific code
run_crew(CrewSpec(agent_name=AGENT_NAME, build_domain=build_domain, readme_md=README, ...))
```

`run_crew` (in [aimeat_crew.py](src/crewaimeat/aimeat_crew.py)) provides everything else:

1. **Connect & onboard** — runs the Hello-Integration once; sets identity (tags via
   `aimeat_agent_tags_set`, capabilities via `aimeat_agent_capabilities_report`) from
   `fleet_identity.py`; publishes offers from `offers.py`.
2. **LLM** — builds the model via `llm.get_llm(agent_name=...)` so routing is per-agent.
3. **Daemon loop** — polls AIMEAT for tasks; on each task, calls `build_domain(ctx)` (with
   `ctx.prompt`, `ctx.today`, `ctx.llm`, a liaison), runs the CrewAI kickoff, and writes the
   deliverable to memory (`crews.<agent>.<slug>.latest_output`). Idle hooks (`idle_hook`) let
   contract agents poll their request namespace deterministically.
4. **Live progress** — a deterministic heartbeat writes status to a memory key (no LLM).

**Dispatch:** all deterministic AIMEAT calls go through one helper, `_aimeat_call(agent, tool,
payload)` — POST to the shared loopback serve daemon (`/local/call/<tool>`), with a subprocess
fallback. This is the single channel the TUI, pipelines, and contracts all use.

---

## Runtime topology (the fleet)

```
start_fleet.ps1  ─ pins AIMEAT_HOME=<repo>/.aimeat, ensures ONE serve daemon, launches crew-forge
   │
   ├── serve daemon (one, shared)         ── the loopback tunnel every crew calls through
   ├── serve_watchdog.ps1                  ── keeps the serve daemon alive (single-instance lock)
   └── watchdog.ps1 <crew>  (one per crew) ── keeps each crew daemon alive; restarts on crash
          └── crew daemon (python)         ── run_crew loop for that agent
```

- **One connector home per repo**: `AIMEAT_HOME` (env wins, else `<cwd>/.aimeat`), resolved *only*
  via `_home.aimeat_home()`. Pinned in every entrypoint so all processes share one `serve.json`,
  isolated from other repos' fleets. Holds tokens → gitignored.
- **`forge.py`** is the control surface: `launch_crew` / `stop_crew` / `recycle_crew` (true restart)
  / `reconcile_fleet` (idempotent — launches stopped crews, skips running ones).

---

## LLM routing

`llm_providers.json` is **profile-based**: named `profiles` (provider→model fallback chains) and a
`crews` map assigning each agent to a profile (content crews → grok; code/app crews → a real coder).
`llm.get_llm(agent_name)` resolves the chain via `_select_chain` and builds a `MultiProviderLLM`
that falls through providers/models on error (OpenRouter → free → local Ollama → xAI).

**Per-agent override** (set from the TUI model picker): `<AIMEAT_HOME>/llm_overrides.json`
(gitignored) pins one agent to a specific model or profile. `_select_chain` consults it **first**,
so the daemon's `get_llm` and the TUI's Config display always agree. See `llm.save_override` /
`clear_override` / `available_models`.

---

## Offers, contracts, identity, workflows (discovery surface)

These are **local Python constants** — the data the fleet advertises and the TUI Config tab renders:

- **`offers.py`** — `_CREW_OFFERS` (crew-task offers) + `_OFFER_META` (contract-derived offers).
  An offer = `{id, title, ask, deliverable, signals, …}`. This is how other agents/humans discover
  what an agent does.
- **`*_contract.py`** — `CONTRACT = {id, spaces:[{space, namespace, mode, schema}]}`. Defines a
  contract agent's input/output spaces and their JSON schemas. `contract_adopt.py` provisions them
  into a workspace.
- **`fleet_identity.py`** — `FLEET_IDENTITY[agent] = {tags, capabilities}` (the matcher reads these).
- **`workflow_spec.py`** — `WORKFLOWS[id] = {schedule, vars, steps}`; each step binds an `agent` +
  `offer` with `after` edges and two-directional signals (`required_to_function` / `success_signal`).

When you add or change an agent, keep these in sync (identity registry, an `offers.py` entry, the
README constant) — discovery reads all of them.

---

## The TUI (`src/crewaimeat/tui/`)

A Textual, lazydocker-style fleet monitor + manager (`uv run crewaimeat-tui`). Clean separation so
the logic unit-tests without a terminal:

| Module | Responsibility |
|---|---|
| `fleet_state.py` | pure data layer — build the `FleetSnapshot` (process table, locks, serve.json, node index) |
| `render.py` | pure presentation — format rows / status bar / detail panes (no Textual import) |
| `agent_meta.py` | per-agent enrichment (LLM chain, README, offers, contracts, identity, overrides) — all local |
| `app.py` | the Textual `App` — table + Overview / **Test** / Config / Logs tabs, actions, modals |
| `actions.py` | thin wrappers over `forge` / `serve_guard` control functions (behind confirm modals) |
| `test_run.py` | live agent test — create a real task, poll the deliverable (the Test tab engine) |
| `versions.py` / `i18n.py` | version check · en/fi chrome |

**Test tab** exercises the live daemon: it creates a real AIMEAT task and polls the agent's
`latest_output` (real model, real tunnel). **Config tab** shows the LLM chain + override + offers +
contract schemas + capabilities + workflow membership. **Model picker** (`m`) pins a model and
restarts the agent via `forge.recycle_crew`.

---

## Conventions that keep it coherent

- **uv** for everything (`uv run` / `uv sync`).
- **Fail loud** — reject at the boundary or raise from one shared dispatcher; a guessing fallback is
  a bug, not a safety net.
- **Resolve `AIMEAT_HOME` via `_home.aimeat_home()`** — never re-derive the path.
- **Positive framing** in all user-facing text (say what to do).
- **One dispatcher** (`_aimeat_call`) for deterministic AIMEAT calls.
- **Tests are the floor** — deterministic, no LLM, no network; add one when you add a behaviour.
- **Ruff** gates style (pre-commit + CI); formatting is already a no-op (line-length 120).

---

## Where to add things

| You want to… | Touch |
|---|---|
| Add a new agent | `crews/<name>_crew.py` (+ `crewaimeat new-crew`), `fleet_identity.py`, `offers.py` |
| Give an agent a contract | a new `*_contract.py` (`CONTRACT`), wire adoption via `contract_adopt.py` |
| Change LLM routing | `llm_providers.json` (profile) or the TUI model picker (per-agent override) |
| Add a multi-step pipeline | `workflow_spec.py` (`WORKFLOWS`) + the deterministic `*_pipeline.py` stages |
| Extend the TUI | `tui/agent_meta.py` (data) + `tui/render.py` (format) + `tui/app.py` (wire) — keep render pure |
| Add fleet control | `forge.py` (logic) + `tui/actions.py` (expose) |

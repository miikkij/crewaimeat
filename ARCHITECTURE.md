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

Two ways to **define** an agent:

- **Python crew** — `crews/<name>_crew.py` IS the agent. Change it, restart it.
- **Node-backed JSON agent** — on disk only a loader that names the agent; the definition lives at
  `crews.registry.<agent>` on the node and the owner edits it in the Crew tab. `build_domain`
  re-reads it per task, so a change takes effect on the next task with no restart
  (`json_agent.py`, `crew_def.py`, `crew_invoke.py`, `crew_publish.py`).

Two ways to **run** an agent, chosen by the node's `run_mode` (the owner's setting):

- **resident** — a thread in `fleet_host`, loaded for as long as the fleet runs.
- **spawn** — data on the node while idle; `spawner` starts one worker process per wake, which runs
  one cycle (`run_crew(one_shot=True)`) and exits. The fleet here runs every agent this way.

And first, whether it should be an agent at all: the default is an AIMEAT-side **app-tool** or
**workflow**. An agent earns its cost only when it acts without being called, plans its own next step
from a real result, is a federated party, or runs long and unattended (see CLAUDE.md).

---

## Tech stack

| Layer | Choice |
|---|---|
| Language / runtime | Python `>=3.10,<3.14` |
| Package / env | **uv** (`uv run`, `uv sync`); build backend **hatchling** |
| Agent framework | **CrewAI** (`crewai[tools]`) — Agents + Tasks + Crew |
| AIMEAT connector | **`aimeat-crewai`** (see `pyproject.toml` for the current floor) — liaison, serve daemon, per-repo home, deterministic Hello Integration, usage metering. Source lives in [aimeat-protocol](https://github.com/miikkij/aimeat-protocol) |
| npm connector | the global `aimeat` CLI (**>= 3.13.4**) — `aimeat connect serve`, the loopback tunnel every crew calls through; `AIMEAT_CLI` overrides the binary |
| LLM access | **litellm** via CrewAI `LLM`; OpenRouter / xAI / OpenAI-compatible / local Ollama |
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
  transport.py         node MCP, JSON REST and raw/streamed REST transport with injected runtime hooks
  lifecycle.py         deterministic publish and completion callbacks, including verify gates
  _sqlite.py           transaction and connection lifetime shared by the six SQLite stores
  session_store.py     expiring conversation state and separately stored durable preferences
  _home.py              AIMEAT_HOME resolution (single source of truth)
  llm.py                LLM factory: provider-profile routing + per-agent overrides
  llm_choice.py         the owner's model choice read from the node (crews.llm.<agent> / .default)
  directives.py         the owner's directives, prepended to every model call get_llm hands out
  agent_manifest.py     each crew's OWN declaration, read statically (the one source per agent)
  fleet_identity.py     resolves tags + capabilities FROM the crew (empty central dict = fallback)
  offers.py             offer machinery; the authored per-crew offers are derived from the crews
  doctor/               registry reconciliation + call-graph route conformance + node liveness
  retire.py             the opposite of forging an agent (park, unregister, stash token)
  fleet_economics.py    model spend per agent, and who spends without producing
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
  fleet_host.py         resident crews as threads; node-selected spawn crews run through spawner.py
  spawner.py /          spawn mode: node roster (?run_mode=spawn), one worker process per wake,
    run_once.py /         single-flight per agent; run_once = one daemon cycle, then exit
    spawn_state.py
  node_engine.py        which connector CLI to run (AIMEAT_CLI override, else the global install)
  json_agent.py         node-backed JSON agents: load/refresh crews.registry.<agent>, report to
                          crews.runtime.<agent>, seed a staged definition on first start
  crew_invoke.py        answers the node's crew.validate / crew.try / crew.menu calls
  crew_publish.py       the publish / install / defs CLI; crew_try.py = `crewaimeat try`
  app_tools.py          list_app_tools / call_app_tool — a crew calling the owner's AIMEAT app-tools
  node_cleanup.py       `crewaimeat orphans`; quality.py = `crewaimeat quality`
  skills.py /           SKILL.md loading (repo-local) + registry/workspace skill attachment
    skills_registry.py
  crew_def.py /         declarative JSON crew definitions: interpreter/validator + the forge `/build-json`
    forge_json.py         path + AIMEAT publish/install (crew_registry.py)
  agency/               the aimeat-agency cockpit (FastAPI): wizard, brains, fleet ops, copilot
    brain_routes.py / memory_routes.py   authenticated route groups mounted by cockpit.create_app
    api_models.py / reset.py             request schemas and explicit reset failure reporting
  tui/                  the fleet TUI (see below)
  <tools>               searxng_search, ddg_search, browser_tool, seedream_gen, librarian, …

crews/                  ONE file per agent: <name>_crew.py with build_domain + AGENT_NAME + README
                        (a leading underscore parks a crew — skipped by fleet discovery)
crew_defs/              declarative JSON crew definitions (data for crew_def.py / the registry)
skills/                 SKILL.md expertise packs crews load via CrewSpec.skills (see skills/README.md)
scripts/                fleet entrypoints (start_fleet, start_host, watchdog, view/terminate_fleet,
                        register_fleet.py, check_models.py; .ps1 Windows / .sh macOS+Linux)
aimeat-agency/          the Tauri desktop shell over crewaimeat.agency.cockpit (see its README)
tests/                  the pytest floor (deterministic; mirrors module names)
benchmarks/             the LOCOMO memory-benchmark harness (driven by scripts/run_locomo.py)
infra/searxng/          optional self-hosted SearXNG (docker compose) for keyless web search
docs/                   guides; the large working sets (docs/aimeat-guides, docs/internal) are
                        local-only (gitignored)
.aimeat/                per-repo connector home — serve.json, tokens, agent configs (GITIGNORED)
llm_providers.json      LLM routing config (profiles + per-crew profile assignment; gitignored,
                        copy llm_providers.example.json)
```

---

## The scaffold — one crew's life

`crews/<name>_crew.py` declares the agent and calls `run_crew`:

```python
AGENT_NAME = "image-maker"
README = '''…How to task me: describe the image you want…'''
def build_domain(ctx: BuildContext) -> tuple[list[Agent], list[Task]]:
    ...                          # the ONLY crew-specific code
run_crew(CrewSpec(agent_name=AGENT_NAME, build_domain=build_domain, readme_md=README, ...))
```

`run_crew` (in [aimeat_crew.py](src/crewaimeat/aimeat_crew.py)) provides everything else:

1. **Connect & onboard** — runs Hello Integration once, then sets identity from the CREW's own
   declaration (tags via `aimeat_agent_tags_set`, capabilities via
   `aimeat_agent_capabilities_report` — rejected at the boundary if malformed) and publishes its
   `OFFERS` through the daemon. The agent's MODE is **not** written: it is the owner's standing
   instruction on the node (a task is auto-activated only for a `task-runner`), and stamping it on
   every start overwrote modes owners had set on purpose. `CrewSpec.mode` only declares what the
   crew expects.
2. **LLM** — builds the model via `llm.get_llm(agent_name=...)` so routing is per-agent; the
   returned model carries the owner's directives as a system message on every call.
3. **Daemon loop** — polls AIMEAT for tasks. PROPOSE: a task-runner's TODO plan is written
   deterministically (`_DeterministicPhase`, no model) because nobody reads it before the work
   starts; other modes keep the model, because a person reads that plan to decide. EXECUTE: calls
   `build_domain(ctx)` (with `ctx.prompt`, `ctx.today`, `ctx.llm`, a liaison), runs the CrewAI
   kickoff — or `CrewSpec.on_task` when the work is deterministic — and the publish and complete
   callbacks write the deliverable (`crews.<agent>.<slug>.latest_output`), mark the todos done, and
   run any verify gate. Idle hooks (`idle_hook`) let contract agents poll their request namespace
   deterministically. With `one_shot=True` the loop runs one cycle and returns (spawn workers).
4. **Live progress** — a deterministic heartbeat writes status to a memory key (no LLM).
5. **Invoke listener** — `crew.validate` / `crew.try` / `crew.menu` calls from the node are answered
   in a worker pool beside the agent's own work (`on_invoke`, aimeat-crewai >= 0.22).

**Dispatch:** `_aimeat_call(agent, tool, payload)` uses the shared loopback daemon's MCP dispatcher,
with a subprocess fallback. `_aimeat_rest` handles JSON envelopes; `_aimeat_request` returns raw
responses for text and binary consumers. Their implementation is in `transport.NodeTransport`.
It owns node authentication and attribution. Presigned uploads to external storage remain external
HTTP calls. Raw reads can retry a broken connection; raw mutations make one attempt by default.

**Identity:** an agent is addressed by its GAII (`<agent>#<owner>@<node>`), read from the credential
(a v2 key file or a v1 bearer), never assembled; the bare name finds the credential file and builds
`/v1/agents/<name>/…` paths. One connector home can serve several owners, so a bare name two owners
share is refused. Before the first write, the runtime checks that the loopback proxy stamps calls
with this agent's identity; a proven mismatch refuses the write, and an unknown answer proceeds.

**State:** SQLite contexts commit or roll back and explicitly close the connection, including on
schema failures. Conversation state expires on reads after seven days. Durable reader/briefing
preferences use their own table, with lazy migration from legacy pseudo-conversations. HITL replies
must match a unique request and atomically consume it before resolving; expired or superseded replies
cannot approve a newer action. Chat windows select the newest turns and return chronological order.

---

## Runtime topology (the fleet)

```
start_fleet.ps1 ─ pins AIMEAT_HOME=<repo>/.aimeat and starts the shared services
   │
   ├── serve daemon (one, shared)          ── the loopback tunnel every crew calls through
   ├── serve_watchdog.ps1/.sh              ── keeps the serve daemon alive (single-instance lock)
   ├── spawner_watchdog.ps1 + spawner      ── node run_mode=spawn agents: roster re-read every 30 s,
   │      └── run_once worker per wake        one worker process per agent at a time, exits after a cycle
   └── fleet host (crewaimeat.fleet_host)  ── resident crews as threads in one process
          └── run_crew loop per agent         (crewai imported once; empty host roster exits)
```

`start_fleet.sh` starts the serve daemon, its supervisor and the host, but not the spawner; on
macOS/Linux run `uv run crewaimeat spawner` yourself. `terminate_fleet` stops the spawner first,
because it would revive the workers.

Legacy per-process model (still available, for per-crew process isolation): start crew-forge under
`watchdog.ps1` and it reconciles the fleet — one watchdog + one crew daemon (python) per agent.
Pick ONE model per checkout; the per-agent single-instance lock exits whichever starts second.

- **One connector home per repo**: `AIMEAT_HOME` (env wins, else `<cwd>/.aimeat`), resolved *only*
  via `_home.aimeat_home()`. Pinned in every entrypoint so all processes share one `serve.json`,
  isolated from other repos' fleets. Holds tokens → gitignored.
- **`forge.py`** is the control surface: `launch_crew` / `stop_crew` / `recycle_crew` (true restart)
  / `reconcile_fleet` (idempotent — launches stopped crews, skips running ones).

---

## LLM routing

`llm_providers.json` is **profile-based**: named `profiles` (provider→model fallback chains). Each
crew names its own profile (`LLM_PROFILE`; a JSON definition's `llm_profile`), and the file's
`crews` map is a per-machine override. Content crews → grok for English prose; Finnish prose → the
`news` profile; code/app crews → `coding`. `llm.get_llm(agent_name)` resolves the chain via
`_select_chain` and builds a `MultiProviderLLM` that falls through providers/models on error.

`_select_chain` precedence, strongest first: a local pin (`<AIMEAT_HOME>/llm_overrides.json`, set
from the TUI model picker) → the owner's choice for this agent on the node (`crews.llm.<agent>`,
`llm_choice.py`, cached 60 s, never fatal) → the machine's `crews` map → the crew's declaration →
the owner's node default (`crews.llm.default`) → the file's `default`. The daemon's `get_llm` and the
TUI's Config display use the same function, so they always agree. Each agent publishes the catalogue
of models it can reach into its own namespace, and the node reads it from there.

No `max_tokens` is sent to a cloud model: on a reasoning model the thinking and the answer share
that budget, and every guessed ceiling so far returned empty or truncated output. A local Ollama
server is the one exception, because it allocates against the number.

---

## Offers, contracts, identity, workflows (discovery surface)

These are **local Python constants** — the data the fleet advertises and the TUI Config tab renders.
Since 2026-08-22 the per-agent half of it lives in the CREW FILE and the central lists are derived:

- **the crew file** — `LLM_PROFILE`, `TAGS`, `CAPABILITIES`, `OFFERS`, `SKILLS` (and optionally
  `RUN_MODE`, `PROMPT_INDEPENDENT`) at module level (a JSON crew states the same keys in its doc). `agent_manifest.py` reads them with `ast`, never by importing.
- **`offers.py`** — the offer MACHINERY: `_OFFER_META` (contract-derived offers) plus `crew_offers()` /
  `crew_offer_agents()`, which assemble the authored offers from what the crews declare. An offer =
  `{id, title, ask, deliverable, signals, …}` — how other agents and humans discover what an agent does.
- **`*_contract.py`** — `CONTRACT = {id, spaces:[{space, namespace, mode, schema}]}`. Defines a
  contract agent's input/output spaces and their JSON schemas. `contract_adopt.py` provisions them
  into a workspace.
- **`fleet_identity.py`** — `identity_for(agent) -> {tags, capabilities}`, resolved from the crew. The
  `FLEET_IDENTITY` dict is empty and kept only as a fallback for an agent with no reachable crew file.
- **`workflow_spec.py`** — `WORKFLOWS[id] = {schedule, vars, steps}`; each step binds an `agent` +
  `offer` with `after` edges and two-directional signals (`required_to_function` / `success_signal`).
  Every step carries a `retry`: a step without one is one transient failure from permanently red,
  and it takes every step that waits on it down too.

When you add or change an agent, it is ONE file: the crew's own declaration plus its `README` constant.
Nothing central needs updating — and `crewaimeat doctor` reports any crew that leaves a field blank,
which is the guarantee that replaced "keep these in sync".

---

## The TUI (`src/crewaimeat/tui/`)

A Textual, lazydocker-style fleet monitor + manager (`uv run crewaimeat-tui`). Clean separation so
the logic unit-tests without a terminal:

| Module | Responsibility |
|---|---|
| `fleet_state.py` | pure data layer — build the `FleetSnapshot` (process table, locks, serve.json, node index) |
| `render.py` | pure presentation — format rows / status bar / detail panes (no Textual import) |
| `agent_meta.py` | per-agent enrichment (LLM chain, README, offers, contracts, identity, overrides) — all local |
| `app.py` | the Textual `App` — table + Overview / **Test** / Config / Logs tabs, actions, modals. Status `parked` = the spawner holds the agent (the resting state of a spawn agent); `attached (no runtime)` = the node can reach it but nothing on this machine would run it |
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
- **The model writes and judges; everything else is code.** Parsing, fetching, formatting, sending
  and checking are deterministic. A crew that never needs the model uses `CrewSpec.on_task`, which
  keeps the publish/complete callbacks; never route around them.
- **A refusal is not an empty result.** A 401/403 from the node is reported with the node's own
  code (aimeat-crewai >= 0.26), never flattened into "nothing to do".
- **No invented limits.** No `max_tokens`, character count or retry cap that is not the vendor's
  documented one or the owner's stated one.
- **Tests are the floor** — deterministic, no LLM, no network; add one when you add a behaviour.
- **Ruff** gates style (pre-commit + CI); formatting is already a no-op (line-length 120).

---

## Where to add things

| You want to… | Touch |
|---|---|
| Decide agent vs tool | the criteria in CLAUDE.md — default to an AIMEAT app-tool or workflow |
| Add a new agent | `crews/<name>_crew.py` only (+ `crewaimeat new-crew`) — declare `LLM_PROFILE` / `TAGS` / `CAPABILITIES` / `OFFERS` there; verify with `crewaimeat doctor` |
| Add an owner-editable agent | `crewaimeat new-json-agent <name>`; try a definition first with `crewaimeat try` |
| Make an agent cost nothing while idle | set its `run_mode` to `spawn` on the node (the owner's setting); `RUN_MODE = "spawn"` in the crew only states the request |
| Skip the model for deterministic work | `CrewSpec.on_task` (keeps the publish/complete callbacks) |
| Remove an agent | `crewaimeat retire <agent> --apply` (parks the crew, unregisters, stashes the token) |
| See what an agent costs | `crewaimeat costs` |
| Give an agent a contract | a new `*_contract.py` (`CONTRACT`), wire adoption via `contract_adopt.py` |
| Change LLM routing | the crew's `LLM_PROFILE` (its default), the owner's choice on the node (`crews.llm.<agent>`), `llm_providers.json` → `crews` (per-machine override), or the TUI model picker (per-agent pin) |
| Add a multi-step pipeline | `workflow_spec.py` (`WORKFLOWS`) + the deterministic `*_pipeline.py` stages |
| Extend the TUI | `tui/agent_meta.py` (data) + `tui/render.py` (format) + `tui/app.py` (wire) — keep render pure |
| Add fleet control | `forge.py` (logic) + `tui/actions.py` (expose) |

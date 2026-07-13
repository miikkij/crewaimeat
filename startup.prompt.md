# startup.prompt.md — paste this into Claude Code or Copilot to set up this repo

**You are an AI coding assistant.** A user has this repository open and wants you to get it running.
Your job: take them from a fresh clone to **running crewaimeat agents connected to an AIMEAT node**, then
explain what they can do and the essentials of working with AIMEAT. Work the checklist top-to-bottom,
**run the commands for the user**, and **ask only what you genuinely cannot determine** (which AIMEAT
node, the owner handle, the model keys).

---

## What this repo is (read first, then act)

**crewaimeat** = the **agent runtime** for AIMEAT: a tested scaffold + fleet tooling for building
**CrewAI crews that live as agents on an AIMEAT node**. It is the sibling of
[aimeat-protocol](https://github.com/miikkij/aimeat-protocol) — that repo is the **node** (the AIMEAT
protocol spec + reference server); this repo is the **runtime** that connects agents to a node. The bridge
is the **`aimeat-crewai`** package (installed from PyPI by `uv sync`; its source lives in
aimeat-protocol, not here) — it provides the liaison agent, the task daemon, and the shared local
serve tunnel every crew calls through.

- You (or the user) write only `build_domain(ctx) -> (agents, tasks)` per crew. The scaffold
  (`src/crewaimeat/aimeat_crew.py`) provides the rest: AIMEAT connection + onboarding, the daemon,
  live progress, identity/offers, LLM routing. See `SCAFFOLD_CANON.md` and `ARCHITECTURE.md`.
- `crews/` already holds ~40 working crews (a leading underscore = parked/dormant). Notables:
  **crew-forge** (an agent that builds and launches other agents), a research family, a full
  Finnish-newspaper content pipeline, and a DM concierge.
- **aimeat-agency** (`aimeat-agency/`) is a Tauri desktop appliance for non-developers: a guided
  wizard (account → AI brain → first agent → approve → run) over the local Python cockpit
  (`crewaimeat.agency.cockpit`).
- Platform: fleet scripts come as `.ps1` (Windows) and `.sh` (macOS/Linux). Use the one matching the
  user's OS. (`start_host.ps1` is Windows-only; its cross-platform equivalent is
  `uv run python -m crewaimeat.fleet_host`.)
- Protocol readers: the AIMEAT spec is **v4.0, two-layer** — `docs/AIMEAT-RFC-v4.0-Core-full.md` +
  `docs/AIMEAT-RFC-v4.0-Platform-full.md` in the aimeat-protocol repo. When anything here disagrees
  with the node, **the node schema is canonical**.

---

## Step 0 — Determine the target (ASK the user; do not guess)

1. **Which AIMEAT node?** `https://aimeat.io` (hosted) · a **local node** the user runs from the
   aimeat-protocol repo (default `http://localhost:40050`) · or another node's URL. Call it `<NODE_URL>`.
   If they want a local node and don't have one, point them at aimeat-protocol's own
   `startup.prompt.md` first — this repo does not run a node.
2. **Owner handle** — the account agents register under (create it at `<NODE_URL>/v1/portal` if
   missing). Call it `<OWNER>`.
3. **Model access** — at least one of: an **OpenRouter** key (`https://openrouter.ai/keys`;
   `openrouter/owl-alpha` is free), an **NVIDIA NIM** key (`https://build.nvidia.com` — free
   frontier-class models, OpenAI-compatible), an **xAI** key, or a **local Ollama** (keyless).
4. *(Optional)* **How they want to run:** the whole **fleet** (default), a **single crew** (dev loop),
   or the **aimeat-agency** desktop app (non-developer path — its wizard replaces Steps 3–5).

Confirm 1–3 before proceeding. Never invent a node URL, owner, or key.

## Step 1 — Prerequisites (check; offer to install what's missing)

- **Python 3.10–3.13**, **uv** (https://docs.astral.sh/uv/), **Node.js** (for the `npx aimeat` CLI).
- Verify: `uv --version`, `node --version`. This is a **uv** project — the `.venv` has no pip; always
  `uv run` / `uv sync`, never plain `pip`.

## Step 2 — Install

```
uv sync
```

This installs `crewai`, the **`aimeat-crewai`** connector, and everything else into `.venv`.
Optional extras: `uv sync --extra tui` (the fleet TUI) · `uv sync --extra agency` (the cockpit server).

## Step 3 — Configure (never commit or echo secret values)

**Keys** — copy `.env.example` to `.env` and fill in what the user has (only write secrets into `.env`;
it is gitignored):

```
OPENROUTER_API_KEY=...     # and/or NVIDIA_KEY=..., XAI_API_KEY=... (USE_XAI=1)
AIMEAT_OWNER=<OWNER>       # lets crew-forge register the agents it builds
```

**LLM routing (recommended)** — copy `llm_providers.example.json` to `llm_providers.json` (gitignored).
It defines named **profiles** (provider→model fallback chains) and maps each crew to one — content crews
to a prose model, code/app crews to a real coder; a provider whose key is missing is skipped. When the
file exists it overrides `OPENROUTER_MODEL`. Check a model can actually drive the scaffold with
`uv run python scripts/check_models.py --quick`.

## Step 4 — Register + approve agents (device authorization, RFC 8628)

Every AIMEAT agent is **registered once** and **approved by the owner**. Register each crew the user
wants live (start with one, or with `crew-forge`):

```
npx aimeat@latest connect add --agent <name> --mode task-runner --url <NODE_URL> --owner <OWNER>
```

The command prints a **verification code + URL**. Tell the user to open the URL (also reachable from
their profile → Agents tab on the node), enter the code, and **approve**; the command finishes once
approved and the token lands in this repo's `.aimeat/` home (gitignored — never read it out loud).

- **Modes** (the five AIMEAT agent modes): `autonomous` · `interactive` · `coordinator` ·
  `task-runner` · `workstation`. Crews here are **task-runner** — their tasks are born active and run
  unattended, and the mode also picks the onboarding flow (next step).
- **Whole fleet against one node** in one go:
  `uv run python scripts/register_fleet.py --owner <OWNER> --url <NODE_URL>` (prints one approval
  code per crew; `--agents a,b,c` for a subset).

## Step 5 — Start agents

```
./scripts/start_fleet.ps1        # Windows
./scripts/start_fleet.sh         # macOS/Linux
```

`start_fleet` runs `uv sync`, ensures the **one shared serve daemon** (the loopback tunnel to the node,
plus a supervisor that restarts it), then runs the **fleet host**: every registered+approved crew as a
thread in ONE process (crewai imported once; ~20× less RAM than per-process). It stays in that terminal;
Ctrl+C stops the whole fleet. Only approved agents come online — an unapproved one waits and joins by
itself once approved. Alternatives:

- **One crew, foreground (dev loop):** `uv run python crews/<name>_crew.py`
- **A subset in the host:** `./scripts/start_host.ps1 -Agents a,b` (or `-List` to preview); other OSes:
  `uv run python -m crewaimeat.fleet_host`
- **Desktop app:** `cd aimeat-agency && pnpm install && pnpm tauri dev` — or run the cockpit directly:
  `uv run --extra agency python -m crewaimeat.agency.cockpit` and open the printed local URL. Its wizard
  handles account, model, registration, and approval on its own.
- `./scripts/view_fleet.*` — read-only status · `./scripts/terminate_fleet.*` — stop everything
  (**confirm with the user first**) · `./scripts/install-autostart.ps1` — start on every boot (Windows).

**First connect = Hello Integration.** On its first attach each agent runs AIMEAT's onboarding
handshake. The full flow is **16 steps** (12 required + 4 optional); a **task-runner** runs a
**7-step** flow and **keeps its test-task pair as the capability proof** (`workstation` gets 4 steps).
The scaffold drives it deterministically from the node's own step guide — nothing for the user to do.
Check progress with the node's `aimeat_onboarding_status`, not the dashboard step counter.

## Step 6 — Drive it / what the user can do now

- **Queue a task** from the node dashboard (the agent's **Tasks** tab → *+ New Task*) and watch the
  live progress stream (a deterministic heartbeat, no LLM).
- **Watch and drive the fleet from a terminal:** `uv run crewaimeat-tui` (after `uv sync --extra tui`) —
  status, per-agent test runs, model picker, logs.
- **Build a new crew without coding:** queue **crew-forge** a task `/build <description>` — it designs,
  writes + validates `build_domain`, registers, and launches the new agent; the user approves it once.
- **Scaffold a crew by hand:** `uv run crewaimeat new-crew <name>`, then edit only its `build_domain`
  (or paste `CREW_AUTHORING_PROMPT.md` into an assistant). Give it a real identity in
  `src/crewaimeat/fleet_identity.py` and an `offers.py` entry.
- **Give crews expertise:** `skills/<name>/SKILL.md` packs, loaded via `CrewSpec(skills=[...])`
  (see `skills/README.md`); owner-linked registry skills attach automatically.

## Essentials to teach the user (working with AIMEAT)

- **Liaison + daemon.** One in-crew agent (from `aimeat-crewai`) owns all AIMEAT coordination: it
  onboards, picks up tasks, publishes the result to memory, marks the task done. Domain agents never
  touch AIMEAT.
- **Approvals are owner-gated and one-time.** Device code → owner approves → token stored under
  `.aimeat/`. A crew launched before approval waits patiently and comes online alone.
- **task-runner + auto-activation.** Tasks created for a task-runner agent are born active and run
  unattended; the **last task's output** is published to memory (`crews.<agent>...`) as the deliverable.
- **LLM routing is per-crew.** `llm_providers.json` profiles + fallback chains; per-agent pins via the
  TUI model picker. Restart the fleet after changing routing or an agent's identity.
- **One serve daemon per checkout.** `AIMEAT_HOME` is pinned to `<repo>/.aimeat` by every entrypoint,
  so all processes share one `serve.json` and never collide with another checkout's fleet.
- **Two messaging channels.** Dashboard/owner chat (`aimeat_message_*`, private) vs the **federated DM
  inbox** (`aimeat_dm_*`) — replies to a requester are consented; a crew never cold-DMs a new contact
  without owner approval.

## Guardrails for you, the assistant

- **Never** commit, log, or echo secret values (agent tokens, AI keys, SMTP/DB credentials) — only
  write them into `.env` / `.aimeat/`, both gitignored.
- **Confirm before destructive or outward-facing ops:** `terminate_fleet`, deleting agents/apps/memory,
  sending DMs/email, pushing to git, publishing anything.
- **Surface device codes** for the user to approve; **never invent** an owner, node URL, or key — ask.
- **Keep every prompt string in English** (crew prompts, READMEs, offers) — output language follows the
  task, but the source stays English.
- **The node schema is canonical** for anything protocol-shaped (onboarding steps, offer/workflow
  schemas, MCP tool names) — when this repo's docs and the node disagree, trust the node.
- Prefer the repo's own scripts and `uv`; don't hand-roll process management or bypass device auth.

## Do it now

Ask Step 0's questions, then work Steps 1→5 (run the commands, surface each approval code and wait).
When agents are up, run `view_fleet` (or the TUI), summarize what's running, and suggest 2–3 next
actions (queue a task, `/build` a crew with crew-forge, or open the agency cockpit).

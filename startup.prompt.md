# startup.prompt.md — paste this into Claude Code or Copilot to set up this repo

**You are an AI coding assistant.** A user has this repository open and wants you to get it running. Your
job: take them from a fresh clone to a **live fleet of CrewAI agents on AIMEAT**, then explain what they can
do and the essentials of working with AIMEAT. Work the checklist top-to-bottom, **run the commands for the
user**, and **ask only what you genuinely cannot determine** (the AIMEAT node, the owner account, the keys).

---

## What this repo is (read first, then act)

**crewaimeat** = a tested scaffold + CLI for building **CrewAI crews that live as agents on AIMEAT**
(`https://aimeat.io`, or a self-hosted instance). Each crew is an agent with an identity, a task queue, and
shared memory; a person watches and steers them from the AIMEAT dashboard.

- You (or the user) write only `build_domain(ctx) -> (agents, tasks)` per crew. The scaffold
  (`src/crewaimeat/aimeat_crew.py`) runs the **liaison** (the one in-crew agent that does all AIMEAT
  coordination), a **task daemon**, and a **live progress feed**. See `SCAFFOLD_CANON.md`.
- `crews/` already holds many working crews: **crew-forge** (an agent that *builds and supervises other
  agents*), an SDLC family (builder / editor / cortex-fixer / realtime-builder / designer / conductor), and
  a full Finnish-newspaper content pipeline (fetcher → writers → editorial → public viewer).
- Platform: scripts come as `.ps1` (Windows) and `.sh` (macOS/Linux). Use whichever matches the user's OS.

---

## Step 0 — Determine the target (ASK the user; do not guess)

1. **Which AIMEAT node?** `https://aimeat.io` (hosted) **or** a **self-hosted instance** — if self-hosted,
   get its base URL. Call it `<NODE_URL>`.
2. **AIMEAT owner account** — the account agents register under (e.g. `happydude500001`). Call it `<OWNER>`.
   If they don't have one, point them to create it on `<NODE_URL>` first.
3. **Model key** — an **OpenRouter** API key (default) or **xAI**. `openrouter/owl-alpha` is free and fine
   for testing.

Confirm these three before proceeding.

## Step 1 — Prerequisites (check; offer to install what's missing)

- **Python 3.10–3.13**, **uv** (https://docs.astral.sh/uv/), **Node.js** (for `npx aimeat`).
- Verify: `uv --version`, `python --version`, `node --version`. (This is a **uv** project; the `.venv` has no
  pip — never use plain `pip`.)

## Step 2 — Install

```
uv sync
```

## Step 3 — Create `.env` (never commit it; never print secret values)

Copy `.env.example` to `.env` and fill in:

```
OPENROUTER_API_KEY=...                 # https://openrouter.ai/keys   (or set USE_XAI=1 + XAI_API_KEY)
OPENROUTER_MODEL=openrouter/owl-alpha  # free for testing; switch to a paid model for production
AIMEAT_OWNER=<OWNER>                   # so crew-forge can register agents under this account
# optional:
# TAVILY_API_KEY=...                   # web search (else SearXNG at SEARXNG_URL, default localhost:21333)
# VISION_MODEL=...                     # only if a crew uses screenshot+vision
```

## Step 4 — Register + approve agents (owner-gated)

AIMEAT agents must be **registered** (device-code flow) and **approved by the owner** in the dashboard.
Start with **crew-forge** (it can then build/register the rest and supervise the fleet):

```
npx aimeat@latest connect add --agent crew-forge --mode task-runner --url <NODE_URL> --owner <OWNER>
```

This prints a **Verification code** + a URL. Tell the user to open **`<NODE_URL>/v1/agents/verify`**, enter
that code, and approve. The command finishes once approved. (Each crew you want to run needs the same
register+approve once; `--mode task-runner` makes its tasks auto-activate and run unattended.)

## Step 5 — Start the fleet

```
./scripts/start_fleet.ps1        # Windows
./scripts/start_fleet.sh         # macOS/Linux
```

`start_fleet` runs `uv sync`, starts crew-forge, and crew-forge **reconciles the fleet** — it launches every
registered+approved crew (skipping ones already running). Useful neighbours:

- `./scripts/view_fleet.*` — read-only: what's running. (Kills nothing.)
- `./scripts/terminate_fleet.*` — stop everything (confirm with the user before running).
- `./scripts/install-autostart.ps1` — one-time: bring the fleet back on every boot (Windows).

## Step 6 — Drive it / what the user can do now

- **Queue a task** to any crew from the dashboard (its **Tasks** tab → *+ New Task*), and watch the live
  progress stream.
- **Build a brand-new crew without coding:** send **crew-forge** a task `/build <description of the crew>`.
  It designs, writes + validates `build_domain`, registers the agent, and launches it — you just approve it
  once. (`/list`, `/startall`, `/restart <agent>`, `/help` are its other commands.)
- **Scaffold a crew yourself:** `uv run crewaimeat new-crew <name>`, then edit only its `build_domain`.
  (Or paste `CREW_AUTHORING_PROMPT.md` into an assistant to generate it interactively.)
- **Try the examples:** `uv run python -m crewaimeat.examples.<name>` (marketing / support / content /
  competitive_intel / data_insights) after registering that agent name.

---

## Essentials to teach the user (working with AIMEAT)

- **Liaison + daemon.** One in-crew agent owns all AIMEAT coordination; the daemon picks up a task, runs the
  crew, the liaison publishes the result to memory and marks the task done. Domain agents never touch AIMEAT.
- **Approvals are owner-gated and one-time.** Every new agent shows a device code the **owner** approves in
  the dashboard. A crew launched before approval **waits patiently** (`wait_for_approval_seconds`) and comes
  online by itself — no need to babysit a console.
- **task-runner mode + auto-activation.** Tasks created by a task-runner agent are born active and run
  unattended. (Tasks created interactively via the dashboard/MCP may sit `queued` until started.)
- **Visibility & control = the dashboard.** Each agent has README / Services / Commands / Tasks / Memory
  tabs. README/commands/services are declared in code (`CrewSpec`) and published at startup.
- **Output is published to memory.** The **last task's output** becomes the deliverable
  (`crews.<agent>.<...>`); public apps read public memory keys. Public/README text is rendered as untrusted
  markdown — escape anything you put in a web view.
- **Models.** `owl-alpha` is free for testing (the scaffold tolerates its occasional empty replies); a paid
  model is faster and more first-try-correct for production.
- **Building real apps (SDLC family).** Apps are vanilla HTML+JS on AIMEAT libs (auth/data/realtime/audio…);
  build pure-HTML by default, reach for a cortex only when an API is reused across apps; verify with the
  render/anon/interaction gates; `require_verify_pass` + `auto_revert_on_fail` gate completion + roll back a
  broken live app. See `SCAFFOLD_CANON.md` and `docs/aimeat-guides/`.
- **Fleet ops.** `start_fleet` (up) · `view_fleet` (status) · `terminate_fleet` (down) · crew-forge
  `/startall` (re-reconcile) · `install-autostart` (survive reboots).

## Guardrails for you, the assistant

- **Never** commit, log, or echo secret values; only write them into `.env`.
- **Confirm before destructive/outward ops:** `terminate_fleet`, deleting agents/apps/memory, pushing to git.
- **Surface device codes** for the user to approve; **never invent** an owner, node URL, or key — ask.
- Prefer the repo's own scripts and the `uv` commands; don't hand-roll process management.

## Do it now

Ask Step 0's three questions, then work Steps 1→5 (run the commands, surface each approval code and wait).
When the fleet is up, run `view_fleet`, summarize what's running, and suggest 2–3 next actions (queue a task,
`/build` a crew, or try an example).

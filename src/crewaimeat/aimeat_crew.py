"""Reusable AIMEAT crew scaffold — the validated, pitfall-covered base.

Reuse the AIMEAT wiring as-is: define only your DOMAIN agents + tasks (a
`build_domain` function) and hand them to `run_crew(CrewSpec(...))`. This module
provides everything that was hard to get right, verified end-to-end against
https://aimeat.io:

- deterministic onboarding gate + one-shot Hello Integration (no LLM in the gate)
- run_crew_daemon wiring with the right LLM (two-phase: propose on queued /
  execute on active)
- the liaison `finalize` task: publish to AIMEAT memory + mark todos done ONE AT
  A TIME with read-after-write verify + complete the task
- the live progress bridge (crewaimeat.progress): milestones -> aimeat_task_event,
  5s live status -> memory key agents.<agent>.tasks.<id>.live
- current-date injection so the crew never hallucinates "today"

Why reuse it: each item above was a real failure we diagnosed and fixed (tool-call
races losing todo writes, OpenRouter empty-choices crashes, date hallucination,
onboarding cache loops). Reusing the scaffold keeps them fixed.

Minimal usage:

    from crewai import Agent, Task
    from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew

    def build_domain(ctx: BuildContext) -> tuple[list, list]:
        worker = Agent(role="Worker", goal="...", backstory="...", llm=ctx.llm)
        task = Task(description=f"{ctx.today}\\n\\n{ctx.prompt}", agent=worker,
                    expected_output="...")
        return [worker], [task]  # (agents, tasks) — last task's output is published

    def run():
        run_crew(CrewSpec(agent_name="my-crew", build_domain=build_domain))
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

for _s in (sys.stdout, sys.stderr):
    _r = getattr(_s, "reconfigure", None)
    if _r:
        _r(encoding="utf-8")

import requests  # noqa: E402 — ships with aimeat-crewai
from aimeat_crewai import (  # noqa: E402
    OnboardingError,
    create_liaison_agent,
    ensure_serve,
    run_crew_daemon,
    run_hello_integration,
    serve_params,
    stdio_params,
)
from aimeat_crewai.daemon import DAEMON_DEFAULT_TOOL_FILTER  # noqa: E402
from aimeat_crewai.mcp_client import AimeatServeError  # noqa: E402 — raised when no live serve daemon
from crewai import Agent, Crew, Process, Task  # noqa: E402

try:  # private helper; degrade gracefully if a future version moves it
    from aimeat_crewai.daemon import _read_token as _aimeat_read_token  # noqa: E402
except Exception:  # pragma: no cover
    _aimeat_read_token = None

from crewaimeat.llm import get_llm  # noqa: E402
from crewaimeat.progress import install_progress  # noqa: E402

# run_crew() exits with this code when the agent's token is no longer accepted by the
# node (needs re-approval). The watchdog scripts treat it as "stop, don't restart".
AUTH_EXIT_CODE = 78


# Lock-file handles for the single-instance guard, kept alive for the whole process lifetime
# (the OS advisory lock is held only while the handle is open).
_SINGLE_INSTANCE_HANDLES: list = []


def _acquire_single_instance(agent_name: str) -> bool:
    """Best-effort name-based single-instance lock so only ONE daemon runs per agent.

    Returns True if this process is the sole daemon for `agent_name`, False if another live
    daemon already holds the lock. The lock is an OS advisory lock on logs/.locks/<agent>.lock
    held for the whole process lifetime; it releases automatically when the process dies, so a
    crash never leaves a stale lock. If locking is unavailable it returns True (never blocks a
    legitimate start). This catches duplicates launched any way (uv run, .\\crews\\, a stray
    watchdog, an orphaned daemon) — unlike a command-line scan, which a differing path string
    defeats.
    """
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", agent_name)
    lock_dir = Path.cwd() / "logs" / ".locks"
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
        fh = open(lock_dir / f"{safe}.lock", "a+")
    except OSError:
        return True  # cannot create the lock file -> do not block startup
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return False  # another daemon holds the lock
    _SINGLE_INSTANCE_HANDLES.append(fh)  # keep it open (and locked) for the process lifetime
    return True


# --------------------------------------------------------------------------- #
# Public API: what a crew author fills in
# --------------------------------------------------------------------------- #
@dataclass
class BuildContext:
    """Passed to your build_domain(ctx). Everything you need to define agents/tasks."""

    task: dict  # the raw AIMEAT task (id, title, description, ...)
    prompt: str  # task.description or task.title — the user's actual request
    llm: Any  # the shared LLM (crewaimeat.llm.get_llm); pass to your Agents
    today: str  # current-time context string — prepend to time-sensitive tasks
    directives: str = ""  # owner-set behavioral directives (GET /v1/agents/me/directives),
    #   already formatted. The scaffold also prepends these to every domain task, so they bind
    #   behavior automatically; reference ctx.directives only if you want finer placement.
    offer: dict | None = None  # when the task was ordered from this agent's Offers surface
    #   (scope.kind == 'offer'), the RESOLVED offer descriptor. ctx.prompt stays the user's RAW
    #   request — never the offer's own ask/example text (re-feeding it made agents treat their
    #   boilerplate as the request). Use ctx.offer only to pick a mode/command for the work.


# build_domain returns (agents, tasks). Tasks run in `process` order; the LAST
# task's output is what the liaison publishes to AIMEAT memory.
BuildDomain = Callable[[BuildContext], "tuple[list[Agent], list[Task]]"]


@dataclass
class CrewSpec:
    """Declares one AIMEAT-connected crew. Only `agent_name` + `build_domain` are required."""

    agent_name: str  # the AIMEAT agent identity (from `aimeat connect add`)
    build_domain: BuildDomain  # returns (domain_agents, domain_tasks)
    process: Any = Process.sequential  # sequential is the validated path; hierarchical is advanced
    poll_seconds: int = 30  # daemon poll interval
    max_concurrent_tasks: int | None = None  # how many EXECUTE tasks this ONE daemon runs at once.
    #   None (default) = read the owner-set value from AIMEAT (Tasks tab -> "max concurrent", needs
    #   AIMEAT >= 1.16.2 + aimeat-crewai >= 0.3.8); 1 = serial (unchanged); >1 = a bounded thread pool
    #   with a SEPARATE liaison+MCP per task (a shared stdio MCP can't run parallel kickoffs). Best for
    #   I/O-bound crews (mostly waiting on the LLM). PROPOSE + inbox stay serial on the shared liaison.
    memory_key_prefix: str | None = None  # default: crews.<agent_name>
    manager_agent: Any = None  # only for Process.hierarchical
    memory: bool = False  # opt-in CrewAI crew memory (remember across runs). OFF by default so crews stay
    #   stateless. When True the per-task Crew gets an embedder-backed, scoped Memory so it recalls prior
    #   runs, with its analysis LLM routed through this crew's own get_llm chain (NOT the OpenAI default).
    #   PREREQUISITE: a reachable embedder — local ollama with an embedding model pulled (free/private;
    #   `ollama pull nomic-embed-text`), else NVIDIA_API_KEY (free cloud) or DASHSCOPE_API_KEY (qwen, paid).
    #   If memory is ON and NONE is reachable the crew FAILS LOUD at build (no silent stateless fallback).
    #   Storage is scoped per owner/agent/principal under AIMEAT_HOME so crews never cross-read. See
    #   crewaimeat.embedder_cascade. Spreads like `verify` — opt-in per crew.
    memory_embedder: dict | None = None  # explicit CrewAI embedder config ({provider, config}) that
    #   BYPASSES the ollama->nvidia->qwen cascade. None -> the cascade probes and picks the first reachable
    #   tier. Ignored when memory is False.
    memory_scope: str = "principal"  # storage isolation when memory is ON: "principal" (default: a
    #   SEPARATE memory per caller — a DM sender's ghii, a delegating agent, else the owner — so a crew never
    #   recalls the wrong caller's data), "agent" (ONE shared brain across all the owner's callers, for a
    #   deliberate knowledge accumulator), or "session" (ephemeral, resets per task).
    embedder_bias: str | None = None  # cost-vs-privacy bias for the embedder cascade: "privacy" (default:
    #   local ollama first, then paid-private qwen; the free-but-cloud nvidia tier is DROPPED) or "cost"
    #   (promote the FREE nvidia tier ahead of paid qwen). None -> the EMBEDDER_BIAS env default ("privacy").
    owner: str | None = None  # AIMEAT owner; set only if the agent name is ambiguous
    max_idle_auth_failures: int = 10  # idle cycles with a rejected token before exiting for re-auth
    idle_hook: Any = None  # optional DETERMINISTIC callable run on idle cycles (throttled to
    #   idle_hook_seconds) while the token is alive — e.g. a workspace-contract poll. The call itself uses
    #   NO LLM (any LLM is in the work it triggers, only when there's something to do); exceptions are
    #   logged, never fatal.
    idle_hook_seconds: int = 60  # minimum seconds between idle_hook runs
    record_spaces: Any = None  # for listen_for=("records",): the (organism_id, ws, space) tuples this agent
    #   subscribes to for workspace-record PUSH events (aimeat-crewai>=0.7.0, event-driven instead of
    #   polling). A list of {"organism_id","ws","space"} (space = the NAMESPACE key segment, e.g.
    #   "shared.moodboard_requests"), OR a 0-arg callable resolved at daemon start — e.g.
    #   contract_record_spaces(AGENT, CONTRACT), which discovers member workspaces × the contract's
    #   record/input namespaces. Replaces a record-scanning idle_hook.
    on_record: Any = None  # handler for a pushed record event {type,organism_id,ws,space,id,op,ts}. op is
    #   "created"|"updated" for a write, or "catchup" (id=None) once per space on (re)connect — re-scan that
    #   space, then go event-only. The event is a WAKE + coordinates (no record value); the handler does its
    #   own authorized read. If omitted, each event is wrapped into a synthetic task routed to build_domain.
    on_dm: Any = None  # with "dms" in listen_for (aimeat-crewai>=0.8.0, node>=1.30.2): handler for a pushed
    #   federated-inbox DM wake {id, conversationId, subject, senderGhii, preview, attachments, createdAt}.
    #   The daemon parks its idle wait on /local/dm/next (event-based, idle-quiet) and calls on_dm(event); a
    #   lightweight wake -> read the full body via dm.dm_thread(conversationId), hand back via dm.dm_reply.
    #   Use dm.handle_dm_event(agent, event, responder, seen=...). If omitted, the wake becomes a synthetic task.
    dm_serviceable: bool = False  # turn this crew into a DM-callable SERVICE: when True (and on_dm is unset),
    #   "dms" is added to listen_for and a generic on_dm runs the crew's own build_domain on the DM body and
    #   REPLIES in-thread with the result. So an agent on the federation can DM "research X" / "make a logo"
    #   and get the deliverable back — the substrate for agent-to-agent delegation (the orchestrator routes
    #   to these). The first-contact gate still applies (it only replies inside a thread).
    listen_for: Iterable[str] = ("tasks",)  # + "messages" (owner inbox), "records" (workspace), "dms" (federated)
    wait_for_approval_seconds: int | None = 1800  # wait this long (30 min) for the token to be approved
    #   before onboarding, then exit for re-auth (None = wait indefinitely)
    services: list[dict] | None = None  # {name, description} capabilities to declare at
    #   onboarding via aimeat_onboarding_declare_services
    discover: bool = False  # give this crew's liaison the MASTER DIRECTORY tool `aimeat_discover` (aimeat-
    #   crewai>=0.10.0, node/connector>=1.32) by ADDING it to the daemon tool_filter (the default 24-tool
    #   allowlist omits it). One faceted query across every node domain (capabilities/workflows/knowledge/
    #   decisions/research/material/companies/docs/apps/memory) -> the agent can ask "what already exists
    #   here I can reuse?" before acting. The 0.10.0 liaison backstory already steers it to call discover
    #   FIRST, so no extra prompt is needed — this flag just makes the tool LOADABLE. Turn ON for
    #   researchers / planners / coordinators / delegators / a head agent; leave OFF for single-purpose
    #   executors (they don't need to survey the node before doing their one job).
    commands: list[dict] | None = None  # slash-command palette [{name, description, category}, ...]
    #   published to memory key agents.<agent>.commands (owner) so the Messages UI surfaces it
    chat_commands: list[dict] | Callable[[str], list[dict]] | None = None  # PUBLIC chat-command palette the
    #   peer inbox composer renders as fill-in CHIPS. [{id, label, description, template, params:[{name,
    #   type∈text|number|select, required, placeholder, default, options}]}]; `template` uses {{param}}
    #   placeholders. Published to the PUBLIC key "chat.commands" (a peer reads it via
    #   aimeat_memory_read_public(<gaii>, "chat.commands")) on EVERY start. Template-as-prose: clicking a
    #   chip fills the template and drops the resulting PROSE into the composer — the human sends it, the
    #   agent receives text it already advertised, and its LLM interprets the filled params (no rigid
    #   parse). May be a CALLABLE(agent_name)->list so an agent can GENERATE commands from LIVE state (e.g.
    #   one "Ask <specialist>" command per live agent it can delegate to — the menu reflects who's up now).
    mode: str | None = None  # AIMEAT agent MODE, set on every start via aimeat_agent_mode_set (self). One
    #   of autonomous|interactive|task-runner|coordinator|workstation. None -> DERIVED: dm_serviceable /
    #   self_monitor crews keep the interactive message surface; every other crewaimeat crew is a
    #   "task-runner". WHY it matters: the node defaults a device-authed agent with no mode to 'interactive',
    #   which gates every created task behind a manual 'Start this task'. task-runner mode makes the node
    #   AUTO-ACTIVATE tasks on create (test runs + real work just run) and serves the shorter 7-step onboarding.
    tags: list[str] | None = None  # capability TAGS set on the agent via aimeat_agent_tags_set
    #   on every start (idempotent, so they survive re-onboarding). The ecosystem-app agent picker
    #   matches on these (+ capabilities/domain), so e.g. tags=["feedback-analysis"] makes the agent the
    #   RECOMMENDED pick for a recipe by TAG, not only by exact name. Charset: lowercase alnum + . _ -
    #   (no ':' or '@' — versioned ids like consumes:feedback-stats@1 belong in `capabilities`).
    capabilities: dict | None = None  # {technical:[{name,type}], domain:[...], languages:[...]}
    #   self-reported via aimeat_agent_capabilities_report on every start (OVERWRITES the set). Advertise
    #   SPECIFIC capabilities (what this agent actually does — e.g. domain "consumes:feedback-stats@1")
    #   over the liaison's generic onboarding defaults; the picker's matcher reads technical + domain.
    offer: dict | None = None  # an inline crew_offer-shape offer META (id/title/ask/example/cost/latency/
    #   repeatability/verification/consequences[/sample]) this agent ADVERTISES so others can discover +
    #   request its value. Published on every start via publish_meta_offer — for a forged crew this is how
    #   it advertises WITHOUT a central offers.py entry. None = advertise nothing. Mirrors brain_templates
    #   Template.offer.
    temperature: float | None = None  # ENFORCE a fixed LLM temperature for this crew, regardless of
    #   the .env LLM_TEMPERATURE default and without per-task classification. Use it for single-purpose
    #   crews whose nature is fixed: a creative service (jokes, jingles, taglines) should declare
    #   temperature=0.7 once instead of relying on adapt_to_task to re-discover "this is creative" every
    #   task. Takes precedence over adapt_to_task for the temperature knob (adapt_to_task still selects
    #   verify mode if both are set). None = fall back to adapt_to_task, else the .env default.
    adapt_to_task: bool = False  # when True, classify each task (fact/creative/mixed) and
    #   adapt: temperature (cool for fact ~0.15, warm for creative ~0.7), inject a grounding rule for
    #   factual work, and pick the verify mode (factcheck for fact, off for creative). Spreads like verify.
    score_to_stats: bool = False  # when True (with verify="factcheck"), the Reviewer's
    #   faithfulness score is parsed and written to agents.stats.<agent>.review.<task>.verify (the
    #   reputation convention). Source-grounded judging — validated by POC v2. Spreads like verify.
    self_monitor: bool = False  # when True, after each task the crew reads its OWN reputation
    #   rollup and, if a gated signal fires (WEAK avg<2.5, or a bimodal SPLIT) with enough data
    #   (n>=10 — the n=3 lesson) and not recently proposed, sends the owner a clickable "explore an
    #   evolution?" prompt (metadata.prompt). Notice+propose only; building/A/B/promote are human-gated
    #   /evolve steps (doc 20). Spreads like verify — opt-in per crew.
    contribute_to_library: bool = False  # when True, after each task the deliverable is classified
    #   (topic + shelf-life, junk dropped) and a compact pointer-entry is appended to
    #   agents.<agent>.library for the librarian to index. Spreads like `verify` — opt-in per crew.
    verify: str = "off"  # "on" appends a Reviewer pass that checks the deliverable
    #   against the goal and FIXES gaps before publish (one pass, no loop). Per-task <<VERIFY>> /
    #   <<NOVERIFY>> in the task description overrides this. "feeling lucky" (off) vs "serious" (on).
    require_verify_pass: bool = False  # SYS-1: gate task COMPLETION on the app verify gates'
    #   DETERMINISTIC outcome (verify_render/verify_anon_render/verify_interaction, via author_tool's
    #   recorded {ok} — not the agent's self-report). When True, a direct (non-conductor) build that
    #   FAILED a gate, or never ran one, is FAILED (aimeat_task_fail) instead of completing 'green'. This
    #   only changes the TASK STATUS — it never touches the live app. Off by default; opt-in for build/fix
    #   crews that run the app gates. (The conductor already withholds completion until green.)
    auto_revert_on_fail: bool = False  # When True AND require_verify_pass fails a build, ALSO restore
    #   each app this run published to its pre-run last-good version (revert_apps_to_baseline) — so the
    #   LIVE app is rolled back, not just left un-'done'. This re-publishes the previous version (an
    #   outward-facing change), so it is a SEPARATE opt-in from the (safe, status-only) gate above; off by
    #   default so the gate can be watched before live rollback is enabled.
    readme_md: str | None = None  # markdown for the agent's README tab; may contain
    #   [[FIGLET[:font]]["text"]] (deterministic ASCII-art) and [[LLM]["prompt"]] (LLM output)
    #   directives expanded at publish time. Written to agents.<agent>.readme (owner).
    #   Expanded only when the text changes (cached).
    clean_deliverable: Callable[[str], str] | None = None  # optional deterministic post-processor run
    #   on the final deliverable TEXT just before it is published (primary + shared writes). Use it to
    #   strip model scaffolding the prompt couldn't fully suppress (e.g. an editor leaking its KEPT/CUT
    #   notes). Deterministic = enforced in code, not left to the model. Must never raise; on any error
    #   the original text is published unchanged.


# --------------------------------------------------------------------------- #
# Built-in machinery — the scaffold provides this; your crew reuses it
# --------------------------------------------------------------------------- #
def _now_context() -> str:
    """Deterministic current-time context (no LLM). Without it the model
    hallucinates the date and cannot anchor time-related questions.

    UTC is always the baseline. Europe/Helsinki is best-effort: if zoneinfo's
    tz database is missing (Windows without `tzdata`) it degrades to UTC only.
    """
    now_utc = datetime.now(timezone.utc)
    local_part = ""
    try:
        now_local = now_utc.astimezone(ZoneInfo(os.getenv("AIMEAT_CREW_TZ", "Europe/Helsinki")))
        local_part = f" = {now_local:%Y-%m-%d %H:%M} {now_local.tzname()} ({now_local:%A})"
    except Exception:  # noqa: BLE001 — tzdata missing etc.; UTC is enough as reference
        pass
    return (
        f"CURRENT TIME (reference for anything time/date related): "
        f"{now_utc:%Y-%m-%d %H:%M} UTC{local_part}. Treat THIS as the single source "
        f"of truth for 'today'/'now' references. "
        f"Verify up-to-date facts with web search."
    )


def _runtime_max_execution_time() -> int | None:
    """Optional fleet-wide wall-clock bound (seconds) for each agent's task, from
    AIMEAT_AGENT_MAX_EXECUTION_TIME. A wall-clock bound stops a STUCK run while letting a
    progressing-but-long build finish — which a raw max_iter cap cannot distinguish (field finding
    2026-06-05: max_iter only fires on non-convergent re-authoring loops, so it cannot tell thrashing
    from legitimate build depth). Default None (off) so it changes nothing until the operator opts in;
    a per-agent value an author set themselves is never overridden."""
    raw = os.getenv("AIMEAT_AGENT_MAX_EXECUTION_TIME")
    if not raw:
        return None
    try:
        v = int(raw)
    except ValueError:
        return None
    return v if v > 0 else None


def _auth_alive(agent_name: str, owner: str | None) -> bool | None:
    """Probe whether the agent's stored token still authenticates with the node.

    Returns True (accepted), False (rejected 401/403 -> needs re-approval), or None
    (unknown / transient network or 5xx -> do not act on it). Light: one GET, reusing
    the connector's stored token (no subprocess, no LLM)."""
    if _aimeat_read_token is None:
        return None
    try:
        token, node_url = _aimeat_read_token(agent_name, owner=owner)
        r = requests.get(
            f"{node_url.rstrip('/')}/v1/agents/{agent_name}/tasks",
            headers={"Authorization": f"Bearer {token}"},
            params={"status": "active"},
            timeout=15,
        )
        if r.status_code in (401, 403):
            return False
        if r.status_code == 200:
            return True
        return None
    except Exception:  # noqa: BLE001 — transient; treat as unknown
        return None


def _fetch_directives(agent_name: str, owner: str | None) -> dict | None:
    """Read the agent's owner-set directives via GET /v1/agents/me/directives (its own token).

    `me` resolves to the calling agent from its JWT. Returns the inner data payload
    {purpose, rules[], memory_areas, shared_tags, shared_memory_prefixes, resources} or None.
    rules are merged system -> owner -> agent, each tagged with its source. This is the canonical
    directives contract (also onboarding STEP 1). Best-effort: any failure returns None so a crew
    still runs without directives."""
    if _aimeat_read_token is None:
        return None
    try:
        token, node_url = _aimeat_read_token(agent_name, owner=owner)
        r = requests.get(
            f"{node_url.rstrip('/')}/v1/agents/me/directives",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if r.status_code != 200:
            return None
        body = r.json()
        return body.get("data") if isinstance(body, dict) else None
    except Exception:  # noqa: BLE001 — transient / offline; run without directives
        return None


def _format_directives(data: dict | None) -> str:
    """Render purpose + rules into a behavioral-constraints block for the crew's prompt.

    system/owner rules are binding policy; agent rules are the agent's own standing notes. All are
    framed as directives to follow. Returns "" when there is nothing to apply."""
    if not isinstance(data, dict):
        return ""
    purpose = (data.get("purpose") or "").strip()
    rules = [r for r in (data.get("rules") or []) if isinstance(r, dict) and (r.get("description") or "").strip()]
    if not purpose and not rules:
        return ""
    lines = [
        "STANDING DIRECTIVES (owner-set policy — follow these in everything you produce. They apply "
        "to YOU; if you delegate work to other crews, do NOT copy these into their instructions — "
        "each crew already applies its own directives):"
    ]
    if purpose:
        lines.append(f"- Purpose: {purpose}")
    label = {"system": "policy", "owner": "policy", "agent": "standing"}
    for r in rules:
        lines.append(f"- [{label.get(r.get('source'), 'rule')}] {r.get('description', '').strip()}")
    return "\n".join(lines)


def _rate_task(
    rater_agent: str, ratee_agent: str, task_id: str, body: dict, owner: str | None = None
) -> tuple[bool, int | None, str]:
    """POST a reputation rating for ratee_agent's task using rater_agent's token (REST).

    The AIMEAT Quality-tab rate endpoint: a coordinator rates a worker it consumed
    (POST /v1/agents/:ratee/tasks/:id/rate). Source-grounded + inter-agent — self-rating is
    rejected 403, missing grounding 422 GROUNDING_REQUIRED (distinct, the caller separates them).
    Returns (ok, status_code, detail). Best-effort: any transport failure returns (False, None, msg)."""
    if _aimeat_read_token is None:
        return False, None, "no token reader available"
    try:
        token, node_url = _aimeat_read_token(rater_agent, owner=owner)
        r = requests.post(
            f"{node_url.rstrip('/')}/v1/agents/{ratee_agent}/tasks/{task_id}/rate",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=body,
            timeout=30,
        )
        ok = r.status_code in (200, 201)
        return ok, r.status_code, ("" if ok else f"{r.status_code} {r.text[:200]}")
    except Exception as exc:  # noqa: BLE001 — transient; caller logs and skips
        return False, None, str(exc)


def _token_exists(agent_name: str, owner: str | None) -> bool:
    """True if the connector has written a token file for this agent yet.

    Mirrors aimeat_crewai's keychain layout (~/.aimeat/tokens/{agent}@{owner}.token). Before
    the owner approves a freshly-registered agent there is no token, so the daemon's
    _read_token would raise at startup — we use this to wait for registration/approval first.
    """
    import glob

    from crewaimeat._home import aimeat_home

    tokens = aimeat_home() / "tokens"
    if owner:
        return (tokens / f"{agent_name}@{owner}.token").is_file()
    return bool(glob.glob(str(tokens / f"{agent_name}@*.token")))


def _wait_for_auth(agent_name: str, owner: str | None, max_wait_seconds: int | None, interval: int = 30) -> None:
    """Wait for the agent's token to exist AND be accepted (the owner has approved it).

    A crew launched BEFORE approval would otherwise crash-loop (no token, or every call 401s)
    until the watchdog gives up. Instead we wait patiently here and continue the moment the
    token is accepted — so an unattended crew comes online by itself once approved, no console.

    Waits while the token is missing OR rejected. Proceeds when accepted (or when the token
    exists but auth cannot be probed — transient / no probe helper). After max_wait_seconds it
    gives up and exits with AUTH_EXIT_CODE so the watchdog stops cleanly and the owner can
    re-approve / re-auth (e.g. message crew-forge "/restart <agent>"). None = wait forever.
    """
    waited = 0
    announced = False
    while True:
        has_token = _token_exists(agent_name, owner)
        # Only probe auth when there's a token to probe; no token -> definitely not ready.
        ready = has_token and _auth_alive(agent_name, owner) is not False
        if ready:
            if announced:
                print(f"[{agent_name}] token accepted — continuing.", file=sys.stderr)
            return
        if not announced:
            reason = (
                "no token yet — register the agent and approve it"
                if not has_token
                else "the token is not accepted (approve it, or it may have been denied)"
            )
            print(
                f"[{agent_name}] waiting for approval: {reason} on AIMEAT (Profile -> Agents). "
                "The crew continues on its own once approved.",
                file=sys.stderr,
            )
            announced = True
        if max_wait_seconds is not None and waited >= max_wait_seconds:
            print(
                f"[{agent_name}] not approved after {waited}s — exiting for re-auth. Re-approve it "
                f"on AIMEAT, then re-run the crew (or message crew-forge '/restart {agent_name}').",
                file=sys.stderr,
            )
            raise SystemExit(AUTH_EXIT_CODE)
        time.sleep(interval)
        waited += interval


# A crew NEVER spawns the serve daemon (the single-spawner discipline that prevents tunnel-stealing
# storms — only the appliance supervisor / start_fleet spawns). But the supervisor restarts the daemon
# on death/tunnel-drop, and during that brief window serve.json transiently names a dead pid. A crew
# that called ensure_serve(auto_start=False) right then hard-crashed ("No live serve daemon …") and the
# per-crew watchdog burned its quick-exit budget (give up at 5) — the appliance startup crash-loop. So
# the crew WAITS for the supervisor's daemon to come back instead of crashing.
SERVE_WAIT_SECONDS = 120  # how long a crew waits for the shared daemon before giving up
SERVE_DAEMON_RETRIES = 3  # re-wait + retry the daemon start this many times on a transient drop


def _wait_for_serve(agent_name: str, max_wait_seconds: int = SERVE_WAIT_SECONDS, interval: int = 2) -> dict:
    """Block (do NOT spawn) until the shared serve daemon is live, returning its discovery doc.

    `ensure_serve(auto_start=False)` returns only when serve.json names a daemon whose pid is alive AND
    answers /local/status — so this rides out both a dead-pid window and a not-yet-bound port. Raises
    AimeatServeError after `max_wait_seconds` so a genuinely-missing supervisor still surfaces."""
    deadline = time.monotonic() + max_wait_seconds
    announced = False
    while True:
        try:
            return ensure_serve(auto_start=False)
        except AimeatServeError:
            if time.monotonic() >= deadline:
                raise
            if not announced:
                print(
                    f"[{agent_name}] waiting for the shared serve daemon (the supervisor is bringing it up)…",
                    file=sys.stderr,
                )
                announced = True
            time.sleep(interval)


def _serve_attach_bridge(agent_name: str) -> None:
    """The approve→attach bridge: make the SHARED serve daemon actually serve this agent.

    The daemon loads its agent set only at STARTUP, so an agent approved AFTER it started (every
    crew-forge-born agent; any late device-auth) is unknown to it: every tunnel call fails
    UNKNOWN_AGENT, Hello Integration strands on its api_call steps, and the daemon-start raises until
    the watchdog burns its quick-exit budget — a fully-approved agent that just never comes online.
    Called right after _wait_for_auth (the moment the token provably exists): if a live daemon already
    serves us this is one serve.json read (the steady state); if there is NO live daemon we do nothing
    (whoever spawns it next loads every token, ours included); only the genuinely-unattached case does
    ONE coordinated restart via serve_guard (spawn-lock serialized — running crews ride out the blip).
    Best-effort: on failure the daemon-start retry loop still gets its chance."""
    if os.environ.get("PYTEST_CURRENT_TEST"):  # a test must never touch the machine's serve daemon
        return
    try:
        doc = ensure_serve(auto_start=False)  # discovery only — never spawns
    except AimeatServeError:
        return  # no live daemon: the next spawn (start_fleet / supervisor) loads all tokens anyway
    if agent_name in {a.get("agent") for a in (doc.get("agents") or [])}:
        return
    print(
        f"[{agent_name}] approved, but the serve daemon predates my token (it serves "
        f"{len(doc.get('agents') or [])} agents, not me) — one coordinated reload to attach.",
        file=sys.stderr,
    )
    try:
        from crewaimeat.serve_guard import restart_serve

        doc = restart_serve()
    except Exception as exc:  # noqa: BLE001 — best-effort; the daemon-start retries may still recover
        print(f"[{agent_name}] serve reload for attach failed ({exc!r}) — continuing.", file=sys.stderr)
        return
    attached = agent_name in {a.get("agent") for a in (doc.get("agents") or [])}
    print(
        f"[{agent_name}] serve daemon reloaded (pid {doc.get('pid')}, {len(doc.get('agents') or [])} agents): "
        f"attached={attached}",
        file=sys.stderr,
    )


# Shared loopback serve daemon (aimeat connect serve --http): discovered once per process,
# then every deterministic call is ONE keep-alive POST on this Session — the daemon multiplexes
# everything over one persistent WebSocket per agent to the node. No subprocess, no per-call TLS.
_SERVE_STATE: dict[str, Any] = {"base": None, "session": None, "warned": False}
_SERVE_LOCK = threading.Lock()


def _serve_api() -> tuple[str, requests.Session] | None:
    """(base_url, shared Session) for the loopback serve daemon. DISCOVERY ONLY — never spawns.

    The daemon is started in exactly ONE place: start_fleet.ps1 (ensure_serve with auto-start),
    BEFORE any crew. Everything else only attaches. This kills the spawn-storm class for good:
    when the daemon died mid-run, dozens of callers used to race-spawn replacements in the gap
    before the winner wrote serve.json -> several daemons survived -> WS-tunnel ping-pong spam.
    Now a missing daemon means a LOUD subprocess fallback, never a spawn."""
    with _SERVE_LOCK:
        if _SERVE_STATE["base"] is not None:
            return _SERVE_STATE["base"], _SERVE_STATE["session"]
        try:
            doc = ensure_serve(auto_start=False)
            _SERVE_STATE["base"] = f"http://127.0.0.1:{doc['port']}"
            # All loopback calls hit ONE host (127.0.0.1), and this one shared Session is used by every
            # agent thread in the fleet host — so requests' default per-host pool (10) fills under the
            # fleet's concurrency (46 agents + parallel EXECUTE) and urllib3 logs "Connection pool is
            # full, discarding connection" (harmless, but it churns TCP). A bigger pool reuses instead.
            _sess = requests.Session()
            _sess.mount("http://", requests.adapters.HTTPAdapter(pool_connections=16, pool_maxsize=64))
            _SERVE_STATE["session"] = _sess
            return _SERVE_STATE["base"], _SERVE_STATE["session"]
        except Exception as exc:  # noqa: BLE001
            if not _SERVE_STATE["warned"]:
                print(
                    f"[aimeat] no live serve daemon ({exc}) -> `aimeat connect call` subprocess "
                    "per call (slow). Start the daemon via scripts/start_fleet.ps1 — crews never "
                    "spawn it themselves.",
                    file=sys.stderr,
                )
                _SERVE_STATE["warned"] = True
            return None


def _serve_reset() -> None:
    """Forget the cached daemon so the next call re-discovers (and auto-restarts) it."""
    with _SERVE_LOCK:
        _SERVE_STATE["base"] = _SERVE_STATE["session"] = None


# Transient TRANSPORT failures — the shared serve tunnel reconnecting, the daemon being recycled, a
# dropped connection. These are worth RETRYING (the tunnel is usually back within seconds): a brief
# nykäys must not lose a memory read/write (the 06-20 Sanomat incident — a tunnel drop mid-run silently
# dropped 7 article categories). A tool-level error (a key that isn't there yet, a validation reject)
# is NOT transient — it returns immediately, so legitimate "not found yet" polls stay fast.
_TRANSIENT_ERR_MARKERS = (
    "tunnel not connected",
    "not connected",
    "no tunnel",
    "connection refused",
    "econnrefused",
    "connection reset",
    "timeout",
    "timed out",
    "temporarily unavailable",
    "502",
    "503",
    "504",
    "bad gateway",
    "service unavailable",
)


def _is_transient_error(err) -> bool:
    """True if an error envelope looks like a transient TRANSPORT failure (retry-worthy) rather than
    a tool-level error (a missing key, a validation reject) which should fail fast."""
    if not err:
        return False
    s = (err if isinstance(err, str) else json.dumps(err, default=str)).lower()
    return any(m in s for m in _TRANSIENT_ERR_MARKERS)


def _aimeat_call(
    agent_name: str, tool: str, payload: dict, *, retries: int = 3, backoff: float = 1.5, quiet: bool = False
) -> dict | None:
    """Deterministic AIMEAT tool call (no LLM).

    Primary path: POST /local/call/<tool> on the shared loopback serve daemon (same tool name +
    JSON input as `connect call`; returns the envelope's data). Fallback when no daemon exists:
    the legacy one-shot `aimeat connect call` subprocess.

    RESILIENCE: a transient TRANSPORT failure (tunnel reconnecting, connection dropped, 5xx) is
    RETRIED up to `retries` times with exponential backoff — the serve daemon is reset between tries
    so the next attempt re-discovers/re-establishes it. Tool-level errors (e.g. a key that isn't
    there yet) are NOT retried — they return None immediately so "not found yet" polls stay cheap."""
    for attempt in range(retries):
        api = _serve_api()
        if api is None:
            return _aimeat_call_subprocess(agent_name, tool, payload)
        base, session = api
        last = attempt + 1 >= retries
        try:
            r = session.post(
                f"{base}/local/call/{tool}",
                json=payload,
                headers={"X-Aimeat-Agent": agent_name},
                timeout=90,
            )
        except requests.RequestException as exc:
            _serve_reset()  # daemon gone mid-flight -> re-discover / auto-restart it on the next try
            if last:
                print(
                    f"[{agent_name}] {tool} loopback POST failed ({exc}); gave up after {retries} tries",
                    file=sys.stderr,
                )
                return None
            print(f"[{agent_name}] {tool} POST failed ({exc}); retry {attempt + 1}/{retries}", file=sys.stderr)
            time.sleep(backoff * (2**attempt))
            continue
        try:
            body = r.json()
        except ValueError:
            print(f"[{agent_name}] {tool} returned non-JSON (HTTP {r.status_code}): {r.text[:120]}", file=sys.stderr)
            return None
        if not isinstance(body, dict) or not body.get("ok"):
            err = (body or {}).get("error") if isinstance(body, dict) else None
            if _is_transient_error(err) and not last:
                _serve_reset()
                print(
                    f"[{agent_name}] {tool} transient failure ({err}); retry {attempt + 1}/{retries}", file=sys.stderr
                )
                time.sleep(backoff * (2**attempt))
                continue
            if not quiet:  # quiet=True for EXPECTED probe failures (e.g. listing an org you don't serve)
                print(f"[{agent_name}] {tool} failed: {err or f'HTTP {r.status_code}'}", file=sys.stderr)
            return None
        return body.get("data")
    return None


def _aimeat_call_subprocess(agent_name: str, tool: str, payload: dict) -> dict | None:
    """Legacy one-shot `aimeat connect call` subprocess (Windows: cmd /c). Kept as the fallback
    for environments without the loopback daemon."""
    if shutil.which("aimeat") is None:
        return None
    base = ["aimeat", "connect", "call", tool, "--agent", agent_name, "--stdin"]
    cmd = ["cmd", "/c", *base] if os.name == "nt" else base
    try:
        proc = subprocess.run(
            cmd,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[{agent_name}] {tool} failed: {exc}", file=sys.stderr)
        return None
    out = (proc.stdout or "").strip()
    if not out:
        return None  # empty result (e.g. memory_read of a key that doesn't exist yet) — not an error
    try:
        return json.loads(out)
    except Exception:  # noqa: BLE001
        print(f"[{agent_name}] {tool} returned non-JSON: {out[:120]}", file=sys.stderr)
        return None


def member_workspaces(agent_name: str) -> list[tuple[str, str]]:
    """(organism_id, ws_id) pairs a contract agent should serve: every organism from
    organism_list PLUS the ids in AIMEAT_CONTRACT_ORGS (comma-separated env).

    Since aimeat 1.23.2 organism_list includes same-owner/implicit memberships natively
    (the connector merges ?member={owner}; verified: image-scout discovers crewaimeat without
    the env). The env extension remains as belt-and-suspenders — it dedups against the list,
    and covers serving an organism the agent is not yet listed in."""
    data = _aimeat_call(agent_name, "aimeat_organism_list", {}) or {}
    orgs = data.get("organisms") or (data if isinstance(data, list) else [])
    org_ids = [o.get("id") for o in orgs if isinstance(o, dict) and o.get("id")]
    for extra in (os.getenv("AIMEAT_CONTRACT_ORGS") or "").split(","):
        if extra.strip() and extra.strip() not in org_ids:
            org_ids.append(extra.strip())
    pairs: list[tuple[str, str]] = []
    for oid in org_ids:
        # quiet: probing an org the agent doesn't serve (or that doesn't exist on this node) returns
        # "not an active member" / "organism not found" — an EXPECTED outcome of scanning, not a fault
        # to spam every idle poll. A real problem still surfaces through the agent's own deliverable.
        wl = _aimeat_call(agent_name, "aimeat_workspace_list", {"organism_id": oid}, quiet=True) or {}
        pairs.extend((oid, w["id"]) for w in (wl.get("workspaces") or []) if w.get("id"))
    return pairs


def contract_record_spaces(agent_name: str, *contracts: dict) -> list[dict]:
    """The subscription list for `listen_for=("records",)` — one {organism_id, ws, space} per
    (member workspace) × (the RECORD/input spaces of the given contract(s)). `space` is the namespace
    key segment (e.g. "shared.moodboard_requests"), what the node matches in keys. Pass one contract or
    several (an agent serving multiple contracts) — member workspaces are discovered ONCE. Resolved at
    daemon start; the connector re-sends the subscribe on reconnect, and a per-space catch-up event
    covers anything written while the socket was down. Replaces a record-scanning idle_hook."""
    namespaces: list[str] = []
    for contract in contracts:
        for s in contract.get("spaces") or []:
            if s.get("mode") == "records" and s.get("namespace"):
                namespaces.append(s["namespace"])
    namespaces = list(dict.fromkeys(namespaces))  # dedup, preserve order
    out: list[dict] = []
    for oid, wid in member_workspaces(agent_name):  # discover member workspaces ONCE
        for ns in namespaces:
            out.append({"organism_id": oid, "ws": wid, "space": ns})
    return out


def record_event_targets(event: dict) -> list[tuple[str, str]] | None:
    """Map a pushed record event to a `process_*(targets=...)` scope: JUST the (organism_id, ws) the
    event came from. An on_record handler passes this so the deterministic scan reads ONLY that one
    workspace — instead of re-discovering (organism_list + a workspace_list per org) and re-scanning
    EVERY member workspace on every event. That bounds per-event traffic and makes a self-write loop
    safe: an agent's own status-write (requested->in-progress->done in a subscribed space) wakes a
    single bounded read of that workspace, the record is already claimed (dedup), so no re-write fires.
    Works for the catch-up event too (op=="catchup" still carries the space's organism_id+ws). Returns
    None when coords are absent (a malformed event) -> process_* falls back to a full member scan."""
    oid, wid = event.get("organism_id"), event.get("ws")
    return [(oid, wid)] if oid and wid else None


def _onboarding_completed(agent_name: str) -> bool:
    data = _aimeat_call(agent_name, "aimeat_onboarding_status", {})
    if not data:
        return False
    # AIMEAT node >=1.35: completion gates on the 12 REQUIRED steps via summary.completable (the optional
    # offers ladder never blocks). Fall back to the pre-1.35 onboarding.status field on an older node.
    if (data.get("summary") or {}).get("completable"):
        return True
    return data.get("onboarding", {}).get("status") == "completed"


def _onboarding_state(agent_name: str) -> tuple[bool, int]:
    """From ONE aimeat_onboarding_status read: (completed, n_drivable_pending). `n_drivable_pending`
    counts pending REQUIRED steps that carry a howTo.tool — the ones the deterministic driver can
    actually advance. completed=False with n_drivable_pending=0 means only toolless/passive steps
    remain, so re-driving would loop forever and we must not. Degrades to (False, 0) on any read/parse
    error — it runs bare in run_crew, so it must never raise and abort startup before the daemon."""
    try:
        data = _aimeat_call(agent_name, "aimeat_onboarding_status", {}) or {}
        completed = bool((data.get("summary") or {}).get("completable")) or (
            (data.get("onboarding") or {}).get("status") == "completed"
        )
        steps = (data.get("onboarding") or {}).get("steps") or []
        guide = data.get("step_guide") or {}
        n_drivable = sum(
            1
            for s in steps
            if isinstance(s, dict)
            and s.get("required")
            and s.get("status") != "passed"
            and (guide.get(s.get("id")) or {}).get("tool")
        )
        return completed, n_drivable
    except Exception as exc:  # noqa: BLE001 — a malformed status payload must not abort startup
        print(
            f"[{agent_name}] onboarding status read failed ({exc!r}) — treating as incomplete/0-drivable",
            file=sys.stderr,
        )
        return False, 0


def _onboarding_marker(agent_name: str) -> Path:
    from crewaimeat._home import aimeat_home

    return aimeat_home() / "logs" / ".locks" / f"onboarding_{agent_name}.attempt"


def _onboarding_attempted_recently(agent_name: str, within_seconds: int = 3600) -> bool:
    """True if we already TRIED onboarding within the window. Guards against re-onboarding (and blocking
    domain work) on every watchdog restart when the node has an optional step with no matching tool that
    can never reach 'completed'. Partial onboarding is fine — the agent is already authorized."""
    p = _onboarding_marker(agent_name)
    try:
        return p.is_file() and (time.time() - p.stat().st_mtime) < within_seconds
    except OSError:
        return False


_ONBOARD_MAX_TRIES = 3  # per .attempt window (1h): re-drive a drivable-but-uncredited step at most this many times


def _mark_onboarding_attempt(agent_name: str, n_drivable: int | None = None, attempts: int = 1) -> None:
    """Record an onboarding attempt (the write refreshes the marker's mtime for
    ``_onboarding_attempted_recently``). The CONTENT stores ``<attempts> <n_drivable>``: `attempts` bounds
    re-drives per window (so a genuinely stuck-but-drivable step is not re-driven on every restart), while
    still retrying a step a prior restart skipped or whose node-side crediting merely lagged."""
    p = _onboarding_marker(agent_name)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"{attempts} {'' if n_drivable is None else n_drivable}", encoding="utf-8")
    except OSError:
        pass


def _onboarding_attempt_info(agent_name: str) -> tuple[int, int | None]:
    """(attempts, n_drivable) recorded at the last onboarding attempt; (0, None) if unknown."""
    try:
        parts = _onboarding_marker(agent_name).read_text(encoding="utf-8").split()
        return (int(parts[0]) if parts else 0), (int(parts[1]) if len(parts) > 1 and parts[1] else None)
    except (OSError, ValueError):
        return 0, None


def _memory_key(agent_name: str, prefix: str | None, task: dict) -> str:
    tid = task.get("id") or "manual"
    short = tid.split("-", 1)[0] if "-" in tid else tid[:8]
    text = task.get("description") or task.get("title") or ""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:32].strip("-")
    token = f"{slug}-{short}" if slug else short
    base = prefix or f"crews.{agent_name}"
    return f"{base}.{token}.latest_output"


def _build_crew_memory(spec: CrewSpec, task: dict) -> Any:
    """Construct a scoped CrewAI Memory for a memory-opted crew (spec.memory is True).

    This is the ONE difference from crewai's `Crew(memory=True)`: we hand Crew a fully-built `Memory`
    instance so its analysis LLM is THIS crew's own get_llm chain (crewai would otherwise default it to
    gpt-4o-mini/OpenAI and crash without OPENAI_API_KEY), the embedder is the cascade-selected tier, and
    storage is a PER-INSTANCE path (not the global CREWAI_STORAGE_DIR env — that would race under the
    threaded fleet_host) scoped per owner/agent/principal so crews never cross-read. Fails LOUD (raises)
    if no embedder is reachable — a crew that asked for memory must not silently run stateless."""
    from crewai.memory.unified_memory import Memory

    from crewaimeat.embedder_cascade import memory_store_path, resolve_embedder, resolve_principal

    embedder, tag = resolve_embedder(spec.agent_name, bias=spec.embedder_bias, override=spec.memory_embedder)
    principal = resolve_principal(task)
    store = memory_store_path(
        spec.agent_name,
        owner=spec.owner,
        principal=principal,
        embedder_tag=tag,
        scope=spec.memory_scope,
        session=task.get("id"),
    )
    print(
        f"[{spec.agent_name}] crew memory ON: scope={spec.memory_scope} principal={principal} "
        f"embedder={tag} -> {store}",
        file=sys.stderr,
    )
    return Memory(
        llm=get_llm(agent_name=spec.agent_name),
        embedder=embedder,
        storage=str(store),
        root_scope=f"/crew/{spec.agent_name}",
    )


# The toolset the deterministic Hello-Integration driver needs — every tool the REQUIRED steps map to
# in the node's howTo contract (status + the 4 confirm tools, capabilities/telemetry reports,
# message_send for send_test_message, the task tools for the test task, memory_write for
# publish_commands/publish_config). declare_services (optional) is included so we can seed services.
# A tight set, not the daemon's full ~24-tool working set.
_ONBOARDING_TOOL_FILTER: tuple[str, ...] = (
    "aimeat_handbook_get",
    "aimeat_onboarding_status",
    "aimeat_onboarding_identify_platform",
    "aimeat_onboarding_confirm_skill_installed",
    "aimeat_onboarding_confirm_directives_read",
    "aimeat_onboarding_declare_services",
    "aimeat_agent_capabilities_report",
    "aimeat_agent_telemetry_report",
    "aimeat_message_send",  # the REAL tool for the send_test_message step (no aimeat_onboarding_* exists)
    "aimeat_task_propose_todos",
    "aimeat_task_todo",
    "aimeat_task_get",
    "aimeat_task_complete",
    "aimeat_memory_write",  # publish_commands -> agents.<name>.commands; publish_config -> agents.config.<name>.*
)


def _resolve_test_task_id(agent_name: str) -> str | None:
    """The agent's still-open onboarding TEST TASK id. `complete_test_task`'s howTo ships a LITERAL
    `{test_task_id}` placeholder in its args, so a driver that passes it verbatim gets 'Task not found' and
    the step stalls at 6/7 forever. Resolve the real id here (the node titles the task 'Onboarding
    verification') before calling aimeat_task_complete. None if no open test task is visible yet."""
    data = _aimeat_call(agent_name, "aimeat_task_list", {}, quiet=True) or {}
    tasks = data.get("tasks") if isinstance(data, dict) else data
    for t in tasks or []:
        if not isinstance(t, dict) or t.get("status") in ("completed", "done", "closed", "failed"):
            continue
        title = (t.get("title") or "").lower()
        verif = t.get("verification") if isinstance(t.get("verification"), dict) else {}
        expects = (verif or {}).get("userExpects", "") or ""
        if "onboarding" in title or "verification" in title or expects.startswith("Agent completes the onboarding"):
            return t.get("id")
    return None


def _finish_pending_onboarding(tools, agent_name: str, step_args: dict, *, attempts: int = 4) -> None:
    """Safety net for the mode-change re-derivation race — with a short retry/backoff drive loop.

    ``aimeat_agent_mode_set`` (run just before onboarding) triggers a node-side re-derivation of the
    onboarding step list to fit the new mode. That write can land AFTER the driver's first
    ``aimeat_onboarding_status`` read, so the driver returns on a stale ``completable=true`` and leaves
    the api_call steps (identify_platform / install_skill / publish_config) pending — the 4/7 stall. A
    single sleep raced the node; instead re-read status with a short backoff and drive EVERY still-
    pending REQUIRED step that has a ``howTo.tool`` (the same tool + node-supplied args the driver would
    use), repeating until nothing drivable remains or the attempts run out (NOT on ``summary.completable``,
    which the node reports true while api_call steps are still pending).
    Idempotent + terminating; best-effort — never raises."""
    status_tool = next((t for t in tools if getattr(t, "name", None) == "aimeat_onboarding_status"), None)
    if status_tool is None:
        return
    for attempt in range(attempts):
        time.sleep(min(1.0 + attempt, 3.0))  # backoff 1,2,3,3s — let a late re-derivation land
        try:
            raw = status_tool.run()
            data = json.loads(raw) if isinstance(raw, str) else raw
        except Exception as exc:  # noqa: BLE001 — verification is best-effort
            print(f"[{agent_name}] onboarding verify: status re-read failed ({exc!r})", file=sys.stderr)
            return
        data = data or {}
        # Do NOT early-return on summary.completable: the node reports completable=true while the api_call
        # steps (identify_platform / install_skill / publish_config) are still pending at 4/7, and
        # run_hello_integration stops at completable — so THOSE are exactly what this safety net must drive
        # to 7/7. Terminate on an empty drivable set instead (below).
        steps = (data.get("onboarding") or {}).get("steps") or []
        guide = data.get("step_guide") or {}
        drivable = [
            s
            for s in steps
            if s.get("required") and s.get("status") != "passed" and (guide.get(s.get("id")) or {}).get("tool")
        ]
        if not drivable:
            # only toolless/passive steps remain — a final status read refreshes lastSeen so they auto-pass
            try:
                status_tool.run()
            except Exception:  # noqa: BLE001
                pass
            return
        print(
            f"[{agent_name}] onboarding: driving {len(drivable)} pending required step(s) "
            f"(attempt {attempt + 1}/{attempts}) — mode re-derivation race.",
            file=sys.stderr,
        )
        for s in drivable:
            sid = s.get("id")
            how = guide.get(sid) or {}
            tool_name = how.get("tool")
            tool = next((t for t in tools if getattr(t, "name", None) == tool_name), None)
            if tool is None:
                continue
            args = dict(step_args.get(sid) or how.get("args") or {})
            if tool_name == "aimeat_memory_write":
                args.setdefault("visibility", "owner")
            if tool_name == "aimeat_task_complete" and ("{" in str(args.get("task_id", "")) or not args.get("task_id")):
                real = _resolve_test_task_id(agent_name)  # resolve the {test_task_id} placeholder -> real task id
                if not real:
                    print(f"[{agent_name}]   {sid}: onboarding test task not visible yet — retrying", file=sys.stderr)
                    continue
                args["task_id"] = real
            try:
                tool.run(**args)
                print(f"[{agent_name}]   {sid} -> {tool_name}: ok", file=sys.stderr)
            except Exception as exc:  # noqa: BLE001 — report; a functional agent is already authorized
                print(f"[{agent_name}]   {sid} -> {tool_name} raised: {exc!r}", file=sys.stderr)
    # attempts exhausted — a final status read to settle any auto-steps
    try:
        status_tool.run()
    except Exception:  # noqa: BLE001
        pass


def _run_onboarding_only(
    agent_name: str, services: list[dict] | None = None, commands: list[dict] | None = None
) -> None:
    """One-shot Hello Integration, driven DETERMINISTICALLY from the node's howTo contract.

    aimeat-crewai 0.12.0's run_hello_integration reads aimeat_onboarding_status (which on AIMEAT
    node >=1.35 returns a per-step howTo + a top-level step_guide + a summary), calls the tool named in
    each pending REQUIRED step's howTo and stops at summary.completable — NO LLM tool-guessing, so a
    small/local model can't invent toolless aimeat_onboarding_<step> names (the old 'Tool not found'
    stall). The optional offers ladder (declare_offerings/make_workflow_compatible/price_offer) is left
    pending; declare_services is seeded separately below. On an older node (no howTo/summary) the driver
    reports it can't proceed and we continue — the agent is already authorized; partial onboarding is OK."""
    print(
        f"[{agent_name}] Hello Integration not done -> running ONBOARDING ONLY (deterministic driver).",
        file=sys.stderr,
    )
    try:
        # Loopback serve daemon: the liaison's MCP calls ride the shared persistent WS tunnel.
        # auto_start=False — crews NEVER spawn the daemon (only start_fleet does); see _serve_api.
        # WAIT for the supervisor's daemon first so a transient restart mid-startup doesn't drop the
        # tunnel under onboarding (the 6/16-stall symptom) before falling back to slow stdio.
        _wait_for_serve(agent_name)
        liaison_params = serve_params(agent_name=agent_name, auto_start=False)
    except Exception as exc:  # noqa: BLE001 — no local daemon (e.g. CI) -> legacy stdio subprocess
        print(f"[{agent_name}] serve daemon unavailable ({exc}) -> stdio fallback", file=sys.stderr)
        liaison_params = stdio_params(agent_name=agent_name)
    # Override publish_commands with THIS crew's real palette (else the driver publishes the node's
    # example commands). Other steps use the node's howTo.args verbatim.
    step_args: dict[str, dict] = {}
    if commands:
        step_args["publish_commands"] = {
            "key": f"agents.{agent_name}.commands",
            "value": commands,
            "visibility": "owner",
        }
    try:
        # create_liaison_agent gives the same MCP tool objects the driver needs (.name + .run) AND
        # context-managed cleanup of the adapter; the built agent's LLM is never invoked (the driver
        # selects tools from the node contract, not the model).
        with create_liaison_agent(
            mcp_server_params=liaison_params,
            agent_name=agent_name,
            llm=get_llm(agent_name=agent_name),
            tool_filter=_ONBOARDING_TOOL_FILTER,
            verbose=False,
        ) as liaison:
            run_hello_integration(
                liaison.tools,
                agent_name=agent_name,
                step_args=step_args,
                sleep_seconds=1.0,  # a beat between rounds so passive steps (configure_delivery) register
                logger=lambda m: print(f"[{agent_name}] {m}", file=sys.stderr),
            )
            # Safety net: if the mode-change re-derivation landed after the driver's first status read,
            # the driver may have returned on a stale completable=true — drive any leftover required step.
            _finish_pending_onboarding(liaison.tools, agent_name, step_args)
            # declare_services is OPTIONAL (the required-only driver skips it) — seed it for discoverability.
            if services:
                ds = next(
                    (t for t in liaison.tools if getattr(t, "name", None) == "aimeat_onboarding_declare_services"),
                    None,
                )
                if ds is not None:
                    try:
                        ds.run(services=services)
                        print(f"[{agent_name}] declared {len(services)} service(s).", file=sys.stderr)
                    except Exception as exc:  # noqa: BLE001 — optional; never block the daemon
                        print(
                            f"[{agent_name}] declare_services failed (optional, continuing): {exc!r}", file=sys.stderr
                        )
    except OnboardingError as exc:
        # Node/connector out of sync, or node <1.35 (no machine-readable howTo/summary). The agent is
        # already authorized; partial onboarding is fine — log and continue to domain work.
        print(
            f"[{agent_name}] deterministic onboarding could not complete ({exc}) — continuing "
            "(the agent is already authorized; needs AIMEAT node >=1.35 for full Hello Integration).",
            file=sys.stderr,
        )
    except Exception as exc:  # noqa: BLE001 — onboarding must NEVER crash the daemon
        print(f"[{agent_name}] onboarding ended with an error (continuing): {exc!r}", file=sys.stderr)
    print(f"\n=== {agent_name}: ONBOARDING-ONLY done ===", file=sys.stderr)


def _finalize_task(agent_name: str, tid: str, mem_key: str, liaison: Agent) -> Task:
    """Liaison closing task. The deliverable is published and the task is completed
    DETERMINISTICALLY by scaffold callbacks (see _make_publish_cb / _make_complete_cb) — not by
    the LLM, which proved unreliable at memory_write on weaker models. This task therefore only
    handles todos (when a task has them); it keeps the liaison in the crew per the daemon contract.

    Sequential todo marking + parallel_tool_calls=False keeps concurrent task-state writes safe.
    """
    return Task(
        description=(
            f"The crew has finished the work for AIMEAT task '{tid}'. Its deliverable has already "
            "been saved to memory, and the task will be marked complete for you automatically — you "
            "do NOT need to write memory and you do NOT need to call aimeat_task_complete.\n"
            f"Your only job: call aimeat_task_get for '{tid}'. If it has any todos, mark each one "
            "done with aimeat_task_todo (status='done') ONE AT A TIME — fire one call, wait for its "
            "result, then the next (never several in one turn). If there are no todos, just reply 'done'."
        ),
        expected_output="Any todos marked done one at a time; otherwise 'done'.",
        agent=liaison,
    )


# A coordinator can ask a delegated worker to ALSO publish into a SHARED TAG memory area it can
# read with its own scope. The marker is embedded in the task description and stripped before the
# domain agents see it.
_PUBLISH_DIRECTIVE = re.compile(r'<<AIMEAT_PUBLISH\s+key="([^"]+)"(?:\s+tag="([^"]*)")?\s*>>')


def _parse_publish_directive(text: str) -> tuple[str | None, str | None, str]:
    """Return (shared_key, tag, cleaned_text) from a task description carrying a publish marker."""
    m = _PUBLISH_DIRECTIVE.search(text or "")
    if not m:
        return None, None, text
    cleaned = (text[: m.start()] + text[m.end() :]).strip()
    return m.group(1), (m.group(2) or None), cleaned


_VERIFY_DIRECTIVE = re.compile(r"<<\s*(NO)?VERIFY\s*>>", re.I)


def _parse_verify_directive(text: str) -> tuple[str | None, str]:
    """Return (override, cleaned_text) from a task description: <<VERIFY>> -> 'on', <<NOVERIFY>> ->
    'off', neither -> None (use the CrewSpec default). Lets the owner flip the verification pass per
    task from the dashboard without touching code."""
    m = _VERIFY_DIRECTIVE.search(text or "")
    if not m:
        return None, text
    override = "off" if m.group(1) else "on"
    return override, _VERIFY_DIRECTIVE.sub("", text).strip()


_VERIFY_SCORE_RE = re.compile(r"score\s*=\s*([1-5])", re.I)
_VERIFY_UNSUP_RE = re.compile(r"unsupported\s*=\s*(\d+)", re.I)


def _write_verify_stat(agent_name: str, tid: str | None, output_text: str, dimension: str) -> None:
    """Parse the factcheck Reviewer's score line and write it as the agent's OWN introspection under
    agents.<agent>.statistics.custom.<short>.verify (owner-visible). This is a SELF assessment of the
    crew's own deliverable — under AIMEAT's Quality-tab contract an agent cannot rate itself via the
    rate endpoint (self-rating → 403), so the self-verify is kept as internal data, not a reputation
    rating. Source-grounded faithfulness (Reviewer checked the deliverable vs its context inputs) —
    validated by POC v2. Best-effort; no-op if no score line. Inter-agent reputation ratings come from
    the coordinator rating its workers (workflow.py _judge_and_rate -> POST /tasks/:id/rate)."""
    m = _VERIFY_SCORE_RE.search(output_text or "")
    if not m:
        return
    score = int(m.group(1))
    um = _VERIFY_UNSUP_RE.search(output_text or "")
    unsupported = int(um.group(1)) if um else None
    short = (tid or "manual").split("-", 1)[0]
    # Single-segment custom key (agents.<agent>.statistics.custom.<name>) so the Quality tab's Custom
    # Metrics renders it; it holds the LATEST self-verify (the per-task reputation history lives on the
    # rate endpoint). `task` keeps it traceable to the run.
    res = _aimeat_call(
        agent_name,
        "aimeat_memory_write",
        {
            "key": f"agents.{agent_name}.statistics.custom.self_verify",
            "value": {
                "score": score,
                "by": agent_name,
                "role": "self-verify",
                "dimension": dimension,
                "unsupported": unsupported,
                "task": short,
                "ts": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            },
            "visibility": "public",  # like other statistics.* keys, so the Quality "Custom Metrics" tab renders it
        },
    )
    print(
        f"[{agent_name}] self-verify score {score}/5 (unsupported={unsupported}, dim={dimension}) -> statistics.custom.self_verify: {bool(res)}",
        file=sys.stderr,
    )


def _publish_selection_rollup(agent_name: str, owner: str | None = None, _state: dict | None = None) -> None:
    """Publish the agent's OWN field-reputation rollup to its PUBLIC memory key
    agents.<agent>.statistics.custom.selection — the live-score key a coordinator's discover_crews reads.

    The node aggregates real task ratings at GET /v1/agents/:agent/statistics, but that route is
    owner-only — a coordinator's agent token 403s on a PEER's stats. An agent CAN read its OWN stats,
    so each crew self-publishes its rollup to public memory, where any same-owner coordinator can read it
    (memory_read_public). Without this writer the selection key never exists and discover_crews shows
    '[no reputation yet]' forever. Normalizes avgStars (0-5) -> 0-1 and carries the node's confidence.
    Best-effort; never raises."""
    try:
        # P3 (aimeat-crewai 0.7.0): read the agent's OWN stats over the TUNNEL TOOL instead of a direct
        # owner-only GET /v1/agents/:name/statistics — it rides the open WS (no separate connection).
        # _aimeat_call returns the envelope's data (performance + reviews). quiet: an agent with no stats
        # yet (or not registered on this node) returns NOT_FOUND — that is an EXPECTED "no reputation yet",
        # not a fault to spam every ~10 min idle cycle (it just leaves the selection key absent).
        data = _aimeat_call(agent_name, "aimeat_agent_statistics", {}, quiet=True)
        reviews = (data.get("reviews") or {}) if isinstance(data, dict) else {}
        overall = reviews.get("overall") or {}
        n = overall.get("n") or 0
        avg = overall.get("avgStars")
        if not n or avg is None:
            return  # nothing rated yet — leave the key absent so discovery shows "[no reputation yet]"
        byctx = reviews.get("byContext") or {}
        ctx = max(byctx, key=lambda c: (byctx.get(c) or {}).get("n", 0)) if byctx else "overall"
        low = (byctx.get(ctx) or {}).get("lowConfidence", n < 3)
        value = {
            "context": ctx,
            "normalized": round(avg / 5.0, 2),
            "n": n,
            "confident": (not low),
            "avg_stars": avg,
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        # CONDITIONAL write: only publish when the SCORE actually moved. The ts always differs, so compare
        # just the score fields against the last publish — an idle agent whose reputation hasn't changed
        # skips the memory write entirely (the common case), so the only periodic cost is the stats GET.
        sig = {k: value[k] for k in ("context", "normalized", "n", "confident", "avg_stars")}
        if _state is not None and _state.get("sig") == sig:
            return  # unchanged since last publish — skip the write
        if _state is not None:
            _state["sig"] = sig
        _aimeat_call(
            agent_name,
            "aimeat_memory_write",
            {"key": f"agents.{agent_name}.statistics.custom.selection", "value": value, "visibility": "public"},
        )
        print(f"[{agent_name}] published field-reputation rollup -> selection {value}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 — reputation publish is best-effort; never break the daemon
        print(f"[{agent_name}] selection-rollup publish skipped: {exc}", file=sys.stderr)


def _eval_ctx(eval_info: dict | None) -> dict:
    """Build the evaluation-context record {model, temperature, nature, tokens_*} for a run.

    Captures the actual LLM the deliverable was produced with (model + temperature from the run's
    LLM) and the token usage (crew.usage_metrics, read via a mutable holder so it is populated by
    kickoff). This is the eval-params payload AIMEAT's rate endpoint stores in rating.metadata, and
    a coordinator reads the worker's copy to attribute a rating to the right model/cost — crew-run
    level (not per-claim). Returns {} when nothing is known."""
    if not eval_info:
        return {}
    ctx: dict = {}
    if eval_info.get("model"):
        ctx["model"] = eval_info["model"]
    if eval_info.get("temperature") is not None:
        ctx["temperature"] = eval_info["temperature"]
    if eval_info.get("nature"):
        ctx["nature"] = eval_info["nature"]
    if eval_info.get("task"):
        ctx["task"] = eval_info["task"]
    crew = (eval_info.get("crew_holder") or {}).get("crew")
    um = None
    if crew is not None:
        # crew.usage_metrics (the field) is None until kickoff FINISHES, but this callback runs
        # mid-kickoff — so aggregate live from the agents' LLMs instead (token usage accrues there
        # as the run proceeds). Falls back to the field if the live call is unavailable.
        try:
            um = crew.calculate_usage_metrics()
        except Exception:  # noqa: BLE001
            um = getattr(crew, "usage_metrics", None)
    if um is not None:
        for src, dst in (
            ("prompt_tokens", "tokens_in"),
            ("completion_tokens", "tokens_out"),
            ("total_tokens", "tokens_total"),
        ):
            v = getattr(um, src, None)
            if v:  # skip None/0 — a real run has nonzero tokens by publish time
                ctx[dst] = v
    return ctx


def _make_publish_cb(
    agent_name: str,
    primary_key: str,
    shared_key: str | None = None,
    tag: str | None = None,
    eval_info: dict | None = None,
    task_id: str | None = None,
    clean: Callable[[str], str] | None = None,
    offer_id: str | None = None,
):
    """Task callback: write the task output to AIMEAT memory deterministically (no LLM).

    Attached to the last DOMAIN task so the deliverable always lands, even if the liaison's
    LLM-driven memory_write loops or errors (observed on weaker models). Always writes the agent's
    own key; if a shared_key/tag are supplied (a delegated workflow subtask), ALSO writes into the
    shared tag area so the coordinator can collect it with its own scope. When eval_info is given,
    also records the run's eval-context (model/temperature/tokens): the shared `<shared_key>.evalctx`
    is written BEFORE the shared deliverable (so a coordinator that detects the deliverable always
    finds the evalctx beside it), plus an own-introspection copy under statistics.custom.*.

    task_id (the full AIMEAT task id) is added as a `task:<id>` tag on every per-task write so AIMEAT
    can list a task's memory entries by tag (GET /v1/memory?...&tags=task:<id>). The tag is additive —
    key formats are unchanged — and since the callback is deterministic it lands as surely as the
    deliverable itself. When the task was ordered from the Offers surface (scope carries offer_id), an
    `offer:<offer_id>` tag is added too, so the Offerings card can list the last N runs for THAT offer."""
    task_tag = f"task:{task_id}" if task_id else None
    offer_tag = f"offer:{offer_id}" if offer_id else None
    _per_task_tags = [t for t in (task_tag, offer_tag) if t]  # additive; key formats unchanged

    def _cb(task_output) -> None:
        text = getattr(task_output, "raw", None)
        if text is None:
            text = str(task_output)
        if clean:  # deterministic post-processor (e.g. strip an editor's leaked KEPT/CUT notes)
            try:
                cleaned = clean(text)
                if cleaned:  # never publish an empty deliverable; fall back to the original
                    text = cleaned
            except Exception as exc:  # noqa: BLE001 — cleaning is best-effort, must not block publish
                print(f"[{agent_name}] clean_deliverable skipped: {exc}", file=sys.stderr)
        r1 = _aimeat_call(
            agent_name,
            "aimeat_memory_write",
            {"key": primary_key, "value": text, "visibility": "owner", "tags": list(_per_task_tags)},
        )
        print(
            f"[{agent_name}] deliverable published -> {primary_key} (tags {_per_task_tags}): {bool(r1)}",
            file=sys.stderr,
        )
        ectx = _eval_ctx(eval_info)
        if ectx and eval_info and eval_info.get("custom_key"):
            _aimeat_call(  # own performance introspection; public so the Quality Custom Metrics tab renders it
                agent_name,
                "aimeat_memory_write",
                {"key": eval_info["custom_key"], "value": ectx, "visibility": "public"},
            )
        if shared_key:
            shared_tags = [t for t in (tag, task_tag, offer_tag) if t]  # delegation + per-task + per-offer (additive)
            if ectx:  # write evalctx FIRST so it is present when the coordinator sees the deliverable
                _aimeat_call(
                    agent_name,
                    "aimeat_memory_write",
                    {"key": f"{shared_key}.evalctx", "value": ectx, "visibility": "owner", "tags": shared_tags},
                )
            r2 = _aimeat_call(
                agent_name,
                "aimeat_memory_write",
                {"key": shared_key, "value": text, "visibility": "owner", "tags": shared_tags},
            )
            print(f"[{agent_name}] deliverable shared -> {shared_key} (tag {tag}): {bool(r2)}", file=sys.stderr)

    return _cb


def _resolve_offer(agent_name: str, task: dict) -> dict | None:
    """OFFER TASK SHAPE (Offers handover v3): a task ordered from the Offers surface carries
    ONLY the user's request in title/description; the offer travels structurally in scope
    (kind='offer', offer_id, offer_title). Resolve the agent's OWN offer descriptor from its
    published offers doc so build_domain can pick the right mode — the offer's ask/example is
    NEVER re-fed as the request (that made agents treat their boilerplate as the ask)."""
    raw_scope = task.get("scope") or []
    if isinstance(raw_scope, dict):
        scope = raw_scope
    else:
        scope = {s.get("name"): s.get("value") for s in raw_scope if isinstance(s, dict)}
    if scope.get("kind") != "offer":
        return None
    oid = str(scope.get("offer_id") or "")
    doc = _aimeat_call(agent_name, "aimeat_memory_read", {"key": f"agents.{agent_name}.offers"}) or {}
    val = doc.get("value") if isinstance(doc, dict) else None
    for o in (val or {}).get("offers") or []:
        if o.get("id") == oid:
            print(f"[{agent_name}] offer task: fulfilling offer '{oid}'", file=sys.stderr)
            return o
    print(
        f"[{agent_name}] offer task: offer_id {oid!r} not found in published offers — running on the raw request",
        file=sys.stderr,
    )
    return {"id": oid, "title": str(scope.get("offer_title") or oid)}


def _make_complete_cb(
    agent_name: str,
    tid: str,
    mem_key: str | None = None,
    require_verify: bool = False,
    owner: str | None = None,
    auto_revert: bool = False,
):
    """Task callback: close the AIMEAT task deterministically (no LLM). Attached to the finalize
    task so the task is completed even if the liaison never calls aimeat_task_complete.

    When require_verify is True (CrewSpec.require_verify_pass — SYS-1), completion is GATED on the app
    verify gates' deterministic outcome: a build whose verify_render / verify_interaction FAILED, or that
    never ran a gate at all, is FAILED (aimeat_task_fail) instead of shipping 'green'. The verdicts come
    from the gate {ok} recorded by the verify tools (author_tool.get_verify_verdicts), never the agent's
    self-reported text — the whole point is to not trust the self-report. The gate is STATUS-ONLY.

    When auto_revert is True (CrewSpec.auto_revert_on_fail), a gate-fail ALSO restores each app this run
    published to its pre-run last-good version (revert_apps_to_baseline) — an outward-facing live rollback,
    kept a SEPARATE opt-in from the safe status gate."""

    def _cb(_task_output) -> None:
        if require_verify:
            try:
                from crewaimeat.author_tool import get_verify_verdicts

                verdicts = get_verify_verdicts(tid)
            except Exception as exc:  # noqa: BLE001 — never break finalize on the lookup
                print(f"[{agent_name}] verify-gate lookup failed ({exc}); completing without gating", file=sys.stderr)
                verdicts = None
            if verdicts is not None:
                failed = sorted(g for g, v in verdicts.items() if v.get("ok") is False)
                passed = [g for g, v in verdicts.items() if v.get("ok") is True]
                reason = None
                if failed:
                    reason = (
                        f"Not shipping a broken build: verify gate(s) FAILED — {', '.join(failed)}. "
                        "Fix the app and re-queue."
                    )
                elif not passed:
                    reason = (
                        "Not shipping unverified: no verify gate produced a PASS. A build must prove "
                        "itself with verify_render / verify_interaction before it can complete."
                    )
                if reason:
                    # The gate itself only fails the task (status-only). Optional, separate opt-in:
                    if auto_revert:
                        # also restore each app this run published to its pre-run last-good version, so the
                        # LIVE app is rolled back, not just left un-'done' (the recorded rollback baseline).
                        restored = []
                        try:
                            from crewaimeat.author_tool import revert_apps_to_baseline

                            restored = [r for r in revert_apps_to_baseline(agent_name, tid, owner) if r.get("ok")]
                        except Exception as exc:  # noqa: BLE001 — revert is best-effort; still fail the task
                            print(f"[{agent_name}] auto-revert skipped ({exc})", file=sys.stderr)
                        if restored:
                            names = ", ".join(f"{r['filename']}->v{r['to_version']}" for r in restored)
                            reason += f" Auto-restored {len(restored)} app(s) to last-good: {names}."
                    fr = _aimeat_call(agent_name, "aimeat_task_fail", {"task_id": tid, "message": reason})
                    print(
                        f"[{agent_name}] require_verify_pass GATE -> task_fail {tid}: {reason[:90]} ({bool(fr)})",
                        file=sys.stderr,
                    )
                    return
        payload = {"task_id": tid, "message": "Crew finished; deliverable published to memory."}
        if mem_key:
            # The Offers/Inbox contract: the task record's deliverableKey points at the memory key
            # holding the deliverable — without it the Inbox shows the task but no content/sample.
            payload["deliverableKey"] = mem_key
            payload["message"] = f"Crew finished; deliverable published to memory at {mem_key}."
        res = _aimeat_call(agent_name, "aimeat_task_complete", payload)
        print(
            f"[{agent_name}] task completed deterministically {tid} (deliverableKey={mem_key or '-'}): {bool(res)}",
            file=sys.stderr,
        )

    return _cb


def _finalize_message_task(agent_name: str, mem_key: str, sender: str | None, liaison: Agent) -> Task:
    """Finalize for a run triggered by an inbox MESSAGE (no real task to complete).

    A message arrives as a synthetic task (id 'msg-...'), so there are no todos and
    nothing to aimeat_task_complete. Instead: publish the result to memory and, if the
    sender is known, reply to them with a short summary.
    """
    reply_step = (
        f"2. Reply to the sender '{sender}' with aimeat_message_send: a short summary of what "
        "was done (one or two sentences)."
        if sender
        else "2. No sender to reply to; skip messaging."
    )
    return Task(
        description=(
            "The crew has handled a request that arrived as an AIMEAT inbox message (NOT a task), "
            "so there are no todos and no task to complete. Work in order, one tool call at a time.\n"
            f"1. Write the previous agent's final result to AIMEAT memory under the EXACT key "
            f"'{mem_key}' with visibility owner (aimeat_memory_write).\n"
            f"{reply_step}\n"
            "Do NOT call aimeat_task_complete or aimeat_task_todo — this was a message, not a task."
        ),
        expected_output=f"Result written to memory '{mem_key}'"
        + (f" and a reply sent to '{sender}'." if sender else "."),
        agent=liaison,
    )


# README directives, expanded at publish time:
#   [[FIGLET]["text"]] / [[FIGLET:font]["text"]] -> deterministic ASCII-art (pyfiglet)
#   [[LLM]["prompt"]]                            -> the LLM's response to that prompt
_FIGLET_DIRECTIVE = re.compile(r"\[\[FIGLET(?::([\w-]+))?\]\[(.*?)\]\]", re.DOTALL)
_LLM_DIRECTIVE = re.compile(r"\[\[LLM\]\[(.*?)\]\]", re.DOTALL)
# [[AVAILABLE_COMMANDS][]] -> a table rendered from the crew's `commands` list (single source).
_AVAILABLE_COMMANDS_DIRECTIVE = re.compile(r"\[\[AVAILABLE_COMMANDS\]\[(.*?)\]\]", re.DOTALL)


def _unquote(s: str) -> str:
    return s.strip().strip('"').strip("'").strip()


def _render_commands(commands: list[dict] | None) -> str:
    """Render the commands list as a markdown table for the README."""
    if not commands:
        return "_No commands declared._"
    rows = "\n".join(f"| `{c.get('name', '')}` | {c.get('description', '')} |" for c in commands)
    return "| Command | Description |\n| --- | --- |\n" + rows


def _humanize_name(agent_name: str) -> str:
    """'jingle-writer' -> 'Jingle Writer' for a friendly README title."""
    return re.sub(r"[-_]+", " ", agent_name).strip().title() or agent_name


def _default_readme(agent_name: str) -> str:
    """A sensible README for a crew whose author did not supply one (e.g. crew-forge builds).

    Deterministic: a FIGLET logo of the name, a one-line description, how to task it, and the
    command table (empty-safe). Keeps every generated crew's README tab from being blank — the
    same shape the hand-written crews use, just generic where they are specific.
    """
    title = _humanize_name(agent_name)
    return (
        f'[[FIGLET:slant]["{title}"]]\n\n'
        f"# {agent_name}\n\n"
        "A task-runner crew on the AIMEAT scaffold (crewaimeat). Queue it a goal and it runs the "
        "work, then publishes the finished result to its memory.\n\n"
        "## How to task me\n"
        "Open the **Tasks** tab, choose **+ New Task**, and describe what you want in plain "
        "language. I take it from there and post the deliverable when it is done.\n\n"
        "## Commands\n"
        "[[AVAILABLE_COMMANDS][]]\n"
    )


def _figlet_repl(m: re.Match[str]) -> str:
    """Render [[FIGLET[:font]]["text"]] to ASCII-art wrapped in a code fence (monospace)."""
    font = m.group(1) or "standard"
    text = _unquote(m.group(2))
    if not text:
        return ""
    try:
        from pyfiglet import figlet_format

        art = figlet_format(text, font=font).rstrip("\n")
        return f"```\n{art}\n```"
    except Exception as exc:  # noqa: BLE001 — unknown font / pyfiglet missing
        return f"[[FIGLET directive failed: {exc}]]"


def _expand_readme(text: str, llm: Any, commands: list[dict] | None = None) -> str:
    """Expand README directives. Deterministic ones (FIGLET, AVAILABLE_COMMANDS) first, then LLM.

    Lets a README stay dynamic (a generated logo, the live command list, an LLM-written tagline)
    while the rest is plain markdown. A directive whose expansion fails is left as a visible
    marker, never crashes.
    """
    text = _FIGLET_DIRECTIVE.sub(_figlet_repl, text)
    text = _AVAILABLE_COMMANDS_DIRECTIVE.sub(lambda _m: _render_commands(commands), text)

    def _repl(m: re.Match[str]) -> str:
        prompt = _unquote(m.group(1))
        if not prompt:
            return ""
        try:
            out = llm.call(
                [
                    {
                        "role": "system",
                        "content": (
                            "Output ONLY the requested content (e.g. the raw ASCII art or text). "
                            "No explanation, no preamble, no surrounding code fences unless asked."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ]
            )
            return (out or "").strip("\n")
        except Exception as exc:  # noqa: BLE001
            return f"[[LLM directive failed: {exc}]]"

    return _LLM_DIRECTIVE.sub(_repl, text)


# Task-nature gate: classify fact vs creative -> cooler/hotter temperature + grounding + verify mode.
_NATURE_TEMP = {"fact": 0.15, "creative": 0.7, "mixed": 0.4}
_NATURE_CREATIVE_HINTS = (
    "joke",
    "jingle",
    "poem",
    "story",
    "funny",
    "slogan",
    "tagline",
    "brainstorm",
    "song",
    "vitsi",
    "runo",
    "laulu",
    "hauska",
    "tarina",
)
_GROUNDING_RULE = (
    "GROUNDING (this work involves factual claims): state only what your sources/inputs actually support. If you "
    "cannot confirm a specific (name, number, date, organisation) from a source, write 'ei julkista "
    "tietoa löytynyt' / 'not found' — do NOT invent estimate-ranges, and NEVER attach a citation to "
    "anything you did not actually find. Never present an invented specific as a verified fact."
)


def _classify_task_nature(prompt: str, llm: Any) -> dict:
    """Classify a task as fact | creative | mixed and derive its temperature, grounding and verify
    mode. One cheap LLM call with a deterministic keyword fallback. fact -> cool (~0.15, not 0) +
    grounded + faithfulness verify; creative -> warm (~0.7) + free; mixed -> in between."""
    text = (prompt or "").lower()
    nature = "creative" if any(k in text for k in _NATURE_CREATIVE_HINTS) else "fact"
    try:
        reply = (
            (
                llm.call(
                    [
                        {
                            "role": "user",
                            "content": (
                                "Classify this task as exactly ONE word — 'fact' (needs verifiable facts, real entities, "
                                "data, sources), 'creative' (invent/entertain, nothing to fact-check), or 'mixed'. Reply "
                                "with ONLY the one word.\n\nTask:\n" + (prompt or "")[:1500]
                            ),
                        }
                    ]
                )
                or ""
            )
            .strip()
            .lower()
        )
        for n in ("mixed", "creative", "fact"):
            if n in reply:
                nature = n
                break
    except Exception:  # noqa: BLE001
        pass
    return {
        "nature": nature,
        "temperature": _NATURE_TEMP[nature],
        "ground": nature in ("fact", "mixed"),
        "verify": "factcheck" if nature in ("fact", "mixed") else "off",
    }


def _liaison_tool_filter(discover: bool):
    """The daemon liaison's MCP tool allowlist: the default ~24-tool set, plus `aimeat_discover` (the master
    directory) when `discover` is on. The default filter omits discover, so without this the 0.10.0 liaison
    backstory would steer at a tool it can't load."""
    return (*DAEMON_DEFAULT_TOOL_FILTER, "aimeat_discover") if discover else DAEMON_DEFAULT_TOOL_FILTER


def _valid_chat_commands(cmds) -> list[dict]:
    """Normalise + validate a chat-command list to the dev's public schema. Keeps only well-formed entries
    (need a charset-safe `id`); coerces param types to {text,number,select}; caps the count. Fail-soft —
    junk is dropped, never raised, so a generated palette can't break startup."""
    out: list[dict] = []
    for c in cmds or []:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id") or "").strip()
        if not re.fullmatch(r"[a-z0-9_-]+", cid):
            continue
        cmd = {
            "id": cid,
            "label": str(c.get("label") or cid),
            "description": str(c.get("description") or ""),
            "template": str(c.get("template") or ""),
        }
        params: list[dict] = []
        for p in c.get("params") or []:
            if not isinstance(p, dict) or not p.get("name"):
                continue
            ptype = p.get("type") if p.get("type") in ("text", "number", "select") else "text"
            pp: dict = {"name": str(p["name"]), "type": ptype, "required": bool(p.get("required", False))}
            if p.get("placeholder"):
                pp["placeholder"] = str(p["placeholder"])
            if p.get("default") is not None:
                pp["default"] = str(p["default"])
            if ptype == "select" and isinstance(p.get("options"), list):
                pp["options"] = [str(o) for o in p["options"]]
            params.append(pp)
        if params:
            cmd["params"] = params
        out.append(cmd)
    return out[:24]


def _publish_readme(agent_name: str, readme_md: str, commands: list[dict] | None = None) -> None:
    """Expand README directives (once per content change) and publish to memory.

    Writes the expanded markdown to agents.<agent>.readme (owner-visible) for the README
    tab. A local cache keyed by a hash of the source (README + commands) means the LLM only
    runs when the content actually changes — so watchdog restarts don't re-bill or reshuffle it.
    """
    cache_seed = readme_md + "\n\x00" + json.dumps(commands or [], sort_keys=True)
    src_hash = hashlib.sha256(cache_seed.encode("utf-8")).hexdigest()
    cache_dir = Path.cwd() / "logs" / ".readme_cache"
    body_file = cache_dir / f"{agent_name}.md"
    hash_file = cache_dir / f"{agent_name}.hash"

    expanded: str | None = None
    if hash_file.is_file() and body_file.is_file() and hash_file.read_text(encoding="utf-8").strip() == src_hash:
        expanded = body_file.read_text(encoding="utf-8")  # unchanged -> reuse, no LLM call
    if expanded is None:
        expanded = _expand_readme(readme_md, get_llm(for_tool_use=False, agent_name=agent_name), commands)
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            body_file.write_text(expanded, encoding="utf-8")
            hash_file.write_text(src_hash, encoding="utf-8")
        except OSError:
            pass  # cache is best-effort

    res = _aimeat_call(
        agent_name,
        "aimeat_memory_write",
        {"key": f"agents.{agent_name}.readme", "value": expanded, "visibility": "owner"},
    )
    print(f"[{agent_name}] published README to agents.{agent_name}.readme: {bool(res)}", file=sys.stderr)


def _effective_mode(spec: CrewSpec) -> str:
    """The AIMEAT agent mode to set on start. Explicit `spec.mode` wins; otherwise crewaimeat crews are
    task-runners — EXCEPT DM-serviceable / self-monitoring crews, which need the interactive message
    surface (chat/inbox) that task-runner mode drops, so they stay 'interactive'."""
    if spec.mode:
        return spec.mode
    if spec.dm_serviceable or spec.self_monitor:
        return "interactive"
    return "task-runner"


def run_crew(spec: CrewSpec) -> None:
    """Entry point: ensure onboarding once, then run the daemon forever.

    The daemon polls the AIMEAT queue; for each active task it builds a crew of
    [liaison, *your domain agents] with tasks [*your domain tasks, finalize] and
    runs it. Stop with Ctrl+C.
    """
    # A rare native crash (observed: Windows exit 0xC0000409) leaves no Python traceback by
    # default. faulthandler dumps the C/Python stack on a fatal signal so the NEXT one is
    # diagnosable instead of a silent exit code; harmless when nothing crashes.
    try:
        import faulthandler

        faulthandler.enable()
    except Exception:  # noqa: BLE001 — never let diagnostics break startup
        pass

    progress = install_progress(spec.agent_name)

    # 0a) Single-instance guard: if another daemon for THIS agent is already running, exit
    #     cleanly instead of double-dispatching its tasks (two daemons each poll the same active
    #     task and run it). Name-based, so it catches duplicates however they were launched.
    if not _acquire_single_instance(spec.agent_name):
        print(
            f"[{spec.agent_name}] another daemon for this agent already holds the single-instance "
            "lock — exiting to avoid duplicate task dispatch.",
            file=sys.stderr,
        )
        raise SystemExit(0)

    # 0) If launched before the owner approved the agent, wait patiently for the token
    #    to be accepted rather than crash-looping in onboarding. Lets an unattended crew
    #    come online by itself once approved (no console needed).
    _wait_for_auth(spec.agent_name, spec.owner, spec.wait_for_approval_seconds)

    # 0a2) Approve→attach bridge: a running serve daemon predating our token doesn't serve us (it loads
    #      agents at startup) — one coordinated reload attaches us BEFORE any tunnel call, so the
    #      publishes below, Hello Integration and the daemon all get a working bridge. No-op (one
    #      serve.json read) when already attached — the steady state of every normal restart.
    _serve_attach_bridge(spec.agent_name)

    # 0b) Set the agent's MODE (idempotent, every start, BEFORE onboarding so the node serves the mode's
    #     step list). crewaimeat crews are task-runners; the node otherwise defaults a device-authed agent
    #     with no mode to 'interactive', which gates every created task behind a manual 'Start this task' in
    #     the dashboard. task-runner mode makes the node AUTO-ACTIVATE tasks on create — test runs and real
    #     work just run. (The connector dropped device-auth's --mode flag, so we set it here, not at register.)
    _mode = _effective_mode(spec)
    _mres = _aimeat_call(
        spec.agent_name, "aimeat_agent_mode_set", {"target_agent_name": spec.agent_name, "mode": _mode}
    )
    print(f"[{spec.agent_name}] set agent mode = {_mode}: {bool(_mres)}", file=sys.stderr)

    # 1) Ensure Hello Integration before the daemon (best-effort). SELF-HEALING: re-invoke the driver
    #    whenever onboarding is incomplete AND at least one pending REQUIRED step is drivable (has a
    #    howTo.tool). Driving those is idempotent + terminating, so a crew stranded at e.g. 4/7 (the
    #    mode re-derivation race left identify_platform / install_skill / publish_config pending) drives
    #    itself to 7/7 on the next restart — no manual dashboard action. The .attempt lock only guards
    #    the toolless case: when the sole remaining pending steps have no tool they can never pass, so we
    #    skip re-driving them (would loop forever) once we've already tried. A functional agent is
    #    authorized regardless; partial onboarding is fine.
    _ob_done, _ob_drivable = _onboarding_state(spec.agent_name)
    _ob_recent = _onboarding_attempted_recently(spec.agent_name)
    _ob_attempts = _onboarding_attempt_info(spec.agent_name)[0] if _ob_recent else 0
    # Drive Hello Integration on EVERY start while onboarding is incomplete and has drivable pending REQUIRED
    # steps (howTo.tool present). A crew start is a manual/supervised event (not a hot loop), the drive is
    # idempotent (only NON-passed steps run, ~seconds when there is nothing to do), and the old failure that
    # stranded new agents — complete_test_task shipping a `{test_task_id}` placeholder it could never
    # complete, so the drivable count never fell and the per-window try cap then gave up — is fixed at the
    # source (_resolve_test_task_id). Reliability beats sparing a few seconds per restart; a genuinely
    # tool-stuck step self-heals the instant its blocker clears. The check does NOT gate on `completable`
    # (the node reports that independently of the api_call steps). Fall back to the fresh-window drive too.
    if (_ob_drivable > 0 and not _ob_done) or (not _ob_done and not _ob_recent):
        _mark_onboarding_attempt(spec.agent_name, n_drivable=_ob_drivable, attempts=_ob_attempts + 1)
        _run_onboarding_only(spec.agent_name, services=spec.services, commands=spec.commands)

    # 1b) Publish the slash-command palette so the dashboard (Data Access) and the Messages UI
    #     can surface it. Key agents.<agent>.commands, owner-visible. Rewritten on every start
    #     (idempotent), so the commands appear even for an already-onboarded agent after a restart.
    if spec.commands:
        res = _aimeat_call(
            spec.agent_name,
            "aimeat_memory_write",
            {"key": f"agents.{spec.agent_name}.commands", "value": spec.commands, "visibility": "owner"},
        )
        print(
            f"[{spec.agent_name}] published {len(spec.commands)} commands to "
            f"agents.{spec.agent_name}.commands: {bool(res)}",
            file=sys.stderr,
        )

    # 1b1) Publish the PUBLIC chat-command palette (dev spec: key "chat.commands") so a PEER's inbox
    #      composer can render fill-in command chips. May be GENERATED dynamically from live state
    #      (callable). Best-effort — a bad/empty palette must never block the daemon.
    try:
        _raw_cmds = spec.chat_commands(spec.agent_name) if callable(spec.chat_commands) else spec.chat_commands
        _chat_cmds = _valid_chat_commands(_raw_cmds)
        if _chat_cmds:
            res = _aimeat_call(
                spec.agent_name,
                "aimeat_memory_write",
                {"key": "chat.commands", "value": {"v": 1, "commands": _chat_cmds}, "visibility": "public"},
            )
            print(
                f"[{spec.agent_name}] published {len(_chat_cmds)} chat.commands (public): {bool(res)}",
                file=sys.stderr,
            )
    except Exception as exc:  # noqa: BLE001 — the command palette is cosmetic; never break startup
        print(f"[{spec.agent_name}] chat.commands publish skipped: {exc!r}", file=sys.stderr)

    # Resolve the agent's tags + capabilities: the crew's own CrewSpec wins; otherwise fall back to
    # the curated fleet registry (so SPECIFIC identity is set fleet-wide without editing every crew).
    from crewaimeat.fleet_identity import identity_for

    _ident = identity_for(spec.agent_name)
    _tags = spec.tags if spec.tags is not None else _ident.get("tags")
    _caps = spec.capabilities if spec.capabilities is not None else _ident.get("capabilities")

    # 1b2) Set the agent's capability TAGS (idempotent, every start — so they survive re-onboarding)
    #      so the ecosystem-app agent picker recommends it by TAG, not only by exact name.
    if _tags:
        res = _aimeat_call(
            spec.agent_name,
            "aimeat_agent_tags_set",
            {"target_agent_name": spec.agent_name, "tags": list(_tags)},
        )
        print(f"[{spec.agent_name}] set capability tags {list(_tags)}: {bool(res)}", file=sys.stderr)

    # 1b3) Re-declare this agent's services (capabilities) on EVERY start — idempotent — so a plain
    #      restart refreshes them with no full re-onboard. The onboarding-only path declares them the
    #      first time; this keeps an already-onboarded agent's capabilities current (the ecosystem-app
    #      picker reads capabilities + tags), so newly-added services appear after a restart.
    if spec.services and _onboarding_completed(spec.agent_name):
        res = _aimeat_call(
            spec.agent_name,
            "aimeat_onboarding_declare_services",
            {"services": spec.services},
        )
        print(f"[{spec.agent_name}] re-declared {len(spec.services)} services: {bool(res)}", file=sys.stderr)

    # 1b4) Report this agent's SPECIFIC capabilities on every start (OVERWRITES the set) so the record
    #      advertises what it actually does — the picker's matcher reads technical + domain — instead of
    #      the liaison's generic onboarding defaults (AIMEAT integration / coordination, which are
    #      implied by completing Hello Integration anyway). Idempotent.
    if _caps:
        payload = {k: _caps[k] for k in ("technical", "domain", "languages") if _caps.get(k)}
        res = _aimeat_call(spec.agent_name, "aimeat_agent_capabilities_report", payload)
        print(f"[{spec.agent_name}] reported capabilities {list(payload)}: {bool(res)}", file=sys.stderr)

    # 1b5) Publish this agent's OFFERS (with golden samples) on EVERY start — idempotent — so the
    #      Tarjoama / "what can I do" surface shows a real last-run sample. Offers are otherwise only
    #      pushed by a manual publish_all, so a plain restart never refreshed them (the samples stayed
    #      'untested'). Only agents with an authored offer publish; best-effort — a publish failure is
    #      logged loud but never blocks the daemon start. Lazy import avoids an import cycle with offers.
    try:
        from crewaimeat.offers import CREW_AGENTS, PILOT_AGENTS, publish_meta_offer, publish_offers_any

        if spec.offer:
            # A forged / template crew advertises its ONE inline offer without a central offers.py entry.
            ok, detail = publish_meta_offer(spec.agent_name, spec.offer, with_sample=True)
            print(
                f"[{spec.agent_name}] inline offer {'published' if ok else 'publish failed: ' + detail}",
                file=sys.stderr,
            )
        elif spec.agent_name in CREW_AGENTS or spec.agent_name in PILOT_AGENTS:
            publish_offers_any(spec.agent_name, with_samples=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[{spec.agent_name}] offer publish skipped ({exc!r})", file=sys.stderr)

    # 1c) Publish the README (FIGLET / AVAILABLE_COMMANDS / LLM directives expanded). Every crew
    #     gets a README tab: the author's text if provided, otherwise a generated default so a
    #     forge-built crew (which passes no readme_md) is never blank.
    _publish_readme(
        spec.agent_name,
        spec.readme_md or _default_readme(spec.agent_name),
        spec.commands,
    )

    # 2) Per-task crew builder handed to the daemon.
    def _build(task: dict, liaison: Agent) -> Crew:
        tid = task.get("id")
        raw_prompt = task.get("description") or task.get("title") or ""

        # Onboarding SMOKE TEST TASK: the node creates an ordinary task during Hello Integration whose
        # sole purpose is to prove the agent can round-trip a task. It carries no special kind, but both
        # node creation sites stamp the SAME locale-independent marker. Do NOT run the domain crew on it
        # (that fabricates junk work) and do NOT let finalize mark invented "do the onboarding" todos done
        # (the "did it lie" bug). Ack it; the deterministic complete-callback closes it — its purpose (a
        # task round-trips through the subprocess) is satisfied without touching any onboarding todo.
        _verif = task.get("verification")
        if (
            isinstance(_verif, dict)
            and _verif.get("userExpects") == "Agent completes the onboarding test task successfully"
        ):
            # The smoke test's only pass criterion is that a task ROUND-TRIPS — which needs NO model. Complete
            # it DETERMINISTICALLY right here (_make_complete_cb is a plain aimeat_task_complete that ignores its
            # output arg), so a down/erroring LLM provider can't fail the probe. No domain crew, no LLM in the
            # completion, no invented "done" todos. The trivial crew below is ceremony the daemon requires; its
            # callback re-completes idempotently as a fallback if the direct call raised.
            print(
                f"[{spec.agent_name}] onboarding smoke test task {tid} -> deterministic complete (no crew/LLM/todos)",
                file=sys.stderr,
            )
            _complete = _make_complete_cb(spec.agent_name, tid, owner=spec.owner)
            try:
                _complete(None)
            except Exception as exc:  # noqa: BLE001 — a still-active task is simply re-dispatched on restart
                print(f"[{spec.agent_name}] smoke test deterministic complete raised: {exc!r}", file=sys.stderr)
            _noop = Agent(
                role="Onboarding Test",
                goal="Return ok.",
                backstory="You do nothing; the task is already complete.",
                llm=get_llm(for_tool_use=False, agent_name=spec.agent_name),
                verbose=False,
            )
            _noopt = Task(description="Output exactly: ok", expected_output="ok", agent=_noop)
            _noopt.callback = _complete  # idempotent fallback (only matters if the direct complete above failed)
            return Crew(agents=[_noop], tasks=[_noopt], process=Process.sequential, verbose=False, cache=False)

        # Self-evolution: if this message is the owner's CLICK on one of our own evolution prompts,
        # handle it here (diagnose / build / dismiss) and reply in our thread — do NOT run the domain
        # crew (joker must not "tell a joke" about the answer). Return a trivial crew so the daemon
        # completes the message; handle_evolve_answer already sent the staged reply.
        if spec.self_monitor:
            from crewaimeat.evolve import handle_evolve_answer, is_evolve_answer

            _pa = is_evolve_answer(task, raw_prompt)
            if _pa is not None:
                print(f"[{spec.agent_name}] handling own evolution answer: {_pa.get('choice')!r}", file=sys.stderr)
                try:
                    handle_evolve_answer(spec.agent_name, _pa, spec.owner)
                except Exception as exc:  # noqa: BLE001 — never break the daemon
                    print(f"[{spec.agent_name}] evolve-answer handling failed: {exc}", file=sys.stderr)
                _ack = Agent(
                    role="Self-monitor",
                    goal="Acknowledge a handled control message tersely",
                    backstory="You quietly acknowledge internal control messages.",
                    llm=get_llm(for_tool_use=False, agent_name=spec.agent_name),
                    verbose=False,
                )
                _at = Task(description="Output exactly: ok", expected_output="ok", agent=_ack)
                return Crew(agents=[_ack], tasks=[_at], process=Process.sequential, verbose=False, cache=False)

        # A coordinator may ask us to also publish into a shared tag area it can read.
        shared_key, shared_tag, prompt = _parse_publish_directive(raw_prompt)
        verify_override, prompt = _parse_verify_directive(prompt)
        # Task-nature gate: fact work runs cool + grounded + faithfulness-verified; creative runs warm.
        gate = (
            _classify_task_nature(prompt, get_llm(for_tool_use=False, agent_name=spec.agent_name))
            if spec.adapt_to_task
            else None
        )
        if gate:
            print(
                f"[{spec.agent_name}] task nature={gate['nature']} temp={gate['temperature']} verify={gate['verify']}",
                file=sys.stderr,
            )
        # A crew-level enforced temperature wins over the per-task gate (a creative service declares its
        # warmth once); otherwise use the gate's temp, otherwise the .env default.
        _temp = spec.temperature if spec.temperature is not None else (gate["temperature"] if gate else None)
        if spec.temperature is not None:
            print(
                f"[{spec.agent_name}] enforced temperature={spec.temperature} (creative-nature crew)", file=sys.stderr
            )
        # Per-crew provider routing: the domain agents (ctx.llm) use this crew's profile in llm_providers.json
        # (e.g. content crews -> grok, code crews -> a real coder).
        llm = (
            get_llm(temperature=_temp, agent_name=spec.agent_name)
            if _temp is not None
            else get_llm(agent_name=spec.agent_name)
        )
        verify_mode = verify_override or (gate["verify"] if gate else None) or spec.verify
        mem_key = _memory_key(spec.agent_name, spec.memory_key_prefix, {"id": tid, "description": prompt})
        print(
            f"[{spec.agent_name}] build crew for task {tid} -> key {mem_key}"
            + (f" (+ shared {shared_key})" if shared_key else ""),
            file=sys.stderr,
        )

        # Bind the progress bridge: kickoff starts a 5s heartbeat writing live status.
        progress.bind(tid, prompt[:80])

        # Owner-set directives (GET /v1/agents/me/directives) bind this run's behavior. Fetched per
        # task so an owner edit takes effect on the next task with no restart.
        directives = _format_directives(_fetch_directives(spec.agent_name, spec.owner))
        if directives:
            print(
                f"[{spec.agent_name}] applying owner directives ({directives.count(chr(10))} line(s))", file=sys.stderr
            )
        # Factual work also gets the grounding rule (no invented specifics, honest gaps) prepended.
        if gate and gate["ground"]:
            directives = _GROUNDING_RULE + ("\n\n" + directives if directives else "")

        ctx = BuildContext(
            task=task,
            prompt=prompt,
            llm=llm,
            today=_now_context(),
            directives=directives,
            offer=_resolve_offer(spec.agent_name, task),
        )
        agents, tasks = spec.build_domain(ctx)

        # Optional verification pass (MAST FM-3.2): a Reviewer checks the deliverable against the goal
        # and FIXES any gap, producing the final deliverable. ONE pass, no loop — so it cannot
        # reintroduce step-repetition (FM-1.3). Enabled by CrewSpec.verify="on" or a <<VERIFY>> task
        # directive; becomes the new last domain task, so publish/directives attach to it below.
        if verify_mode in ("on", "factcheck") and tasks:
            reviewer = Agent(
                role="Deliverable Reviewer",
                goal="Verify the deliverable and return a corrected, trustworthy final version",
                backstory=(
                    "You are a sharp, skeptical reviewer. You never rubber-stamp; you check the work hard and "
                    "FIX what is wrong, but you never add anything that is not supported."
                ),
                llm=llm,
                verbose=True,
            )
            if verify_mode == "factcheck":
                # Faithfulness check (RAGAS/QAFactEval + CoVe style): every claim must appear in the CREW
                # CONTRIBUTIONS in context — invented specifics get removed, never dressed as fact. The
                # contributions are the source of truth; the Reviewer does NOT have (and must not ask for or
                # refuse over) the original external web pages. It always returns the deliverable itself.
                verify_desc = (
                    f"Original goal:\n{prompt}\n\n"
                    "Your context above contains the CREW CONTRIBUTIONS the deliverable was built from, then the "
                    "deliverable itself. Fact-check the deliverable AGAINST THOSE CONTRIBUTIONS — they are your "
                    "only source of truth. You do NOT have the original external web pages/articles, and you must "
                    "NOT ask for them or refuse for lack of them. A claim is SUPPORTED if it appears in the "
                    "contributions (a source named/cited WITHIN the contributions counts as support). A claim is "
                    "UNSUPPORTED only if it is not in the contributions at all — an invented name, number, date, "
                    "organisation, or citation. Work claim by claim: remove or mark '[unverified]' anything not in "
                    "the contributions; never invent; never attach a citation that is not in the contributions; do "
                    "not add anything new. If only one crew contributed, return its content faithfully. ALWAYS "
                    "output the corrected deliverable ITSELF — never a commentary about your process or about "
                    "missing materials — ending with EXACTLY this line: "
                    "'Verify: faithfulness | score=<1-5> | unsupported=<N> | <short note>' "
                    "(score 5 = every specific is in the contributions; 1 = several invented specifics)."
                )
            else:
                verify_desc = (
                    f"Original goal:\n{prompt}\n\n"
                    "Review the deliverable produced above against this goal. Does it fully answer the goal? "
                    "Is anything missing, incorrect, unsupported, or off-target versus the expected output? "
                    "If it is complete and correct, return it verbatim. If not, CORRECT it and return the "
                    "final, complete deliverable. End with one line: 'Verify: pass' or 'Verify: fixed - <what>'."
                )
            verify_task = Task(
                description=verify_desc,
                expected_output="The final, verified deliverable (corrected if needed), ending with a one-line Verify note.",
                agent=reviewer,
                context=list(tasks),
            )
            agents = [*agents, reviewer]
            tasks = [*tasks, verify_task]

        # Prepend the directives to every domain task so the agent that produces the deliverable
        # also sees them (not just the first task). The finalize task is added after this and stays
        # deterministic.
        if directives:
            for _t in tasks:
                _t.description = f"{directives}\n\n---\n\n{_t.description}"

        # Capture the run's eval-context (model + temperature from the actual LLM; tokens from the
        # crew's usage_metrics via a holder populated by kickoff). Published beside the deliverable so
        # a coordinator can attribute its rating to the right model/cost (AIMEAT rate metadata).
        _short = (tid or "manual").split("-", 1)[0] if tid else "manual"
        _crew_holder: dict = {}
        eval_info = {
            "model": getattr(llm, "model", None),
            "temperature": getattr(llm, "temperature", None),
            "nature": gate["nature"] if gate else None,
            "task": _short,
            "crew_holder": _crew_holder,
            "custom_key": f"agents.{spec.agent_name}.statistics.custom.eval_context",
        }

        # Guarantee the deliverable lands even if the liaison's LLM memory_write loops/errors:
        # publish the LAST domain task's output deterministically via its callback (chained so an
        # author-set callback still runs).
        if tasks:
            _author_cb = getattr(tasks[-1], "callback", None)
            _publish = _make_publish_cb(
                spec.agent_name,
                mem_key,
                shared_key,
                shared_tag,
                eval_info,
                task_id=tid,
                clean=spec.clean_deliverable,
                offer_id=(ctx.offer or {}).get("id"),
            )

            def _last_cb(out, _pub=_publish, _prev=_author_cb):
                _pub(out)
                if _prev:
                    try:
                        _prev(out)
                    except Exception:  # noqa: BLE001
                        pass

            tasks[-1].callback = _last_cb

            # Optional: contribute this deliverable to the agent's library so the librarian can index
            # it (classified by topic + shelf-life, junk dropped). Chained after publish; best-effort.
            if spec.contribute_to_library:
                _pub_cb = tasks[-1].callback

                def _lib_cb(out, _prev=_pub_cb, _key=mem_key):
                    if _prev:
                        try:
                            _prev(out)
                        except Exception:  # noqa: BLE001
                            pass
                    try:
                        from crewaimeat.librarian import contribute_deliverable  # local: avoid import cycle

                        text = getattr(out, "raw", None) or str(out)
                        contribute_deliverable(spec.agent_name, _key, text)
                    except Exception as exc:  # noqa: BLE001
                        print(f"[{spec.agent_name}] library contribute skipped: {exc}", file=sys.stderr)

                tasks[-1].callback = _lib_cb

            # Optional: persist the factcheck Reviewer's faithfulness score to agents.stats.* (the
            # reputation convention). Validated by POC v2 — source-grounded judging discriminates
            # faithful from confabulated. tasks[-1] is the verify task, so its output carries the score.
            if spec.score_to_stats and verify_mode == "factcheck":
                _prev_cb2 = tasks[-1].callback
                _dim = gate["nature"] if gate else "general"

                def _score_cb(out, _prev=_prev_cb2, _dim=_dim):
                    if _prev:
                        try:
                            _prev(out)
                        except Exception:  # noqa: BLE001
                            pass
                    try:
                        text = getattr(out, "raw", None) or str(out)
                        _write_verify_stat(spec.agent_name, tid, text, _dim)
                    except Exception as exc:  # noqa: BLE001
                        print(f"[{spec.agent_name}] verify-score skipped: {exc}", file=sys.stderr)

                tasks[-1].callback = _score_cb

        if task.get("_source") == "message":
            original = task.get("_original") or {}
            sender = original.get("from") or original.get("sender") or original.get("from_agent")
            finalize = _finalize_message_task(spec.agent_name, mem_key, sender, liaison)
        else:
            finalize = _finalize_task(spec.agent_name, tid, mem_key, liaison)
            # Guarantee the task is closed even if the liaison never calls aimeat_task_complete; when
            # require_verify_pass is set, the close is GATED on the app verify gates' outcome (SYS-1).
            finalize.callback = _make_complete_cb(
                spec.agent_name,
                tid,
                mem_key=mem_key,
                require_verify=spec.require_verify_pass,
                owner=spec.owner,
                auto_revert=spec.auto_revert_on_fail,
            )

        # Self-evolution monitor (doc 20 P1): after the task, read own reputation and, if a gated
        # signal fires, propose an evolution to the owner. Chained after finalize; best-effort.
        if spec.self_monitor:
            _prev_fin = getattr(finalize, "callback", None)

            def _monitor_cb(out, _prev=_prev_fin):
                if _prev:
                    try:
                        _prev(out)
                    except Exception:  # noqa: BLE001
                        pass
                try:
                    from crewaimeat.evolve import self_monitor_check  # local: avoid import cycle

                    self_monitor_check(spec.agent_name, spec.owner)
                except Exception as exc:  # noqa: BLE001 — monitoring must never break the task
                    print(f"[{spec.agent_name}] self-monitor skipped: {exc}", file=sys.stderr)

            finalize.callback = _monitor_cb

        # Optional wall-clock runaway bound (field finding 2026-06-05: safer than lowering max_iter —
        # it kills a STUCK loop without truncating a long-but-progressing build). Off unless
        # AIMEAT_AGENT_MAX_EXECUTION_TIME is set; never overrides an agent that declared its own.
        _met = _runtime_max_execution_time()
        if _met is not None:
            for _a in agents:
                if getattr(_a, "max_execution_time", None) in (None, 0):
                    try:
                        _a.max_execution_time = _met
                    except Exception:  # noqa: BLE001 — never break the build if the model rejects the assignment
                        pass

        crew_kwargs: dict[str, Any] = {
            "agents": [liaison, *agents],
            "tasks": [*tasks, finalize],
            "process": spec.process,
            "verbose": True,
            "cache": False,
        }
        if spec.manager_agent is not None:
            crew_kwargs["manager_agent"] = spec.manager_agent
        # Opt-in CrewAI memory: hand Crew a scoped Memory (its analysis LLM = this crew's chain, embedder =
        # cascade, storage = per-instance scoped path). Fails loud if no embedder is reachable.
        if spec.memory:
            crew_kwargs["memory"] = _build_crew_memory(spec, task)
        crew = Crew(**crew_kwargs)
        _crew_holder["crew"] = crew  # so the publish callback can read usage_metrics after kickoff
        return crew

    # Idle work runs between pushed tasks. NB the daemon now SELF-EXITS on a revoked token (aimeat-crewai
    # 0.7.0: the node pushes auth_revoked, the connector reports transport auth_failed via /local/status,
    # run_crew_daemon stops) — so we no longer probe auth here; the supervisor re-auths on the daemon's exit.
    pub = {"last": 0.0, "sig": None}
    hook = {"last": 0.0}

    def _on_idle() -> None:
        now = time.time()
        # Self-publish this crew's field-reputation rollup so coordinators' discover_crews see its live
        # score. Throttled to ~10 min, CONDITIONAL (writes only when the score moved), over the tunnel tool.
        if now - pub["last"] > 600:
            pub["last"] = now
            _publish_selection_rollup(spec.agent_name, spec.owner, _state=pub)
        # Optional DETERMINISTIC per-crew idle work (e.g. a CLOCK check). NO LLM; throttled. Record-driven
        # contracts use listen_for="records" + on_record instead of polling here.
        if spec.idle_hook is not None and now - hook["last"] > spec.idle_hook_seconds:
            hook["last"] = now
            try:
                spec.idle_hook()
            except Exception as exc:  # noqa: BLE001
                print(f"[{spec.agent_name}] idle_hook failed: {exc!r}", file=sys.stderr)

    # Daemon: receive pushed tasks/messages/records, execute the per-task crew. llm=get_llm() keeps the
    # daemon's liaison on the configured model (not CrewAI's OpenAI default).
    # self_monitor crews must also hear inbox messages — that's how the owner's click on an
    # evolution prompt comes back (AIMEAT routes the answer to the agent that sent the prompt).
    # dm_serviceable: make this crew DM-callable. A generic on_dm runs the crew's OWN build_domain on the
    # DM body and replies in-thread — so any agent/person can DM a request and get the deliverable back.
    _on_dm = spec.on_dm
    if spec.dm_serviceable and _on_dm is None:
        from crewaimeat import dm as _dm

        _svc_seen: set = set()

        def _service_responder(event: dict):
            mid, _conv, _sender, body, _subj = _dm._inbound_fields(event)
            if not str(body or "").strip():
                return ""
            ctx = BuildContext(
                task={"id": f"dm-{mid}", "title": "DM request", "description": body},
                prompt=str(body),
                llm=get_llm(agent_name=spec.agent_name),
                today=_now_context(),
            )
            # Build memory BEFORE the try so a missing-embedder RuntimeError fails LOUD (a memory
            # misconfiguration must surface, not hide behind the generic per-DM apology below). Memory on
            # the DM path isolates by the SENDER's ghii so each federation requester keeps a separate
            # memory (no cross-peer bleed) — the cross-owner privacy boundary.
            _dm_mem = (
                _build_crew_memory(spec, {"id": f"dm-{mid}", "_source": "dm", "_dm_sender": _sender})
                if spec.memory
                else None
            )
            try:
                agents, tasks = spec.build_domain(ctx)
                _dm_kwargs: dict[str, Any] = dict(agents=agents, tasks=tasks, process=spec.process, verbose=False)
                if _dm_mem is not None:
                    _dm_kwargs["memory"] = _dm_mem
                result = Crew(**_dm_kwargs).kickoff()
            except Exception as exc:  # noqa: BLE001
                print(f"[{spec.agent_name}] DM service crew failed: {exc!r}", file=sys.stderr)
                return "Sorry — I couldn't complete that request."
            return {"text": str(result)[:6000]}

        def _on_dm(e):  # noqa: ANN001
            return _dm.handle_dm_event(spec.agent_name, e, _service_responder, seen=_svc_seen)

    _listen = tuple(spec.listen_for)
    if spec.dm_serviceable and "dms" not in _listen:
        _listen = _listen + ("dms",)
    if spec.self_monitor and "messages" not in _listen:
        _listen = _listen + ("messages",)
    # record_spaces may be a 0-arg callable (resolved here at daemon start, e.g. discovers member workspaces).
    _records = spec.record_spaces() if callable(spec.record_spaces) else spec.record_spaces

    # Tool filter for the daemon liaison: the default ~24-tool allowlist, plus aimeat_discover (the master
    # directory) when this crew opts in. The 0.10.0 liaison backstory steers it to discover first; without
    # this the tool would be filtered out and that steering would point at a tool the liaison can't load.
    _tool_filter: Any = _liaison_tool_filter(spec.discover)
    if spec.discover:
        print(f"[{spec.agent_name}] discover ON -> liaison tool_filter += aimeat_discover", file=sys.stderr)

    # Wait for the supervisor's shared serve daemon to be live before binding the daemon loop. A crew
    # never spawns it (single-spawner discipline), but riding out a transient restart/tunnel-drop beats
    # hard-crashing on AimeatServeError and burning the per-crew watchdog's quick-exit budget (the
    # appliance startup crash-loop). If the bridge still drops in the gap before run_crew_daemon's own
    # ensure_serve, re-wait and retry a few times rather than exiting the process.
    _wait_for_serve(spec.agent_name)
    _serve_attempt = 0
    while True:
        try:
            run_crew_daemon(
                agent_name=spec.agent_name,
                build_crew=_build,
                poll_interval_seconds=spec.poll_seconds,
                tool_filter=_tool_filter,
                listen_for=_listen,
                record_spaces=_records,  # subscribe to workspace-record PUSH events for these spaces (0.7.0)
                on_record=spec.on_record,  # handler for a pushed record event (or None -> synthetic task)
                on_dm=_on_dm,  # federated-inbox DM wake: caller's on_dm, else the dm_serviceable generic, else None
                llm=get_llm(agent_name=spec.agent_name),
                owner=spec.owner,
                on_idle=_on_idle,
                max_concurrent_tasks=spec.max_concurrent_tasks,  # None = read owner-set value from AIMEAT
                serve_options={"auto_start": False},  # crews never spawn the daemon — only start_fleet does
            )
            return
        except AimeatServeError as exc:
            _serve_attempt += 1
            if _serve_attempt > SERVE_DAEMON_RETRIES:
                raise
            print(
                f"[{spec.agent_name}] serve bridge dropped at daemon start ({exc}); re-waiting for the "
                f"supervisor and retrying ({_serve_attempt}/{SERVE_DAEMON_RETRIES}).",
                file=sys.stderr,
            )
            _wait_for_serve(spec.agent_name)

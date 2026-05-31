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
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

for _s in (sys.stdout, sys.stderr):
    _r = getattr(_s, "reconfigure", None)
    if _r:
        _r(encoding="utf-8")

import requests  # noqa: E402 — ships with aimeat-crewai

from crewai import Agent, Crew, Process, Task  # noqa: E402
from aimeat_crewai import create_liaison_agent, run_crew_daemon, stdio_params  # noqa: E402
from aimeat_crewai.daemon import DAEMON_DEFAULT_TOOL_FILTER  # noqa: E402

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

    task: dict          # the raw AIMEAT task (id, title, description, ...)
    prompt: str         # task.description or task.title — the user's actual request
    llm: Any            # the shared LLM (crewaimeat.llm.get_llm); pass to your Agents
    today: str          # current-time context string — prepend to time-sensitive tasks
    directives: str = ""  # owner-set behavioral directives (GET /v1/agents/me/directives),
    #   already formatted. The scaffold also prepends these to every domain task, so they bind
    #   behavior automatically; reference ctx.directives only if you want finer placement.


# build_domain returns (agents, tasks). Tasks run in `process` order; the LAST
# task's output is what the liaison publishes to AIMEAT memory.
BuildDomain = Callable[[BuildContext], "tuple[list[Agent], list[Task]]"]


@dataclass
class CrewSpec:
    """Declares one AIMEAT-connected crew. Only `agent_name` + `build_domain` are required."""

    agent_name: str                       # the AIMEAT agent identity (from `aimeat connect add`)
    build_domain: BuildDomain             # returns (domain_agents, domain_tasks)
    process: Any = Process.sequential     # sequential is the validated path; hierarchical is advanced
    poll_seconds: int = 30                # daemon poll interval
    memory_key_prefix: str | None = None  # default: crews.<agent_name>
    manager_agent: Any = None             # only for Process.hierarchical
    owner: str | None = None              # AIMEAT owner; set only if the agent name is ambiguous
    max_idle_auth_failures: int = 10      # idle cycles with a rejected token before exiting for re-auth
    listen_for: Iterable[str] = ("tasks",)  # add "messages" to also act on inbox messages
    wait_for_approval_seconds: int | None = 900  # wait this long for the token to be approved
    #   before onboarding, then exit for re-auth (None = wait indefinitely)
    services: list[dict] | None = None    # {name, description} capabilities to declare at
    #   onboarding via aimeat_onboarding_declare_services
    commands: list[dict] | None = None    # slash-command palette [{name, description, category}, ...]
    #   published to memory key agents.<agent>.commands (owner) so the Messages UI surfaces it
    readme_md: str | None = None          # markdown for the agent's README tab; may contain
    #   [[FIGLET[:font]]["text"]] (deterministic ASCII-art) and [[LLM]["prompt"]] (LLM output)
    #   directives expanded at publish time. Written to agents.<agent>.readme (owner).
    #   Expanded only when the text changes (cached).


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
    rules = [
        r for r in (data.get("rules") or [])
        if isinstance(r, dict) and (r.get("description") or "").strip()
    ]
    if not purpose and not rules:
        return ""
    lines = ["STANDING DIRECTIVES (owner-set policy — follow these in everything you produce):"]
    if purpose:
        lines.append(f"- Purpose: {purpose}")
    label = {"system": "policy", "owner": "policy", "agent": "standing"}
    for r in rules:
        lines.append(f"- [{label.get(r.get('source'), 'rule')}] {r.get('description', '').strip()}")
    return "\n".join(lines)


def _token_exists(agent_name: str, owner: str | None) -> bool:
    """True if the connector has written a token file for this agent yet.

    Mirrors aimeat_crewai's keychain layout (~/.aimeat/tokens/{agent}@{owner}.token). Before
    the owner approves a freshly-registered agent there is no token, so the daemon's
    _read_token would raise at startup — we use this to wait for registration/approval first.
    """
    import glob

    home = Path(os.environ.get("AIMEAT_HOME") or (Path.home() / ".aimeat"))
    tokens = home / "tokens"
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


def _aimeat_call(agent_name: str, tool: str, payload: dict) -> dict | None:
    """Deterministic AIMEAT call via the connector CLI (no LLM). Windows: cmd /c."""
    if shutil.which("aimeat") is None:
        return None
    base = ["aimeat", "connect", "call", tool, "--agent", agent_name, "--stdin"]
    cmd = ["cmd", "/c", *base] if os.name == "nt" else base
    try:
        proc = subprocess.run(
            cmd, input=json.dumps(payload), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=90,
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


def _onboarding_completed(agent_name: str) -> bool:
    data = _aimeat_call(agent_name, "aimeat_onboarding_status", {})
    return bool(data) and data.get("onboarding", {}).get("status") == "completed"


def _memory_key(agent_name: str, prefix: str | None, task: dict) -> str:
    tid = task.get("id") or "manual"
    short = tid.split("-", 1)[0] if "-" in tid else tid[:8]
    text = task.get("description") or task.get("title") or ""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:32].strip("-")
    token = f"{slug}-{short}" if slug else short
    base = prefix or f"crews.{agent_name}"
    return f"{base}.{token}.latest_output"


def _run_onboarding_only(agent_name: str, services: list[dict] | None = None) -> None:
    """One-shot Hello Integration (liaison alone, no domain work)."""
    print(
        f"[{agent_name}] Hello Integration not done -> running ONBOARDING ONLY "
        "(liaison alone, no domain work).",
        file=sys.stderr,
    )
    if services:
        services_step = (
            "3. Declare this agent's services so they are discoverable: call "
            f"aimeat_onboarding_declare_services with services={json.dumps(services, ensure_ascii=False)} "
            "(do this even if it is not listed as a pending step).\n"
        )
    else:
        services_step = ""
    with create_liaison_agent(
        mcp_server_params=stdio_params(agent_name=agent_name),
        agent_name=agent_name,
        llm=get_llm(),
        tool_filter=DAEMON_DEFAULT_TOOL_FILTER,  # ~24 tools, not 95 (smaller models cope)
        verbose=True,
    ) as liaison:
        task = Task(
            description=(
                "Complete AIMEAT Hello Integration. Work carefully and in order — do NOT "
                "rush, and do NOT fire several tool calls in the same turn.\n"
                "1. aimeat_onboarding_status to see pending steps.\n"
                "2. Complete each pending step with its matching aimeat_onboarding_* tool.\n"
                f"{services_step}"
                "4. Test task: aimeat_task_propose_todos ONCE, then mark TODOs done with "
                "aimeat_task_todo ONE AT A TIME (wait for each result). Then you MUST call "
                "aimeat_task_complete with the test task's id to complete it. Do NOT re-mark "
                "done TODOs.\n"
                "5. aimeat_onboarding_status once more and report. No domain work."
            ),
            expected_output="All onboarding steps passed; test task completed.",
            agent=liaison,
        )
        Crew(
            agents=[liaison], tasks=[task], process=Process.sequential, verbose=True, cache=False
        ).kickoff()
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


def _parse_publish_directive(text: str) -> "tuple[str | None, str | None, str]":
    """Return (shared_key, tag, cleaned_text) from a task description carrying a publish marker."""
    m = _PUBLISH_DIRECTIVE.search(text or "")
    if not m:
        return None, None, text
    cleaned = (text[: m.start()] + text[m.end():]).strip()
    return m.group(1), (m.group(2) or None), cleaned


def _make_publish_cb(agent_name: str, primary_key: str, shared_key: str | None = None, tag: str | None = None):
    """Task callback: write the task output to AIMEAT memory deterministically (no LLM).

    Attached to the last DOMAIN task so the deliverable always lands, even if the liaison's
    LLM-driven memory_write loops or errors (observed on weaker models). Always writes the agent's
    own key; if a shared_key/tag are supplied (a delegated workflow subtask), ALSO writes into the
    shared tag area so the coordinator can collect it with its own scope."""
    def _cb(task_output) -> None:
        text = getattr(task_output, "raw", None)
        if text is None:
            text = str(task_output)
        r1 = _aimeat_call(
            agent_name, "aimeat_memory_write", {"key": primary_key, "value": text, "visibility": "owner"}
        )
        print(f"[{agent_name}] deliverable published -> {primary_key}: {bool(r1)}", file=sys.stderr)
        if shared_key:
            r2 = _aimeat_call(
                agent_name,
                "aimeat_memory_write",
                {"key": shared_key, "value": text, "visibility": "owner", "tags": [tag] if tag else []},
            )
            print(f"[{agent_name}] deliverable shared -> {shared_key} (tag {tag}): {bool(r2)}", file=sys.stderr)

    return _cb


def _make_complete_cb(agent_name: str, tid: str):
    """Task callback: close the AIMEAT task deterministically (no LLM). Attached to the finalize
    task so the task is completed even if the liaison never calls aimeat_task_complete."""
    def _cb(_task_output) -> None:
        res = _aimeat_call(
            agent_name,
            "aimeat_task_complete",
            {"task_id": tid, "message": "Crew finished; deliverable published to memory."},
        )
        print(f"[{agent_name}] task completed deterministically {tid}: {bool(res)}", file=sys.stderr)

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
        expected_output=f"Result written to memory '{mem_key}'" + (f" and a reply sent to '{sender}'." if sender else "."),
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
    rows = "\n".join(
        f"| `{c.get('name', '')}` | {c.get('description', '')} |" for c in commands
    )
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


def _figlet_repl(m: "re.Match[str]") -> str:
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

    def _repl(m: "re.Match[str]") -> str:
        prompt = _unquote(m.group(1))
        if not prompt:
            return ""
        try:
            out = llm.call([
                {
                    "role": "system",
                    "content": (
                        "Output ONLY the requested content (e.g. the raw ASCII art or text). "
                        "No explanation, no preamble, no surrounding code fences unless asked."
                    ),
                },
                {"role": "user", "content": prompt},
            ])
            return (out or "").strip("\n")
        except Exception as exc:  # noqa: BLE001
            return f"[[LLM directive failed: {exc}]]"

    return _LLM_DIRECTIVE.sub(_repl, text)


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
        expanded = _expand_readme(readme_md, get_llm(for_tool_use=False), commands)
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


def run_crew(spec: CrewSpec) -> None:
    """Entry point: ensure onboarding once, then run the daemon forever.

    The daemon polls the AIMEAT queue; for each active task it builds a crew of
    [liaison, *your domain agents] with tasks [*your domain tasks, finalize] and
    runs it. Stop with Ctrl+C.
    """
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

    # 1) Ensure Hello Integration once (one-shot) before the daemon.
    if not _onboarding_completed(spec.agent_name):
        _run_onboarding_only(spec.agent_name, services=spec.services)

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
        llm = get_llm()
        tid = task.get("id")
        raw_prompt = task.get("description") or task.get("title") or ""
        # A coordinator may ask us to also publish into a shared tag area it can read.
        shared_key, shared_tag, prompt = _parse_publish_directive(raw_prompt)
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
            print(f"[{spec.agent_name}] applying owner directives ({directives.count(chr(10))} line(s))", file=sys.stderr)

        ctx = BuildContext(task=task, prompt=prompt, llm=llm, today=_now_context(), directives=directives)
        agents, tasks = spec.build_domain(ctx)

        # Prepend the directives to every domain task so the agent that produces the deliverable
        # also sees them (not just the first task). The finalize task is added after this and stays
        # deterministic.
        if directives:
            for _t in tasks:
                _t.description = f"{directives}\n\n---\n\n{_t.description}"

        # Guarantee the deliverable lands even if the liaison's LLM memory_write loops/errors:
        # publish the LAST domain task's output deterministically via its callback (chained so an
        # author-set callback still runs).
        if tasks:
            _author_cb = getattr(tasks[-1], "callback", None)
            _publish = _make_publish_cb(spec.agent_name, mem_key, shared_key, shared_tag)

            def _last_cb(out, _pub=_publish, _prev=_author_cb):
                _pub(out)
                if _prev:
                    try:
                        _prev(out)
                    except Exception:  # noqa: BLE001
                        pass

            tasks[-1].callback = _last_cb

        if task.get("_source") == "message":
            original = task.get("_original") or {}
            sender = original.get("from") or original.get("sender") or original.get("from_agent")
            finalize = _finalize_message_task(spec.agent_name, mem_key, sender, liaison)
        else:
            finalize = _finalize_task(spec.agent_name, tid, mem_key, liaison)
            # Guarantee the task is closed even if the liaison never calls aimeat_task_complete.
            finalize.callback = _make_complete_cb(spec.agent_name, tid)

        crew_kwargs: dict[str, Any] = {
            "agents": [liaison, *agents],
            "tasks": [*tasks, finalize],
            "process": spec.process,
            "verbose": True,
            "cache": False,
        }
        if spec.manager_agent is not None:
            crew_kwargs["manager_agent"] = spec.manager_agent
        return Crew(**crew_kwargs)

    # 3) Idle auth-guard: run_crew_daemon's _poll_tasks swallows a 401 and returns []
    #    (a dead token looks exactly like an empty queue), so the daemon would idle
    #    forever. On each idle cycle we probe auth; after `max_idle_auth_failures`
    #    consecutive rejections we exit with AUTH_EXIT_CODE so the user re-approves the
    #    agent on AIMEAT. (SystemExit escapes the daemon's `except Exception` on_idle
    #    guard, so this cleanly stops the loop.)
    auth = {"fails": 0}

    def _on_idle() -> None:
        alive = _auth_alive(spec.agent_name, spec.owner)
        if alive is True:
            auth["fails"] = 0
        elif alive is False:
            auth["fails"] += 1
            print(
                f"[{spec.agent_name}] token rejected by the node "
                f"({auth['fails']}/{spec.max_idle_auth_failures} consecutive idle checks)",
                file=sys.stderr,
            )
            if auth["fails"] >= spec.max_idle_auth_failures:
                print(
                    f"\n[{spec.agent_name}] The agent's token is no longer valid. Re-approve / "
                    "re-authenticate it on AIMEAT (Profile -> Agents), then start the crew again.",
                    file=sys.stderr,
                )
                raise SystemExit(AUTH_EXIT_CODE)
        # alive is None -> unknown/transient; leave the counter unchanged.

    # 4) Daemon: poll the queue, execute the per-task crew. llm=get_llm() keeps the
    #    daemon's liaison on the configured model (not CrewAI's OpenAI default).
    run_crew_daemon(
        agent_name=spec.agent_name,
        build_crew=_build,
        poll_interval_seconds=spec.poll_seconds,
        listen_for=tuple(spec.listen_for),
        llm=get_llm(),
        owner=spec.owner,
        on_idle=_on_idle,
    )

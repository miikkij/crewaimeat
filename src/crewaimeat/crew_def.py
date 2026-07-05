"""Declarative crew definitions — a crew as DATA, interpreted (never exec'd) into CrewAI objects.

Today a crew is Python: ``crews/<name>_crew.py`` defines ``build_domain(ctx)`` that constructs
``crewai.Agent``/``Task`` objects in code, and crew-forge / the agency generator emit that Python as a
STRING and ``exec`` it — fragile, and impossible to validate before it runs live. This module is the
foundation for the other path: define the crew as a JSON document and hand it to a single generic
interpreter that builds the SAME agents/tasks, with NO ``exec`` of generated code.

Three pure entry points (all fail LOUD at the boundary — a bad def is rejected before construction):

- ``validate_crew_doc(doc) -> [errors]`` — catches problems a human/LLM author makes (unknown tool,
  a task pointing at a missing agent, a non-DAG ``context`` edge, a bad ``{{ctx.x}}`` placeholder, a
  malformed signal) BEFORE any object is built.
- ``build_domain_from_json(doc, ctx) -> ([Agent], [Task])`` — the interpreter. Drop-in replacement for a
  hand-written ``build_domain``: resolve ``tools`` names to the real ``make_*_tools`` factories, inject
  ``{{ctx.prompt}}`` / ``{{ctx.today}}`` into task descriptions, and resolve each task's ``context`` to
  the earlier ``Task`` objects. Every agent uses ``ctx.llm`` (already routed for this agent by
  ``run_crew`` via ``get_llm``), exactly like the Python crews.
- ``crewspec_from_json(doc) -> CrewSpec`` — wrap a doc as a runnable ``CrewSpec`` (its ``build_domain``
  is this interpreter bound to the doc), carrying the crew-level fields (temperature / process / tags /
  capabilities / offers / readme) so the doc is a self-contained, runnable crew definition.

This is ADDITIVE: existing Python crews keep working untouched. The Python ``build_domain`` stays the
escape hatch for exotic crews (dynamic memory blocks, custom hooks) that this declarative v1 doesn't
express. Later phases (crew-forge emitting JSON, AIMEAT install/registry) build ON this, not into it.

Scope notes (Phase 1):
- ``llm_profile`` is DECLARATIVE here. ``get_llm`` routes by ``agent_name`` (via the ``crews`` map in
  ``llm_providers.json``), and ``ctx.llm`` is already that routed LLM — so the interpreter binds
  ``ctx.llm``. ``llm_profile`` is validated (must name a real profile when a providers file is present)
  and carried for discovery; ACTUALLY enforcing routing means adding ``agent_name -> profile`` to
  ``llm_providers.json`` at registration, which is a later phase.
- ``ctx.directives`` are NOT injected here — ``run_crew`` prepends them to every domain task itself
  after ``build_domain`` returns; ``{{ctx.directives}}`` is offered only for finer manual placement.
"""

from __future__ import annotations

import json
import re
from functools import partial
from pathlib import Path
from typing import Any

from aimeat_crewai.workflow_spec import SignalError, validate_signal

# The only ctx.* attributes a task description may template. `directives` is auto-prepended by
# run_crew, so it's here only for authors who want finer placement; `prompt`/`today` are the
# common ones (a missing ctx.prompt is the classic "agent drifts to a guessed target" bug —
# see the crew-builddomain-must-inject-ctx-prompt lesson).
_ALLOWED_CTX = ("prompt", "today", "directives")
_PLACEHOLDER_RE = re.compile(r"\{\{\s*ctx\.(\w+)\s*\}\}")
_TAG_RE = re.compile(r"^[a-z0-9._-]+$")  # AIMEAT tag charset (no ':' or '@' — those go in capabilities)
_LISTEN_KINDS = ("tasks", "messages", "records", "dms")  # the event surfaces CrewSpec.listen_for accepts


class CrewDocError(ValueError):
    """Raised when a crew doc fails validation. Carries the full ``errors`` list so the caller sees
    EVERY problem at once, not just the first."""

    def __init__(self, errors: list[str]):
        self.errors = list(errors)
        super().__init__("invalid crew doc:\n  - " + "\n  - ".join(self.errors))


# --------------------------------------------------------------------------- #
# Tool registry — a "tools" NAME in the doc resolves to a REAL make_*_tools factory call.
# The ids mirror crewaimeat.forge_catalog (web/memory/schedule/delegate/image/app_build) so the two
# surfaces never drift; forge_catalog EMITS SOURCE for these, this registry CONSTRUCTS them at runtime.
# Imports are lazy (per resolver) so importing this module stays cheap and a heavy/optional dependency
# is only touched when a doc actually names that tool.
# --------------------------------------------------------------------------- #
def _tools_memory(agent_name: str, ctx: Any) -> list:
    from crewaimeat.memory_tools import make_memory_tools

    return list(make_memory_tools(agent_name))


def _tools_web(agent_name: str, ctx: Any) -> list:
    from crewaimeat.crew import _web_tools

    return list(_web_tools())


def _tools_schedule(agent_name: str, ctx: Any) -> list:
    from crewaimeat.scheduler import make_schedule_tools

    return list(make_schedule_tools(agent_name))


def _tools_dm(agent_name: str, ctx: Any) -> list:
    from crewaimeat.dm import make_dm_tools

    return list(make_dm_tools(agent_name))


def _tools_delegate(agent_name: str, ctx: Any) -> list:
    from crewaimeat.workflow import make_workflow_tools

    tid = (getattr(ctx, "task", None) or {}).get("id") or "manual"
    wf = make_workflow_tools(coordinator_name=agent_name, run_id=tid, task_id=tid, tag="workflow", timeout=1800)
    # Same subset forge_catalog's `delegate` capability wires: discover peers + delegate-and-wait.
    return [t for t in wf if getattr(t, "name", "") in ("discover_crews", "delegate_and_wait")]


def _tools_image(agent_name: str, ctx: Any) -> list:
    from crewaimeat.seedream_gen import make_image_tools

    return list(make_image_tools(agent_name))


def _tools_app_build(agent_name: str, ctx: Any) -> list:
    from crewaimeat.author_tool import make_author_tools

    tid = (getattr(ctx, "task", None) or {}).get("id") or "manual"
    tools, _state = make_author_tools(agent_name, task_id=tid)
    return list(tools)


def _tools_local_memory(agent_name: str, ctx: Any) -> list:
    # Two-tier memory: keep raw findings LOCAL (remember/recall/search), publish only the refined result
    # UPWARD (publish_memory). What the agency's research/watcher brains use.
    from crewaimeat.local_memory import make_local_memory_tools

    return list(make_local_memory_tools(agent_name))


def _tools_article_fetch(agent_name: str, ctx: Any) -> list:
    # A single tool: given result URLs, fetch + extract the readable article text (so a searcher reads
    # sources instead of guessing from snippets). Pairs with `web`.
    from crewaimeat.article_extract import fetch_article_text

    return [fetch_article_text]


TOOL_REGISTRY: dict[str, Any] = {
    "memory": _tools_memory,
    "web": _tools_web,
    "schedule": _tools_schedule,
    "dm": _tools_dm,
    "delegate": _tools_delegate,
    "image": _tools_image,
    "app_build": _tools_app_build,
    "local_memory": _tools_local_memory,
    "article_fetch": _tools_article_fetch,
}

# One-line purpose per tool id — the single source the AI generators render into their tool menus, so an
# author can only pick a tool the interpreter can actually resolve (TOOL_REGISTRY is the resolver, this is
# its human-facing description; keep the two key sets in lockstep).
TOOL_PURPOSES: dict[str, str] = {
    "memory": "read/write the owner's node memory at EXACT keys with a chosen visibility (public/owner)",
    "web": "search the live web (SearXNG if reachable, else keyless DuckDuckGo)",
    "schedule": "create/list/update/delete AIMEAT node cron schedules (fire offline, 0 tokens)",
    "dm": "read + reply to the agent's federated inbox (agent-to-agent messages)",
    "delegate": "discover peer crews and delegate a subtask to one, then wait for its result",
    "image": "generate an image from a text prompt (Seedream) and get back its public URL",
    "app_build": "author, install, publish and verify a real AIMEAT app / cortex / extension",
    "local_memory": "keep raw findings in LOCAL memory (remember/recall/search) and publish only the refined result upward (publish_memory)",
    "article_fetch": "fetch + extract the readable article text behind result URLs (read sources, not snippets)",
}


def render_tool_catalog(ids: list[str] | None = None) -> str:
    """A '- id: purpose' menu of the tools the interpreter can resolve — the tool half of an AI author's
    spec. Defaults to every registered tool; pass ``ids`` to restrict it."""
    ids = ids if ids is not None else list(TOOL_REGISTRY)
    return "\n".join(f"    - {i}: {TOOL_PURPOSES.get(i, '(no description)')}" for i in ids if i in TOOL_REGISTRY)


def _agent_key(a: dict) -> str | None:
    """The local reference for an agent — its ``name`` (preferred), else its ``role``. Tasks name this
    in their ``agent`` field, and other tasks never do; it never reaches the LLM."""
    return a.get("name") or a.get("role")


# --------------------------------------------------------------------------- #
# Validation — reject a bad doc at the boundary, before any object is constructed.
# --------------------------------------------------------------------------- #
def _known_profiles() -> set[str] | None:
    """The profile names declared in the active ``llm_providers.json`` (``None`` when there is no
    providers file / no profiles — then ``llm_profile`` can't be checked and is accepted as-is)."""
    from crewaimeat.llm import _providers_file

    pf = _providers_file()
    if not pf:
        return None
    try:
        with open(pf, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return None
    profiles = cfg.get("profiles")
    return set(profiles) if isinstance(profiles, dict) and profiles else None


def _validate_signal_tree(sig: Any, field: str) -> list[str]:
    """One of ``required_to_function`` / ``success_signal``: the literal ``"none"`` (no gate) is fine;
    anything else must be a valid signal tree per the published grammar."""
    if sig in (None, "none"):
        return []
    try:
        validate_signal(sig)
        return []
    except SignalError as exc:
        return [f"signals.{field}: {exc}"]
    except Exception as exc:  # noqa: BLE001 — a malformed non-dict etc. must surface as an error, not crash
        return [f"signals.{field}: {exc}"]


def _validate_signals(sig: Any) -> list[str]:
    if not isinstance(sig, dict):
        return ["signals: must be an object"]
    errs: list[str] = []
    for field in ("required_to_function", "success_signal"):
        if field in sig:
            errs.extend(_validate_signal_tree(sig[field], field))
    dl = sig.get("deliverable_location")
    if dl is not None and (not isinstance(dl, dict) or not dl.get("key")):
        errs.append("signals.deliverable_location: must be an object with a non-empty 'key'")
    return errs


def _validate_agents(agents: Any) -> tuple[list[str], set[str]]:
    """Returns (errors, the set of valid agent keys) so task validation can check ``agent`` refs."""
    errors: list[str] = []
    keys: set[str] = set()
    if not isinstance(agents, list) or not agents:
        return ["agents: required non-empty list"], keys
    for i, a in enumerate(agents):
        if not isinstance(a, dict):
            errors.append(f"agents[{i}]: must be an object")
            continue
        key = _agent_key(a)
        for f in ("role", "goal", "backstory"):
            v = a.get(f)
            if not isinstance(v, str) or not v.strip():
                errors.append(f"agents[{i}] ({key or '?'}): {f} is required (non-empty string)")
        if not key:
            errors.append(f"agents[{i}]: needs a 'name' or 'role' to reference it by")
        elif key in keys:
            errors.append(f"agents[{i}]: duplicate agent key {key!r}")
        else:
            keys.add(key)
        tools = a.get("tools")
        if tools is not None:
            if not isinstance(tools, list):
                errors.append(f"agents[{i}] ({key}): tools must be a list of tool names")
            else:
                for tn in tools:
                    if tn not in TOOL_REGISTRY:
                        errors.append(f"agents[{i}] ({key}): unknown tool {tn!r} (known: {sorted(TOOL_REGISTRY)})")
        ad = a.get("allow_delegation")
        if ad is not None and not isinstance(ad, bool):
            errors.append(f"agents[{i}] ({key}): allow_delegation must be a boolean")
    return errors, keys


def _validate_tasks(tasks: Any, agent_keys: set[str]) -> list[str]:
    errors: list[str] = []
    if not isinstance(tasks, list) or not tasks:
        return ["tasks: required non-empty list"]
    seen_ids: set[str] = set()
    defined_before: set[str] = set()  # ids of tasks that appear EARLIER (context must point into this)
    prompt_injected = False
    for i, tk in enumerate(tasks):
        if not isinstance(tk, dict):
            errors.append(f"tasks[{i}]: must be an object")
            continue
        tid = tk.get("id")
        if not isinstance(tid, str) or not tid.strip():
            errors.append(f"tasks[{i}]: id is required (non-empty string)")
            tid = None
        elif tid in seen_ids:
            errors.append(f"tasks[{i}]: duplicate task id {tid!r}")
        else:
            seen_ids.add(tid)
        desc = tk.get("description")
        if not isinstance(desc, str) or not desc.strip():
            errors.append(f"tasks[{i}] ({tid}): description is required (non-empty string)")
        else:
            for m in _PLACEHOLDER_RE.finditer(desc):
                attr = m.group(1)
                if attr not in _ALLOWED_CTX:
                    errors.append(
                        f"tasks[{i}] ({tid}): unknown placeholder {{{{ctx.{attr}}}}} (allowed: {_ALLOWED_CTX})"
                    )
                elif attr == "prompt":
                    prompt_injected = True
        eo = tk.get("expected_output")
        if not isinstance(eo, str) or not eo.strip():
            errors.append(f"tasks[{i}] ({tid}): expected_output is required (non-empty string)")
        ag = tk.get("agent")
        if ag not in agent_keys:
            errors.append(f"tasks[{i}] ({tid}): agent {ag!r} does not match any defined agent")
        ctxrefs = tk.get("context")
        if ctxrefs is not None:
            if not isinstance(ctxrefs, list):
                errors.append(f"tasks[{i}] ({tid}): context must be a list of task ids")
            else:
                for ref in ctxrefs:
                    if ref == tid:
                        errors.append(f"tasks[{i}] ({tid}): context references itself")
                    elif ref not in defined_before:
                        errors.append(
                            f"tasks[{i}] ({tid}): context {ref!r} must reference an EARLIER task "
                            "(unknown id, or a forward/cyclic reference)"
                        )
        asy = tk.get("async")
        if asy is not None and not isinstance(asy, bool):
            errors.append(f"tasks[{i}] ({tid}): async must be a boolean")
        if tid:
            defined_before.add(tid)
    if not prompt_injected:
        errors.append(
            "no task description references {{ctx.prompt}} — the crew would drift from the actual "
            "request (see the crew-builddomain-must-inject-ctx-prompt lesson)"
        )
    return errors


def validate_crew_doc(doc: Any) -> list[str]:
    """Return a list of human-readable problems with ``doc`` (empty list == valid). Runs BEFORE any
    CrewAI object is built, so a bad definition is rejected at the boundary, not at run time. Catches:
    a missing/blank ``agent_name``; an agent without role/goal/backstory or with an unknown tool; a
    task missing id/description/expected_output, pointing at a missing agent, or whose ``context`` edge
    is unknown / forward / cyclic (non-DAG); a bad ``{{ctx.x}}`` placeholder; a doc where no task
    injects ``{{ctx.prompt}}``; a malformed signal tree; and out-of-range/wrong-typed crew fields."""
    if not isinstance(doc, dict):
        return ["crew doc must be a JSON object"]
    errors: list[str] = []

    name = doc.get("agent_name")
    if not isinstance(name, str) or not name.strip():
        errors.append("agent_name: required non-empty string")

    t = doc.get("temperature")
    if t is not None and (isinstance(t, bool) or not isinstance(t, (int, float)) or not 0.0 <= float(t) <= 2.0):
        errors.append("temperature: must be a number in [0, 2]")

    proc = doc.get("process")
    if proc is not None and proc not in ("sequential", "hierarchical"):
        errors.append("process: must be 'sequential' or 'hierarchical'")

    for flag in ("discover", "memory"):
        if doc.get(flag) is not None and not isinstance(doc[flag], bool):
            errors.append(f"{flag}: must be a boolean")

    listen = doc.get("listen_for")
    if listen is not None:
        if not isinstance(listen, list) or not listen:
            errors.append("listen_for: must be a non-empty list of event surfaces")
        else:
            for kind in listen:
                if kind not in _LISTEN_KINDS:
                    errors.append(f"listen_for: {kind!r} must be one of {_LISTEN_KINDS}")

    prof = doc.get("llm_profile")
    if prof is not None:
        if not isinstance(prof, str) or not prof.strip():
            errors.append("llm_profile: must be a non-empty string")
        else:
            known = _known_profiles()
            if known is not None and prof not in known:
                errors.append(f"llm_profile: {prof!r} not a profile in llm_providers.json {sorted(known)}")

    tags = doc.get("tags")
    if tags is not None:
        if not isinstance(tags, list):
            errors.append("tags: must be a list of strings")
        else:
            for tg in tags:
                if not isinstance(tg, str) or not _TAG_RE.match(tg):
                    errors.append(f"tags: {tg!r} must match the AIMEAT tag charset [a-z0-9._-] (no ':' or '@')")

    caps = doc.get("capabilities")
    if caps is not None and not isinstance(caps, dict):
        errors.append("capabilities: must be an object {technical, domain, languages}")

    offers = doc.get("offers")
    if offers is not None:
        if not isinstance(offers, list):
            errors.append("offers: must be a list of offer objects")
        else:
            for i, o in enumerate(offers):
                if not isinstance(o, dict) or not o.get("id"):
                    errors.append(f"offers[{i}]: must be an object with an 'id'")

    agent_errs, agent_keys = _validate_agents(doc.get("agents"))
    errors.extend(agent_errs)
    errors.extend(_validate_tasks(doc.get("tasks"), agent_keys))

    if doc.get("signals") is not None:
        errors.extend(_validate_signals(doc["signals"]))

    return errors


# --------------------------------------------------------------------------- #
# Interpreter — build the SAME (agents, tasks) a Python build_domain would, from the doc. No exec/eval.
# --------------------------------------------------------------------------- #
def _inject(text: str, ctx: Any) -> str:
    """Substitute ``{{ctx.prompt}}`` / ``{{ctx.today}}`` / ``{{ctx.directives}}`` from the live ctx.
    Defense-in-depth: the validator already rejects an unknown placeholder, but if one slips through
    (e.g. the interpreter is called without validating) fail LOUD rather than leave a literal token."""

    def repl(m: re.Match) -> str:
        attr = m.group(1)
        if attr not in _ALLOWED_CTX:
            raise CrewDocError([f"unknown template placeholder {{{{ctx.{attr}}}}}"])
        return str(getattr(ctx, attr, "") or "")

    return _PLACEHOLDER_RE.sub(repl, text)


def build_domain_from_json(doc: dict, ctx: Any) -> tuple[list, list]:
    """Interpret ``doc`` into ``([Agent], [Task])`` — the pure, exec-free equivalent of a hand-written
    ``build_domain(ctx)``. Validates first and raises ``CrewDocError`` on ANY problem (so a bad def
    never reaches construction). Agents use ``ctx.llm`` (already routed for this agent by run_crew);
    ``tools`` names resolve to the real factories; task ``context`` refs resolve to the earlier Task
    objects; ``{{ctx.*}}`` is injected into descriptions."""
    errs = validate_crew_doc(doc)
    if errs:
        raise CrewDocError(errs)

    from crewai import Agent, Task

    agent_name = doc["agent_name"]
    # Use ctx.llm as-is — exactly like a hand-written build_domain. run_crew always supplies the routed
    # LLM (get_llm(agent_name=...)) at run time; it is only None during OFFLINE validation
    # (crewaimeat._validate_crew passes llm=None), where crewai builds its default lazily at kickoff so
    # construction still succeeds. NOT get_llm-on-None here — that would spuriously fail validation on a
    # machine with no LLM key, unlike every Python crew (which passes llm=None straight to Agent).
    llm = getattr(ctx, "llm", None)

    agents_by_key: dict[str, Any] = {}
    agent_objs: list = []
    for a in doc["agents"]:
        tools: list = []
        for tn in a.get("tools") or []:
            tools.extend(TOOL_REGISTRY[tn](agent_name, ctx))
        kwargs: dict[str, Any] = dict(
            role=a["role"],
            goal=a["goal"],
            backstory=a["backstory"],
            llm=llm,
            tools=tools,
            allow_delegation=bool(a.get("allow_delegation", False)),
            verbose=bool(a.get("verbose", True)),
        )
        if a.get("max_iter") is not None:
            kwargs["max_iter"] = a["max_iter"]
        agent = Agent(**kwargs)
        agents_by_key[_agent_key(a)] = agent
        agent_objs.append(agent)

    tasks_by_id: dict[str, Any] = {}
    task_objs: list = []
    for tk in doc["tasks"]:
        context = [tasks_by_id[r] for r in (tk.get("context") or [])]
        kwargs = dict(
            description=_inject(tk["description"], ctx),
            expected_output=tk["expected_output"],
            agent=agents_by_key[tk["agent"]],
        )
        if context:
            kwargs["context"] = context
        if tk.get("async"):
            kwargs["async_execution"] = True
        task = Task(**kwargs)
        tasks_by_id[tk["id"]] = task
        task_objs.append(task)

    return agent_objs, task_objs


def crewspec_from_json(doc: dict, **overrides: Any):
    """Wrap a crew doc as a runnable ``CrewSpec`` whose ``build_domain`` is this interpreter bound to
    ``doc``. Carries the crew-level fields the doc declares (temperature / process / tags /
    capabilities / first offer / readme). ``overrides`` win, for CrewSpec fields the doc doesn't model
    yet (e.g. ``listen_for``, ``owner``). Validates the doc up front (raises ``CrewDocError``)."""
    errs = validate_crew_doc(doc)
    if errs:
        raise CrewDocError(errs)

    from crewai import Process

    from crewaimeat.aimeat_crew import CrewSpec

    process = Process.hierarchical if doc.get("process") == "hierarchical" else Process.sequential
    kwargs: dict[str, Any] = dict(
        agent_name=doc["agent_name"],
        build_domain=partial(build_domain_from_json, doc),
        process=process,
    )
    if doc.get("readme_md") is not None:
        kwargs["readme_md"] = doc["readme_md"]
    if doc.get("temperature") is not None:
        kwargs["temperature"] = doc["temperature"]
    if doc.get("tags") is not None:
        kwargs["tags"] = doc["tags"]
    if doc.get("capabilities") is not None:
        kwargs["capabilities"] = doc["capabilities"]
    if doc.get("discover") is not None:
        kwargs["discover"] = bool(doc["discover"])
    if doc.get("memory") is not None:
        kwargs["memory"] = bool(doc["memory"])
    if doc.get("listen_for") is not None:
        kwargs["listen_for"] = tuple(doc["listen_for"])
    offers = doc.get("offers")
    if offers:  # CrewSpec carries ONE inline offer meta; the rest live in the doc for discovery
        kwargs["offer"] = offers[0]
    kwargs.update(overrides)
    return CrewSpec(**kwargs)


def load_crew_doc(path: str | Path) -> dict:
    """Load a crew doc from a JSON file. Raises on unreadable/invalid JSON — never returns a partial."""
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    if not isinstance(doc, dict):
        raise CrewDocError([f"{path}: crew doc must be a JSON object"])
    return doc

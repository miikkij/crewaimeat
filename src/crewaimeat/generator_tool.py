"""Generator REST tools — drive the AIMEAT app generator pipeline (/v1/generator/*) from a crew.

This is "Tie 1" of the AIMEAT SDLC: the calibrated generator pipeline does the heavy lifting
(prompt templates, server-side validation, component registration/activation, probe machinery),
and a crew drives it over REST. The tools here are DETERMINISTIC plumbing — auth + the request/
response round-trips. The AGENT supplies the content (the interview spec JSON, the blueprint JSON,
and each component's artifact code in the exact required format).

Auth reuses the agent's OWN token (~/.aimeat/tokens/<agent>@<owner>.token) via
aimeat_crewai.daemon._read_token + a Bearer header — the same pattern as evolve.py. Verified
2026-06-02 against https://aimeat.io: a task-runner agent token carries generator read/write/
execute scope (POST /v1/generator/projects -> 201, DELETE -> 200).

Usage (in a crew's build_domain):

    from crewaimeat.generator_tool import make_generator_tools
    gen_tools, gen_state = make_generator_tools(AGENT_NAME)
    builder = Agent(..., tools=[*gen_tools, delegate_and_wait], llm=ctx.llm)

`gen_state` is a shared dict; gen_create_project stores the projectId there and every later tool
reads it automatically, so the agent never has to thread the id through tool calls.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import requests
from crewai.tools import tool

try:  # private helper; degrade gracefully if a future version moves it
    from aimeat_crewai.daemon import _read_token as _aimeat_read_token
except Exception:  # pragma: no cover
    _aimeat_read_token = None

GEN_TIMEOUT = 90  # generator prompts/artifacts can be large; give the node room

# Deterministic pre-submit quality gates — the "catch" a human gave the UI flow, automated. A cortex
# that fails these (syntax error, or memory reads missing the service_slug prefix) must be fixed
# BEFORE it registers, never shipped "green". See app_verify.py.
from crewaimeat.app_verify import cortex_syntax_ok, cortex_uses_slug


# --------------------------------------------------------------------------- #
# Auth + owner discovery
# --------------------------------------------------------------------------- #
def _discover_owner(agent_name: str) -> str | None:
    """Find the owner from the token filename (~/.aimeat/tokens/<agent>@<owner>.token).

    Deterministic and env-free: there is exactly one token file per (agent, owner)."""
    from crewaimeat._home import aimeat_home

    tokens = aimeat_home() / "tokens"
    try:
        for p in tokens.glob(f"{agent_name}@*.token"):
            stem = p.name[: -len(".token")]
            if "@" in stem:
                return stem.split("@", 1)[1]
    except OSError:
        pass
    return os.environ.get("AIMEAT_OWNER")


def _token(agent_name: str, owner: str | None):
    if _aimeat_read_token is None:
        return None, None
    try:
        return _aimeat_read_token(agent_name, owner=owner)
    except Exception:  # noqa: BLE001 — never crash the agent on auth read
        return None, None


def _call(agent_name: str, owner: str | None, method: str, path: str, body: Any = None) -> dict:
    """Authenticated REST call to the node. Returns the parsed AIMEAT envelope dict (with an extra
    `_status`), or {"_error": ...}. Never raises."""
    tok, url = _token(agent_name, owner)
    if not tok or not url:
        return {"_error": "no token/url for agent (is it registered + approved?)"}
    base = url.rstrip("/")
    headers = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
    try:
        r = requests.request(method, f"{base}{path}", headers=headers, json=body, timeout=GEN_TIMEOUT)
    except Exception as e:  # noqa: BLE001
        return {"_error": f"request failed: {e!r}"}
    try:
        data = r.json()
        if not isinstance(data, dict):
            data = {"_raw": data}
    except Exception:  # noqa: BLE001
        data = {"_raw": (r.text or "")[:600]}
    data["_status"] = r.status_code
    return data


def _ok(env: dict) -> bool:
    return isinstance(env, dict) and env.get("ok") is True and "_error" not in env


def _err(env: dict) -> str:
    if "_error" in env:
        return env["_error"]
    e = env.get("error") or {}
    msg = f"HTTP {env.get('_status')}: {e.get('code', '')} {e.get('message', '')}".strip()
    det = e.get("details")
    if det:
        msg += f" details={json.dumps(det)[:400]}"
    return msg or json.dumps(env)[:400]


def _node_base(agent_name: str, owner: str | None) -> str | None:
    _tok, url = _token(agent_name, owner)
    return url.rstrip("/") if url else None


# --------------------------------------------------------------------------- #
# Tool factory
# --------------------------------------------------------------------------- #
def make_generator_tools(agent_name: str, owner: str | None = None, task_id: str | None = None) -> tuple[list, dict]:
    """Return (tools, state). Attach `tools` to the builder agent; `state` is the shared dict that
    carries the active projectId across tool calls within one build. `task_id` (the AIMEAT task) lets
    the tools post descriptive progress events so the dashboard shows real steps (spec/code/register)."""
    owner = owner or _discover_owner(agent_name)
    state: dict = {"pid": None, "owner": owner, "types": {}, "specs": set()}

    def _need_pid() -> str | None:
        return state.get("pid")

    def _blueprint_meta() -> tuple[str, list[str]]:
        """(service_slug, seeded-key prefixes) from the blueprint, cached. The slug-gate needs these
        to know which memory-key first-segments (i18n, settings, the domain namespace) the cortex must
        read slug-prefixed."""
        if state.get("_bp_meta") is not None:
            return state["_bp_meta"]
        slug, prefixes = "", []
        pid = state.get("pid")
        if pid:
            g = _call(agent_name, owner, "GET", f"/v1/generator/{pid}")
            bp = (((g.get("data") or {}).get("project") or {}).get("blueprint")) or {}
            if isinstance(bp, str):
                try:
                    bp = json.loads(bp)
                except Exception:  # noqa: BLE001
                    bp = {}
            slug = bp.get("service_slug") or ""
            mk = (bp.get("dataModel") or {}).get("memoryKeys") or {}
            # Blueprints sometimes store keys already slug-qualified ('<slug>.i18n.en') and sometimes
            # bare ('i18n.en'); normalise to the seeded first-segment either way, and never let the
            # slug itself become a "prefix" (that yields an impossible double-prefix in the gate).
            pfxset: set[str] = set()
            for k in mk.keys():
                ks = str(k)
                if slug and ks.startswith(f"{slug}."):
                    ks = ks[len(slug) + 1:]
                first = ks.split(".")[0]
                if first and first != slug:
                    pfxset.add(first)
            prefixes = sorted(pfxset)
        state["_bp_meta"] = (slug, prefixes)
        return state["_bp_meta"]

    def _event(message: str) -> None:
        """Best-effort: post a human-readable progress event to the AIMEAT task timeline."""
        if not task_id:
            return
        try:
            _call(agent_name, owner, "POST", f"/v1/agents/me/tasks/{task_id}/event",
                  {"type": "progress", "message": message})
        except Exception:  # noqa: BLE001 — progress is best-effort, never break the build
            pass

    @tool("gen_create_project")
    def gen_create_project(name: str, description: str) -> str:
        """Create a new generator project for the app you are about to build. `name` = a short
        human name; `description` = one paragraph of what the app does. The returned projectId is
        stored automatically — every later gen_* tool uses it. Call this FIRST."""
        env = _call(agent_name, owner, "POST", "/v1/generator/projects",
                    {"name": name, "description": description})
        if not _ok(env):
            return f"FAILED to create project: {_err(env)}"
        pid = (env.get("data") or {}).get("projectId")
        state["pid"] = pid
        return f"Project created (projectId={pid}, stored). Next: gen_get_interview_prompt."

    @tool("gen_open_project")
    def gen_open_project(project_id: str) -> str:
        """Open an EXISTING generator project so the other gen_* tools operate on it (sets the active
        projectId). Use this when fixing/finishing a project someone else built — NOT gen_create_project."""
        env = _call(agent_name, owner, "GET", f"/v1/generator/{project_id}")
        if not _ok(env):
            return f"FAILED to open project {project_id}: {_err(env)}"
        state["pid"] = project_id
        state["_bp_meta"] = None  # recompute slug/key-prefixes for the opened project
        proj = ((env.get("data") or {}).get("project") or {})
        return f"Opened project {project_id} (name={proj.get('name')}, status={proj.get('status')}). The gen_* tools now act on it."

    @tool("gen_get_interview_prompt")
    def gen_get_interview_prompt() -> str:
        """Fetch the calibrated INTERVIEW prompt. Run it yourself (you are both the requirements
        analyst AND the interviewee): produce the structured JSON spec it asks for — use cases,
        data sources (honour the URL-validation protocol: every external URL fetched + given a real
        sampleEntry, or marked verified:false with a fallback), data model, views, style, settings,
        locale. Then submit it with gen_import_spec."""
        pid = _need_pid()
        if not pid:
            return "No project yet — call gen_create_project first."
        env = _call(agent_name, owner, "GET", f"/v1/generator/{pid}/prompts?type=interview")
        if not _ok(env):
            return f"FAILED to get interview prompt: {_err(env)}"
        return (env.get("data") or {}).get("prompt") or json.dumps(env.get("data"))[:4000]

    @tool("gen_import_spec")
    def gen_import_spec(spec_json: str) -> str:
        """Import the interview spec you produced. `spec_json` = the spec as a JSON string (an
        object). Returns saved=true, or the validation errors to fix. Do not proceed to the
        blueprint until this saves AND the spec passes the quality gate (verified URL + sampleEntry
        per data source, >=2 use cases, a locale, views reference real entities)."""
        pid = _need_pid()
        if not pid:
            return "No project yet — call gen_create_project first."
        try:
            spec = json.loads(spec_json)
        except Exception as e:  # noqa: BLE001
            return f"spec_json is not valid JSON: {e}. Fix and resend the full object."
        env = _call(agent_name, owner, "POST", f"/v1/generator/{pid}/interview", {"interviewSpec": spec})
        if not _ok(env):
            return f"SPEC REJECTED: {_err(env)}"
        return "Spec saved (saved=true). Self-check the quality gate, then gen_get_blueprint_prompt."

    @tool("gen_get_blueprint_prompt")
    def gen_get_blueprint_prompt() -> str:
        """Fetch the calibrated BLUEPRINT prompt (it includes your saved spec + a live catalog of
        reusable cortex libs). Run it: produce the JSON blueprint — components[], phases,
        dataModel.structures built from REAL sampleEntry data with strict $ref discipline,
        memoryKeys, actions, service_slug, testScenarios. Decompose the cortex (data + >=1 component
        + one app-domain). Only add an extension for genuine server-only work (external API / cron).
        Then submit with gen_import_blueprint."""
        pid = _need_pid()
        if not pid:
            return "No project yet — call gen_create_project first."
        env = _call(agent_name, owner, "GET", f"/v1/generator/{pid}/prompts?type=blueprint")
        if not _ok(env):
            return f"FAILED to get blueprint prompt: {_err(env)}"
        return (env.get("data") or {}).get("prompt") or json.dumps(env.get("data"))[:4000]

    @tool("gen_import_blueprint")
    def gen_import_blueprint(blueprint_json: str) -> str:
        """Import the blueprint. `blueprint_json` MUST be the blueprint as a JSON STRING (the route
        requires a string, unlike the spec). On success it seeds one component record per blueprint
        component. Returns valid=true or the validation errors to fix."""
        pid = _need_pid()
        if not pid:
            return "No project yet — call gen_create_project first."
        env = _call(agent_name, owner, "POST", f"/v1/generator/{pid}/steps/blueprint",
                    {"blueprint": blueprint_json})
        if not _ok(env):
            return f"BLUEPRINT REJECTED: {_err(env)}"
        d = env.get("data") or {}
        warn = d.get("warnings") or []
        return f"Blueprint imported (valid={d.get('valid')}). Warnings: {json.dumps(warn)[:300]}. Next: gen_save_settings (if any), then gen_list_components."

    @tool("gen_save_settings")
    def gen_save_settings(values_json: str) -> str:
        """Store initial service/user setting values the spec surfaced. `values_json` = a JSON object
        string of flat key->value (string|number|boolean). If the app needs no settings, pass '{}'."""
        pid = _need_pid()
        if not pid:
            return "No project yet — call gen_create_project first."
        try:
            values = json.loads(values_json or "{}")
        except Exception as e:  # noqa: BLE001
            return f"values_json is not valid JSON: {e}."
        if not values:
            return "No settings to store — skip and continue to gen_list_components."
        env = _call(agent_name, owner, "POST", f"/v1/generator/{pid}/settings", {"values": values})
        if not _ok(env):
            if env.get("_status") == 403:
                return ("Settings are owner-managed (403 owner-only) — the agent cannot set them. "
                        "Fine for an app with no external-service secrets; proceed to gen_list_components.")
            return f"FAILED to save settings: {_err(env)}"
        return f"Settings stored ({(env.get('data') or {}).get('stored', 0)} keys)."

    @tool("gen_list_components")
    def gen_list_components() -> str:
        """List the blueprint's components IN PHASE ORDER with their type/subtype and current status
        (not_started/ready/registered). Build them in exactly this order: define (csm) -> seed
        (memory, translations) -> [extension] -> data cortex -> component cortexes -> app-domain
        cortex -> app."""
        pid = _need_pid()
        if not pid:
            return "No project yet — call gen_create_project first."
        env = _call(agent_name, owner, "GET", f"/v1/generator/{pid}")
        if not _ok(env):
            return f"FAILED to read project: {_err(env)}"
        d = env.get("data") or {}
        project = d.get("project") or {}
        bp = project.get("blueprint") or {}
        if isinstance(bp, str):  # tolerate a blueprint stored as a JSON string (older/edge nodes)
            try:
                bp = json.loads(bp)
            except Exception:  # noqa: BLE001
                bp = {}
        comps = bp.get("components") or []
        phases = bp.get("phases") or []
        # status records may be a dict keyed by id or a list
        recs = d.get("components") or {}
        status_of: dict = {}
        if isinstance(recs, dict):
            for cid, rec in recs.items():
                status_of[cid] = (rec or {}).get("status")
        elif isinstance(recs, list):
            for rec in recs:
                status_of[(rec or {}).get("id")] = (rec or {}).get("status")
        meta = {c.get("id"): c for c in comps if isinstance(c, dict)}
        for _c in comps:  # cache (type, subtype) so gen_component_prompt can enforce spec-first
            if isinstance(_c, dict) and _c.get("id"):
                state.setdefault("types", {})[_c["id"]] = (_c.get("type"), _c.get("subtype"))
        ordered_ids: list = []
        for ph in phases:
            for cid in (ph.get("componentIds") or []):
                if cid not in ordered_ids:
                    ordered_ids.append(cid)
        for c in comps:  # any not covered by phases, appended at the end
            if c.get("id") not in ordered_ids:
                ordered_ids.append(c.get("id"))
        if not ordered_ids:
            return "No components found — import a blueprint first (gen_import_blueprint)."
        lines = [f"service_slug={bp.get('service_slug')}", "Components in build order:"]
        for cid in ordered_ids:
            c = meta.get(cid, {})
            sub = f"/{c.get('subtype')}" if c.get("subtype") else ""
            lines.append(f"- {cid}  [{c.get('type')}{sub}]  status={status_of.get(cid, '?')}  — {c.get('label', '')}")
        return "\n".join(lines)

    @tool("gen_component_prompt")
    def gen_component_prompt(component_id: str, ptype: str = "code") -> str:
        """Fetch the calibrated prompt for one component. ptype: 'code' (default, the artifact),
        'spec' (extension/cortex/app only — the formal contract), or 'test' (test code). Run it and
        produce the artifact in the EXACT format: csm/msm -> YAML; memory/translation -> JSON object;
        extension -> fenced YAML manifest + fenced JS action scripts; cortex -> fenced YAML manifest +
        fenced ```javascript IIFE; app -> a single HTML document. Then gen_submit_component."""
        pid = _need_pid()
        if not pid:
            return "No project yet — call gen_create_project first."
        # Enforce SPEC-FIRST for extension/cortex/app: block the code prompt until the spec is stored.
        # (Deterministic — do not rely on the model to remember the spec step.)
        if (not ptype) or ptype == "code":
            ctype = (state.get("types") or {}).get(component_id, (None, None))[0]
            if ctype is None:  # types not cached yet — fetch the blueprint once
                _g = _call(agent_name, owner, "GET", f"/v1/generator/{pid}")
                _bp = (((_g.get("data") or {}).get("project") or {}).get("blueprint")) or {}
                if isinstance(_bp, str):
                    try:
                        _bp = json.loads(_bp)
                    except Exception:  # noqa: BLE001
                        _bp = {}
                for _c in (_bp.get("components") or []):
                    if isinstance(_c, dict) and _c.get("id"):
                        state.setdefault("types", {})[_c["id"]] = (_c.get("type"), _c.get("subtype"))
                ctype = (state.get("types") or {}).get(component_id, (None, None))[0]
            if ctype in ("extension", "cortex", "app") and component_id not in state.get("specs", set()):
                return (f"SPEC REQUIRED FIRST — '{component_id}' is a {ctype} and MUST be built spec-first. "
                        f"Do NOT request its code yet. Steps: (1) gen_component_prompt('{component_id}', 'spec') "
                        f"(2) produce the spec JSON (3) gen_submit_spec('{component_id}', spec_json) "
                        f"(4) THEN gen_component_prompt('{component_id}', 'code'). This is mandatory.")
        _event(("Writing spec for " if ptype == "spec" else "Generating code for ") + component_id)
        q = f"?type={ptype}" if ptype and ptype != "code" else ""
        env = _call(agent_name, owner, "GET", f"/v1/generator/{pid}/prompts/{component_id}{q}")
        if not _ok(env):
            return f"FAILED to get {ptype} prompt for {component_id}: {_err(env)}"
        return (env.get("data") or {}).get("prompt") or json.dumps(env.get("data"))[:4000]

    @tool("gen_submit_spec")
    def gen_submit_spec(component_id: str, spec_json: str) -> str:
        """Store a component's formal SPEC — the spec-first step for extension/cortex/app. `spec_json`
        is the spec as a JSON string. Call this AFTER producing the spec (gen_component_prompt with
        ptype='spec') and BEFORE fetching the code prompt: the code prompt reads this spec back as its
        formal contract (selfSpec / extensionSpec / dataApiSpec), so the generated code matches it.
        Skip entirely for csm/memory/translation (those are code-only)."""
        pid = _need_pid()
        if not pid:
            return "No project yet — call gen_create_project first."
        try:
            spec = json.loads(spec_json)
        except Exception as e:  # noqa: BLE001
            return f"spec_json is not valid JSON: {e}. Fix and resend the full object."
        env = _call(agent_name, owner, "POST",
                    f"/v1/generator/{pid}/components/{component_id}/spec", {"spec": spec})
        if not _ok(env):
            return f"SPEC STORE FAILED ({component_id}): {_err(env)}"
        state.setdefault("specs", set()).add(component_id)
        _event("Spec stored: " + component_id)
        return (f"Spec stored for {component_id}. Next: gen_component_prompt('{component_id}', 'code') "
                "— the code prompt now includes this spec.")

    @tool("gen_submit_component")
    def gen_submit_component(component_id: str, ctype: str, content: str) -> str:
        """Validate + store one component's artifact (server-side). `ctype` is one of
        csm|msm|extension|app|memory|translation|cortex. `content` is the full artifact text. On
        success the component becomes 'ready'. On 422 it returns the validation errors — read them,
        fix the artifact, and resubmit (up to ~3 rounds, then regenerate fresh)."""
        pid = _need_pid()
        if not pid:
            return "No project yet — call gen_create_project first."
        # PRE-SUBMIT QUALITY GATE (cortex only) — catch the two failure modes that pass server-side
        # validation but break at runtime, BEFORE the component registers "green":
        #   (1) JS syntax error (e.g. a missing dot → SyntaxError), (2) memory reads that drop the
        #   service_slug prefix (→ 404 → raw i18n keys / empty data). The agent fixes, then resubmits.
        if ctype == "cortex":
            import re as _re
            jsm = _re.search(r"```(?:javascript|js)\s*\n([\s\S]*?)```", content)
            js = jsm.group(1) if jsm else ""
            if js:
                ok_syntax, syn_err = cortex_syntax_ok(js)
                if not ok_syntax:
                    return (f"PRE-SUBMIT BLOCKED ({component_id}): the cortex JavaScript has a SYNTAX "
                            f"ERROR — {syn_err}. Fix the JS and resubmit; do NOT register broken code.")
                slug, prefixes = _blueprint_meta()
                if slug and prefixes:
                    ok_slug, offenders = cortex_uses_slug(js, slug, prefixes)
                    if not ok_slug:
                        eg = offenders[0]
                        return (f"PRE-SUBMIT BLOCKED ({component_id}): the cortex reads memory key(s) "
                                f"WITHOUT the service_slug prefix: {offenders}. Seeded data is stored "
                                f"namespaced as '{slug}.<key>'. Read every seeded key slug-prefixed, e.g. "
                                f"AIMEAT.data.get('{slug}.{eg}...'), then resubmit.")
        env = _call(agent_name, owner, "POST", f"/v1/generator/{pid}/components/{component_id}/submit",
                    {"type": ctype, "content": content})
        if not _ok(env):
            return f"SUBMIT REJECTED ({component_id}): {_err(env)}"
        d = env.get("data") or {}
        warn = d.get("warnings") or []
        return f"Submitted {component_id} (valid={d.get('valid')}, status=ready). Warnings: {json.dumps(warn)[:300]}. Next: gen_register_component."

    @tool("gen_register_component")
    def gen_register_component(component_id: str) -> str:
        """Install one 'ready' component into the catalogue. Cortex is registered AND activated by
        this one call; CSM/MSM/memory/translation/app go live on register; an extension is stored
        INACTIVE (activate it separately if you built one)."""
        pid = _need_pid()
        if not pid:
            return "No project yet — call gen_create_project first."
        env = _call(agent_name, owner, "POST", f"/v1/generator/{pid}/components/{component_id}/register")
        if not _ok(env):
            return f"REGISTER FAILED ({component_id}): {_err(env)}"
        _event("Registered: " + component_id)
        return f"Registered {component_id} (status=registered)."

    @tool("gen_activate_extension")
    def gen_activate_extension(extension_name: str) -> str:
        """Activate a registered extension (runs @activate jobs + schedules). Only needed if you
        built an extension — cortex auto-activates on register. `extension_name` is its registeredAs
        name. (Also applies settings into ctx.config first.)"""
        pid = _need_pid()
        if not pid:
            return "No project yet — call gen_create_project first."
        _call(agent_name, owner, "POST", f"/v1/generator/{pid}/apply-settings/{extension_name}")
        env = _call(agent_name, owner, "POST", f"/v1/extensions/{extension_name}/activate")
        if not _ok(env):
            return f"ACTIVATE FAILED ({extension_name}): {_err(env)}"
        return f"Activated extension {extension_name}."

    @tool("gen_complete")
    def gen_complete() -> str:
        """Mark the project complete (status=active). Requires at least one registered component.
        Call this only AFTER the final browser test (delegated to web-tester) passes."""
        pid = _need_pid()
        if not pid:
            return "No project yet — call gen_create_project first."
        env = _call(agent_name, owner, "POST", f"/v1/generator/{pid}/complete")
        if not _ok(env):
            return f"COMPLETE FAILED: {_err(env)}"
        d = env.get("data") or {}
        return f"Project complete (status={d.get('status')}, registeredComponents={d.get('registeredComponents')})."

    @tool("gen_app_inline_url")
    def gen_app_inline_url(filename: str) -> str:
        """Build the public inline URL of a published app, to hand to web-tester for the final
        browser test. `filename` = the app's published filename (e.g. 'my-app.html')."""
        base = _node_base(agent_name, owner)
        if not base:
            return "No node URL available."
        own = state.get("owner") or owner or "<owner>"
        return f"{base}/v1/apps/{own}/{filename}?mode=inline"

    tools = [
        gen_create_project, gen_open_project, gen_get_interview_prompt, gen_import_spec,
        gen_get_blueprint_prompt, gen_import_blueprint, gen_save_settings,
        gen_list_components, gen_component_prompt, gen_submit_spec, gen_submit_component,
        gen_register_component, gen_activate_extension, gen_complete, gen_app_inline_url,
    ]
    # These tools carry mutable state / live server responses — never serve a cached result.
    for _t in tools:
        try:
            _t.cache_function = lambda *_a, **_k: False
        except Exception:  # noqa: BLE001
            pass
    return tools, state

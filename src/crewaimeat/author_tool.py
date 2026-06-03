"""Direct-build tools — author + install a cortex + app stack WITHOUT the generator pipeline.

This is the pivot (2026-06-02): instead of driving /v1/generator/* (LLM codegen at ~9 components,
fragile, needs gates to catch slips), a capable agent AUTHORS the artifacts in one coherent context —
a cortex lib (the app's clean API) and an app HTML (presentation only) — then installs/publishes them
via REST. Proven end-to-end on localhost (fleet-activity-dashboard: 5 cards, topic filter, authed
render, zero console errors). See the memory note [[aimeat-direct-build-pattern]] and
docs/aimeat-app-authoring-guide.md.

These tools are DETERMINISTIC plumbing (auth + the request/response round-trips + pre-flight syntax
gates). The AGENT supplies the content: the cortex manifest YAML + lib JS, and the app HTML — authored
against the REAL lib APIs (read them with read_lib_api, don't trust stale docs).

Auth reuses the agent's own token (same as generator_tool). Cortex install is owner-gated on the node
until the agent-write grant is deployed; install_cortex surfaces that clearly if it 403s. App publish
already works for agents. Apps are served under the OWNER ( /v1/apps/<owner>/<file> ) even when an
agent publishes them.

Usage (in a crew's build_domain):

    from crewaimeat.author_tool import make_author_tools
    author_tools, author_state = make_author_tools(AGENT_NAME, task_id=tid)
    builder = Agent(..., tools=[*author_tools, delegate_and_wait], llm=ctx.llm)
"""

from __future__ import annotations

import base64
import json
from typing import Any

import requests
from crewai.tools import tool

# Reuse the shared auth + REST helpers (stable, not generator-specific).
from crewaimeat.generator_tool import (
    _call,
    _discover_owner,
    _err,
    _node_base,
    _ok,
    _token,
)
from crewaimeat.app_verify import cortex_syntax_ok

AUTHOR_TIMEOUT = 60


def _check_js(code: str) -> tuple[bool, str]:
    """Pre-flight syntax gate (node --check). Degrades open if node is missing."""
    return cortex_syntax_ok(code or "")


def _extract_inline_js(html: str) -> str:
    """Concatenate the contents of every <script>…</script> with no src, for a syntax check."""
    import re
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html or "", re.S | re.I)
    return "\n;\n".join(blocks)


def make_author_tools(agent_name: str, owner: str | None = None, task_id: str | None = None) -> tuple[list, dict]:
    """Return (tools, state). Attach tools to the builder agent. `state` carries the node base + owner
    and tracks what got installed/published, for a clean final report."""
    owner = owner or _discover_owner(agent_name)
    base = _node_base(agent_name, owner)
    state: dict = {"owner": owner, "node": base, "cortexes": [], "apps": [], "task_id": task_id}

    def _event(msg: str) -> None:
        if task_id:
            _call(agent_name, owner, "POST", f"/v1/agents/{agent_name}/tasks/{task_id}/event",
                  {"type": "progress", "message": msg[:300]})

    # ── discovery: read the REAL lib + a real manifest, so the agent doesn't guess ──
    @tool("read_lib_api")
    def read_lib_api(lib_name: str) -> str:
        """Read the public API surface of an AIMEAT browser lib (its self-documenting header), so you
        author against the REAL methods rather than stale docs. lib_name e.g. 'aimeat-auth',
        'aimeat-data', 'aimeat-storage', 'aimeat-ai'. (Reminder from the field: this node's
        aimeat-auth.js has login()/loginWithPassword()/getSession() — there is NO ensureSession()."""
        if not base:
            return "ERROR: no node url (agent token missing?)"
        name = lib_name if lib_name.endswith(".js") else f"{lib_name}.js"
        try:
            r = requests.get(f"{base}/v1/libs/{name}", timeout=AUTHOR_TIMEOUT)
        except Exception as e:  # noqa: BLE001
            return f"ERROR: {e!r}"
        if r.status_code != 200:
            return f"HTTP {r.status_code} for /v1/libs/{name}"
        lines = r.text.splitlines()
        # header comments + the method signatures (async name( … ) and  name: )
        import re
        sig = [ln for ln in lines if re.match(r"\s*(async\s+)?[a-zA-Z_]+\s*\(", ln) or re.match(r"\s*[a-zA-Z_]+\s*:\s*", ln)]
        head = "\n".join(lines[:40])
        return f"// /v1/libs/{name} — header + method signatures\n{head}\n…\nSIGNATURES:\n" + "\n".join(sig[:60])

    @tool("read_cortex_example")
    def read_cortex_example(name: str = "") -> str:
        """Export a real installed cortex's manifest to copy its EXACT schema (apiVersion:
        cortex.aimeat.org/v1, kind: Extension, metadata{name,namespace,...}, spec{version,components[]};
        a lib component is {type: lib, name, filename, exports, api_surface}). Pass a name, or leave
        blank to pick one automatically."""
        lst = _call(agent_name, owner, "GET", "/v1/cortex")
        if not _ok(lst):
            return f"could not list cortexes: {_err(lst)}"
        exts = (lst.get("data") or {}).get("extensions") or (lst.get("data") or {}).get("items") or []
        target = name or (exts[0].get("name") if exts else "")
        if not target:
            return "no cortexes installed to use as an example"
        ex = _call(agent_name, owner, "GET", f"/v1/cortex/{target}/export")
        if not _ok(ex):
            return f"export failed for {target}: {_err(ex)}"
        return (ex.get("data") or {}).get("manifest", "(no manifest in export)")

    @tool("read_node_api")
    def read_node_api(path: str) -> str:
        """DISCOVER the real AIMEAT API + read LIVE data before authoring (authenticated as the agent,
        which is owner-scoped — it can see the owner's whole fleet). Use this to build on REAL data, not
        seeded examples. Useful paths: 'llms.txt' (protocol/library overview for agents), '/' (node id +
        top-level routes), '/v1/agents' (the owner's REAL agent roster — name, mode, capabilities,
        last_seen), '/v1/agents/<name>/tasks?status=done' (an agent's real tasks + outputs), '/v1/docs'
        (OpenAPI), '/v1/catalogue', '/v1/libs'. Returns the HTTP status + the response body (truncated).
        For the in-browser app, the equivalent live calls are session.fetch('/v1/agents') etc., and the
        AIMEAT.agents / AIMEAT.data libs — read_lib_api('aimeat-agents') to see that API."""
        if not base:
            return "ERROR: no node url (agent token missing?)"
        tok2, _u = _token(agent_name, owner)
        p = path.strip()
        if not p.startswith("http"):
            p = "/" + p.lstrip("/")
        url = p if p.startswith("http") else base + p
        try:
            r = requests.get(url, headers={"Authorization": f"Bearer {tok2}"}, timeout=AUTHOR_TIMEOUT)
        except Exception as e:  # noqa: BLE001
            return f"ERROR: {e!r}"
        body = (r.text or "")[:2600]
        return f"GET {p} -> {r.status_code}\n{body}"

    # ── install the cortex (author supplies manifest YAML + libs) ──
    @tool("install_cortex")
    def install_cortex(name: str, manifest_yaml: str, libs_json: str) -> str:
        """Install + activate a cortex. manifest_yaml = the full k8s-style manifest (see
        read_cortex_example). libs_json = a JSON object mapping each lib filename to its JS code,
        e.g. '{"fleetdash.js": "(function(g){…})(this);"}'. Every lib is syntax-checked (node --check)
        BEFORE install; a syntax error returns BLOCKED so you fix it first. Re-installs cleanly if the
        cortex already exists. Returns the served lib URL(s) on success."""
        try:
            libs = json.loads(libs_json) if isinstance(libs_json, str) else dict(libs_json or {})
        except Exception as e:  # noqa: BLE001
            return f"BLOCKED: libs_json is not valid JSON: {e}"
        if not isinstance(libs, dict) or not libs:
            return "BLOCKED: libs_json must be a non-empty JSON object {filename: code}"
        for fn, code in libs.items():
            ok, err = _check_js(code if isinstance(code, str) else "")
            if not ok:
                return f"PRE-INSTALL BLOCKED ({fn}): JS syntax error -> {err}. Fix and resubmit."
        body = {"manifest": manifest_yaml, "libs": libs}
        r = _call(agent_name, owner, "POST", "/v1/cortex", body)
        if r.get("_status") == 409:  # already installed -> redeploy
            _call(agent_name, owner, "POST", f"/v1/cortex/{name}/deactivate")
            _call(agent_name, owner, "DELETE", f"/v1/cortex/{name}")
            r = _call(agent_name, owner, "POST", "/v1/cortex", body)
        if not _ok(r):
            if r.get("_status") == 403:
                return ("INSTALL DENIED (403, owner role required). Cortex install is owner-gated on "
                        "this node — it works once the agent-write grant for /v1/cortex is deployed. "
                        f"Detail: {_err(r)}")
            return f"install failed: {_err(r)}"
        a = _call(agent_name, owner, "POST", f"/v1/cortex/{name}/activate")
        if not _ok(a):
            return f"installed but ACTIVATE failed: {_err(a)}"
        served = [f"{base}/v1/cortex/{name}/libs/{fn}" for fn in libs]
        state["cortexes"].append(name)
        _event(f"installed + activated cortex '{name}' ({len(libs)} lib)")
        return f"OK: cortex '{name}' installed + active. Libs served at: {', '.join(served)}"

    # ── install an extension (server-side WASM: manifest + action scripts) ──
    @tool("install_extension")
    def install_extension(name: str, manifest_yaml: str, scripts_json: str) -> str:
        """Install + activate a server-side EXTENSION (QuickJS WASM sandbox). Use ONLY when the app needs
        server-only work: an external API behind auth/CORS, a scheduled cron job, server-validated writes,
        or task-dispatch to agents. manifest_yaml = metadata{name,version,description,author} + actions[]
        (each: id, method, path '/v1/ext/<name>/<id>', script '<file>.js') + optional schedules/config/
        limits. scripts_json = JSON object mapping each action's script filename to its JS. Each action
        script MUST be a single top-level `export default async function (ctx, input) { ... }` (the sandbox
        allows NO other top-level statements — helpers go inside). The REAL ctx API (verified vs the
        runtime — the published handbook DRIFTS, use THIS): ctx.memory.get(key)/set(key,value)/
        search(prefix,opts)/delete(key)/getPublic(namespace,key) are ALL async (the ext owns its
        ext:<name> namespace). ctx.fetch(url, opts?) -> {status, ok, text, headers} — `text` is a STRING,
        so parse with JSON.parse(res.text); there is NO res.json() (calling it throws 'not a function').
        ctx.log.* and ctx.notify(msg, opts?). There is NO ctx.api / ctx.task — never call ctx.api.post.
        Example action: `export default async function (ctx, input) { const res = await ctx.fetch(url);
        const data = JSON.parse(res.text); await ctx.memory.set('prices', data); return {ok:true}; }`"""
        try:
            scripts = json.loads(scripts_json) if isinstance(scripts_json, str) else dict(scripts_json or {})
        except Exception as e:  # noqa: BLE001
            return f"BLOCKED: scripts_json is not valid JSON: {e}"
        if not isinstance(scripts, dict) or not scripts:
            return "BLOCKED: scripts_json must be a non-empty JSON object {filename: code}"
        # Pre-validate the manifest shape — the server's bare "actions array required" hasn't been enough
        # for the agent to self-correct, so fail with the EXACT template + the precise problem.
        _EX = ("metadata:\n  name: " + name + "\n  version: 0.1.0\n  description: <what it does>\n"
               "  author: <you>\nactions:\n  - id: refresh\n    method: POST\n    path: /v1/ext/" + name +
               "/refresh\n    script: refresh.js\nschedules:\n  - id: refresh\n    cron: \"*/10 * * * *\"\n"
               "    script: refresh.js\n# scripts_json = {\"refresh.js\": \"export default async function (ctx, input) { ... }\"}")
        try:
            import yaml as _yaml
            man = _yaml.safe_load(manifest_yaml) if isinstance(manifest_yaml, str) else manifest_yaml
        except Exception as e:  # noqa: BLE001
            return f"BLOCKED: manifest_yaml is not valid YAML ({e}). It must be a YAML STRING shaped like:\n{_EX}"
        if not isinstance(man, dict):
            return f"BLOCKED: manifest_yaml must be a YAML string that parses to a mapping. Shape:\n{_EX}"
        acts = man.get("actions")
        if not isinstance(acts, list) or not acts:
            return ("BLOCKED: the manifest needs a non-empty TOP-LEVEL 'actions:' list (not nested under "
                    f"metadata). Each action: id, method, path '/v1/ext/{name}/<id>', script. Shape:\n{_EX}")
        for ai, act in enumerate(acts):
            if not isinstance(act, dict):
                return f"BLOCKED: actions[{ai}] must be a mapping with id/method/path/script. Shape:\n{_EX}"
            miss = [k for k in ("id", "method", "path", "script") if not act.get(k)]
            if miss:
                return f"BLOCKED: actions[{ai}] is missing {miss}. Each action needs id/method/path/script. Shape:\n{_EX}"
            if act["script"] not in scripts:
                return (f"BLOCKED: actions[{ai}].script '{act['script']}' is not a key in scripts_json "
                        f"(keys present: {list(scripts)}). Put the action's code under that exact filename in scripts_json.")
        body = {"manifest": manifest_yaml, "scripts": scripts}
        r = _call(agent_name, owner, "POST", "/v1/extensions", body)
        if r.get("_status") == 409:  # already installed -> redeploy
            _call(agent_name, owner, "POST", f"/v1/extensions/{name}/deactivate")
            _call(agent_name, owner, "DELETE", f"/v1/extensions/{name}")
            r = _call(agent_name, owner, "POST", "/v1/extensions", body)
        if not _ok(r):
            if r.get("_status") == 403:
                return ("INSTALL DENIED (403). Extension install is owner-gated on this node "
                        "(POST /v1/extensions still checks owner role). It works once that route is opened "
                        f"to the ext:write scope (like /v1/cortex was). Detail: {_err(r)}")
            return f"extension install failed: {_err(r)}"
        a = _call(agent_name, owner, "POST", f"/v1/extensions/{name}/activate")
        if not _ok(a):
            return f"extension installed but ACTIVATE failed: {_err(a)}"
        state.setdefault("extensions", []).append(name)
        _event(f"installed + activated extension '{name}'")
        acts = [str(x.get("id")) for x in ((r.get("data") or {}).get("actions") or [])]
        return f"OK: extension '{name}' installed + active. Actions: {acts}. Invoke via POST /v1/ext/{name}/<action>."

    @tool("invoke_extension")
    def invoke_extension(name: str, action: str, input_json: str = "{}") -> str:
        """Call an extension action (to smoke-test it): POST /v1/ext/<name>/<action> with the JSON input.
        Returns the action's result. Use after install_extension to confirm the server logic works."""
        try:
            inp = json.loads(input_json) if isinstance(input_json, str) else (input_json or {})
        except Exception as e:  # noqa: BLE001
            return f"BLOCKED: input_json is not valid JSON: {e}"
        r = _call(agent_name, owner, "POST", f"/v1/ext/{name}/{action}", inp)
        if not _ok(r):
            return f"invoke failed: {_err(r)}"
        return f"OK: {json.dumps(r.get('data'))[:400]}"

    # ── publish the app (INLINE — never presigned; it keys the owner wrong) ──
    @tool("publish_app")
    def publish_app(filename: str, html: str, name: str = "", description: str = "",
                    category: str = "utility", icon: str = "", uses_cortex_json: str = "[]") -> str:
        """Publish (or update) an app via the INLINE path (base64 content) — do NOT use presigned, it
        keys the owner wrong and serves a stale version. The app's inline <script> is syntax-checked
        before publish. filename must be the canonical name (e.g. 'fleet-activity-dashboard.html') and
        is reused on every update. uses_cortex_json = JSON array of cortex names the app loads.
        Returns the live inline URL (served under the OWNER)."""
        js = _extract_inline_js(html)
        ok, err = _check_js(js)
        if not ok:
            return f"PRE-PUBLISH BLOCKED: app inline <script> has a JS syntax error -> {err}. Fix and resubmit."
        # Structural gate: any app that touches AIMEAT MUST loadScript the base libs, or the AIMEAT
        # global is undefined at runtime ("AIMEAT is not defined") — even an app that only calls a cortex
        # needs aimeat-data.js (the cortex reads memory through it) and aimeat-auth.js (the session).
        if "AIMEAT" in html:
            missing = [lib for lib in ("aimeat-auth.js", "aimeat-data.js") if lib not in html]
            if missing:
                return ("PRE-PUBLISH BLOCKED: the app uses AIMEAT but does not loadScript "
                        + " and ".join("/v1/libs/" + m for m in missing)
                        + ". Load /v1/libs/aimeat-auth.js, then /v1/libs/aimeat-data.js, THEN the cortex lib "
                        "(awaiting each) before any AIMEAT call. Fix and resubmit.")
        # A cortex the app loads may itself use AIMEAT.<lib> (agents/storage/ai/...). The APP must load
        # those libs too, or the cortex call throws "Cannot read properties of undefined (reading ...)".
        import re as _re
        LIBMAP = {
            "AIMEAT.agents": "aimeat-agents.js", "AIMEAT.storage": "aimeat-storage.js",
            "AIMEAT.ai": "aimeat-ai.js", "AIMEAT.social": "aimeat-social.js",
            "AIMEAT.wallet": "aimeat-wallet.js", "AIMEAT.work": "aimeat-work.js",
            "AIMEAT.capabilities": "aimeat-capabilities.js", "AIMEAT.speech": "aimeat-speech.js",
            "AIMEAT.audio": "aimeat-audio.js",
        }
        blob = html
        for cname, cfile in _re.findall(r"/v1/cortex/([a-zA-Z0-9_-]+)/libs/([a-zA-Z0-9_.-]+)", html):
            try:
                blob += "\n" + requests.get(f"{base}/v1/cortex/{cname}/libs/{cfile}", timeout=20).text
            except Exception:  # noqa: BLE001
                pass
        dep_missing = [f"/v1/libs/{lib} (used: {ns})" for ns, lib in LIBMAP.items() if ns in blob and lib not in html]
        if dep_missing:
            return ("PRE-PUBLISH BLOCKED: the app (or a cortex it loads) uses these AIMEAT libs but the "
                    "app does not loadScript them: " + ", ".join(dep_missing) + ". Add a loadScript for "
                    "each (awaited, before use) — e.g. AIMEAT.agents.list() needs /v1/libs/aimeat-agents.js. "
                    "Fix and resubmit.")
        try:
            uses_cortex = json.loads(uses_cortex_json) if isinstance(uses_cortex_json, str) else list(uses_cortex_json or [])
        except Exception:  # noqa: BLE001
            uses_cortex = []
        # delete any prior version so a stale one can't shadow the update
        _call(agent_name, owner, "DELETE", f"/v1/apps/{filename}")
        meta = {
            "filename": filename,
            "content": base64.b64encode(html.encode("utf-8")).decode(),
            "name": name or filename.replace(".html", ""),
            "description": description,
            "category": category,
            "tags": [],
            "uses_cortex": uses_cortex,
        }
        if icon:
            meta["icon"] = icon
        r = _call(agent_name, owner, "POST", "/v1/apps", meta)
        if not _ok(r):
            return f"publish failed: {_err(r)}"
        url = f"{base}/v1/apps/{owner}/{filename}?mode=inline"
        state["apps"].append(filename)
        _event(f"published app '{filename}'")
        return f"OK: app published. Live (logged-in owner): {url}"

    @tool("seed_memory")
    def seed_memory(key: str, value_json: str, visibility: str = "public") -> str:
        """Seed an example/contract memory entry the app will read, e.g. an agent activity entry:
        key='activity.web-researcher.demo1', value_json='{"agentName":"web-researcher","topic":"research",
        "latestOutput":"…","writtenAt":"2026-06-02T14:00:00Z"}'. visibility 'public' lets anonymous
        viewers see it; 'private'/'owner' keeps it to the logged-in owner."""
        try:
            value = json.loads(value_json) if isinstance(value_json, str) else value_json
        except Exception as e:  # noqa: BLE001
            return f"BLOCKED: value_json is not valid JSON: {e}"
        r = _call(agent_name, owner, "POST", "/v1/memory", {"key": key, "value": value, "visibility": visibility})
        if not _ok(r):
            return f"seed failed for {key}: {_err(r)}"
        return f"OK: seeded {key} ({visibility})"

    @tool("app_inline_url")
    def app_inline_url(filename: str) -> str:
        """Return the live inline URL for a published app (served under the owner)."""
        return f"{base}/v1/apps/{owner}/{filename}?mode=inline"

    @tool("read_app_stack")
    def read_app_stack(url: str) -> str:
        """RUN THIS FIRST before EDITING any existing app. Given the app's inline URL, it (1) CONFIRMS the
        app still exists via the apps API and (2) MAPS its whole stack, so an edit targets the right
        artifacts and never touches the wrong cortex/extension. Returns: the app (filename/owner/size +
        which base libs it loads), every cortex it loads (name, lib file, exported method names, which
        extensions that cortex calls), every extension involved (name, exists?, action ids), and the
        memory key-prefix hints it uses. ABORTS if the URL is not an app URL or the app does not exist —
        so you never edit blind. Only modify artifacts that appear in this map."""
        import re as _re, urllib.parse as _up, json as _json
        m = _re.search(r"/v1/apps/([^/]+)/([^/?#]+)", url or "")
        if not m:
            return ("BLOCKED: not an app URL. Provide the app's inline URL, e.g. "
                    f"{base}/v1/apps/{owner}/<file>.html?mode=inline")
        app_owner, filename = _up.unquote(m.group(1)), _up.unquote(m.group(2))
        try:
            g = requests.get(f"{base}/v1/apps/{app_owner}/{filename}?mode=inline", timeout=AUTHOR_TIMEOUT)
        except Exception as e:  # noqa: BLE001
            return f"ERROR fetching {url}: {e!r}"
        if g.status_code != 200:
            return (f"APP NOT FOUND (HTTP {g.status_code}) at {url}. Confirm the URL — REFUSING to proceed "
                    "so no wrong app/cortex/extension is edited.")
        html = g.text
        blob = html
        cortexes, ext_names = [], set()
        for cname, cfile in sorted(set(_re.findall(r"/v1/cortex/([a-zA-Z0-9_-]+)/libs/([a-zA-Z0-9_.-]+)", html))):
            try:
                lib = requests.get(f"{base}/v1/cortex/{cname}/libs/{cfile}", timeout=AUTHOR_TIMEOUT).text
            except Exception:  # noqa: BLE001
                lib = ""
            blob += "\n" + lib
            mfound = _re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s*:\s*(?:async\s*)?function|async\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", lib)
            methods = sorted({a or b for a, b in mfound if (a or b)})[:20]
            calls = sorted(set(_re.findall(r"callExt\(['\"]([a-zA-Z0-9_-]+)['\"]", lib) + _re.findall(r"/v1/ext/([a-zA-Z0-9_-]+)/", lib)))
            ext_names.update(calls)
            cortexes.append({"name": cname, "lib": cfile, "methods": methods, "calls_ext": calls})
        exts = []
        for en in sorted(ext_names):
            er = _call(agent_name, owner, "GET", f"/v1/extensions/{en}")
            if er.get("_status") != 200:
                continue  # regex caught an action name, not a real extension — skip the noise
            e = (er.get("data") or {}).get("extension") or {}
            exts.append({"name": en, "actions": [a.get("id") for a in (e.get("actions") or [])]})
        prefixes = sorted(set(_re.findall(r"prefix['\"]?\s*:\s*['\"]([a-zA-Z0-9_.-]+)", blob)
                              + _re.findall(r"\.get(?:Public)?\(['\"]([a-zA-Z0-9_.-]+)['\"]", blob)))[:12]
        return "APP STACK (edit ONLY artifacts that appear here — never anything else):\n" + _json.dumps({
            "app": {"filename": filename, "owner": app_owner, "size_bytes": len(html),
                    "loads_auth": "aimeat-auth.js" in html, "loads_data": "aimeat-data.js" in html},
            "cortexes": cortexes,
            "extensions": exts,
            "memory_key_hints": prefixes,
        }, indent=1)[:2200]

    @tool("verify_render")
    def verify_render(filename: str, expect_csv: str = "") -> str:
        """DETERMINISTIC authed render gate — the real proof the app works for a logged-in owner. Loads the
        published app headless, logs in with the owner credentials from env (AIMEAT_APP_LOGIN_USER /
        AIMEAT_APP_LOGIN_PASSWORD), reloads, and checks that real content renders with NO console errors and
        NO leaked raw i18n keys. Pass expect_csv = comma-separated strings that MUST appear in the rendered
        text (e.g. seeded agent names like 'web-researcher,data-analyst') to assert the data actually shows.
        Returns 'VERIFY PASS' or 'VERIFY FAIL ...'. The password stays in env and is never echoed."""
        import os
        from crewaimeat.app_verify import app_renders_authed
        u = os.getenv("AIMEAT_APP_LOGIN_USER")
        pw = os.getenv("AIMEAT_APP_LOGIN_PASSWORD")
        if not u or not pw:
            return ("VERIFY SKIPPED: AIMEAT_APP_LOGIN_USER / AIMEAT_APP_LOGIN_PASSWORD not set in env — "
                    "cannot log in to check the authed view.")
        url = f"{base}/v1/apps/{owner}/{filename}?mode=inline"
        expect = [x.strip() for x in expect_csv.split(",") if x.strip()] or None
        r = app_renders_authed(url, u, pw, expect_any=expect)
        if r.get("ok") is None:
            return f"VERIFY SKIPPED: {r.get('skipped')}"
        if r.get("ok"):
            return f"VERIFY PASS: logged-in render OK, real content present. sample: {str(r.get('content_sample',''))[:220]}"
        return (f"VERIFY FAIL: login={r.get('login')} | "
                f"failed_resources(404/403/5xx)={r.get('failed_resources')} | "
                f"console_errors={r.get('console_errors')} | raw_i18n_keys={r.get('raw_i18n_keys')} | "
                f"content_sample={str(r.get('content_sample',''))[:200]}")

    tools = [read_lib_api, read_cortex_example, read_node_api, read_app_stack, install_cortex,
             install_extension, invoke_extension, publish_app, seed_memory, app_inline_url, verify_render]
    # Side-effecting / live-state tools must NOT be cached. crewai caches tool results by args, which
    # would serve a STALE verdict across fix-loop iterations (observed: verify_render "(from cache)"
    # returning the pre-fix FAIL after a re-publish). The read-only discovery tools may cache.
    for _t in (install_cortex, install_extension, invoke_extension, publish_app, seed_memory, verify_render, read_node_api, read_app_stack):
        try:
            _t.cache_function = lambda *_a, **_k: False
        except Exception:  # noqa: BLE001
            pass
    return tools, state

# AIMEAT app authoring guide (direct build — no generator)

> Crew-side operational guide for the `aimeat-app-builder` / `aimeat-cortex-fixer` crews.
> Build AIMEAT apps the way comic-land was built: **author a cortex + app directly** and install
> them via REST. No generator pipeline. Field-proven 2026-06-02 (fleet-activity-dashboard:
> 5 cards, topic filter, authed render, zero console errors). See the memory note
> [[aimeat-direct-build-pattern]]. The canonical AIMEAT docs are
> `aimeat-protocol/docs/guides/building-extension-cortex-app-stack.md` +
> `agent-data-dashboard-cookbook.md` — but they drift from the deployed node, so **read the live
> lib APIs and export a real cortex** instead of trusting any doc (this guide included).

## Architecture (who calls whom)
```
APP (HTML/CSS/JS)  → calls ONLY cortex methods (+ AIMEAT.auth/AIMEAT.data for boot)
   ↓
CORTEX lib (browser IIFE on AIMEAT.<name>) → AIMEAT.data read/write owner memory; callExt only if needed
   ↓
EXTENSION (server WASM)  → external HTTP, cron, server-validated writes, task dispatch   [OPTIONAL]
```
- **The app is presentation only.** It never touches raw memory routes or `/v1/ext` directly.
- **The cortex is the single API** the app talks to. One coherent author = consistent keys end to end.
- **Skip the extension** for any app that reads/writes the OWNER's own data with no external API, no
  cron, no cross-user sharing. (Cortex + app is the floor; pure-app-with-AIMEAT.data is even simpler.)

## Build flow (what the builder agent does)
1. **read_lib_api('aimeat-auth')** + **read_lib_api('aimeat-data')** — author against the REAL methods.
1b. **read_app_template()** — the canonical AIMEAT app skeleton (fetched live from `llms.txt`). **Start
   every app from it.** It already wires auth right — loads `aimeat-auth.js` + `aimeat-data.js`, mounts the
   login bar (`AIMEAT.auth.mountLoginButton('#header-auth', {onLogin, onLogout})`), and runs
   `startApp(session)` **only after** `const session = await AIMEAT.auth.login();`. That ordering PREVENTS
   the boot-order race (`Not logged in. Call AIMEAT.auth.login() first.`). Both styles are supported: keep
   the template's `<nav>` login bar (recommended), or for a clean no-login-bar look drop the `<nav>` bar +
   `mountLoginButton` but **keep** the `boot()`/await-login/`startApp` order. Proven end-to-end 2026-06-03.
2. **read_cortex_example()** — copy the EXACT manifest schema.
3. **Design the memory key map** (the contract with the data-producing agents): ONE prefix, flat shape,
   e.g. `activity.<agentName>.<id>` = `{agentName, topic, latestOutput, writtenAt(ISO)}`.
4. **Author the cortex lib** (IIFE on `AIMEAT.<name>`, uses `AIMEAT.data` directly) + its manifest YAML.
5. **install_cortex(name, manifest_yaml, libs_json)** — syntax-gated; installs + activates.
6. **Author the app HTML** — start from the step-1b template; in `boot()` add a `loadScript` for the cortex
   + every `AIMEAT.<lib>` it uses; put all rendering inside `startApp(session)`, calling cortex methods only
   (plus `session.fetch(path)` for raw reads — returns parsed JSON, no `.json()`). The template's
   tailwind/daisyui CDN is fine (CSP permits it); never use `eval`/`new Function` (unsafe-eval is blocked).
7. **publish_app(...)** — inline publish; returns the live URL.
8. **seed_memory(...)** — a few example entries so it shows content + demonstrates the contract.
9. **Verify**: delegate an authed browser walkthrough to **web-tester** (logs in, checks real content).

## The REAL cortex manifest (k8s-style — the docs' simplified `spec_version/name/libs` is REJECTED)
```yaml
apiVersion: cortex.aimeat.org/v1
kind: Extension
metadata:
  name: fleetdash
  namespace: community          # or the owner's name; operators may use any
  description: "…"
  author: <you>
  tags: [dashboard, fleet]
  labels: { domain: fleet-activity }
spec:
  version: "1.0.0"
  license: MIT
  components:
    - type: lib
      name: fleetdash
      filename: fleetdash.js     # MUST match the libs_json key
      exports: [entries, latestPerAgent, topics]
      api_surface: |
        AIMEAT.fleetdash.entries(topic) -> [...]
```

## REST contracts
- **Cortex install**: `POST /v1/cortex` JSON `{manifest:"<yaml>", libs:{"fleetdash.js":"<code>"}}` →
  `POST /v1/cortex/<name>/activate`. Lib served at `/v1/cortex/<name>/libs/<file>`. Re-deploy =
  deactivate→delete→install (re-activate alone is idempotent/skips). **Owner-gated** (`requireRole owner`)
  until the agent-write grant is deployed — an agent token gets 403 until then.
- **App publish**: `POST /v1/apps` `{filename, content: base64(html), name, version, category, icon,
  uses_cortex:[...]}` — **INLINE, not presigned** (presigned keys the owner as the full GAII and the
  GET-by-owner resolver then serves a STALE version). Delete first to drop stale versions. Served at
  `/v1/apps/<owner>/<file>.html?mode=inline` — under the OWNER even when an agent publishes it.
  App publish works for agents today (no grant needed).

## Live lib APIs (verified on the node — trust these over docs)
- **auth**: `await AIMEAT.auth.login(username?)` restores a stored session (null if none);
  `loginWithPassword(user, pass)`; `getSession()` (sync). **There is NO `ensureSession()`** — the
  cookbook is ahead of the deployed lib. App boot: `session = (await AIMEAT.auth.login()) || AIMEAT.auth.getSession()`.
- **data**: `data.list({prefix})` → `{items:[{key, value, owner_gaii, …}]}` (needs a session);
  `get(key)` (auto public-fallback to the app-creator namespace via the `/v1/apps/<creator>/` path);
  `getPublic(gaii, key)`; `set(key, value, {visibility:'public'|'private'})`.

## Gotchas (the field lessons — each one cost a real bug)
1. **Inline publish, never presigned.** (owner-keying bug → stale serve)
2. **`login()`/`getSession()`, never `ensureSession()`.** (doc drift; caught by the render gate) The
   one correct boot line: `let session = (await AIMEAT.auth.login()) || AIMEAT.auth.getSession();` —
   `login()` is **async** and restores the saved session **after a reload**; `getSession()` is
   **synchronous** (returns the session or null) — never `.then()` it (throws *"Cannot read properties
   of null (reading 'then')"* and the app sticks on the login screen). Login form button:
   `await AIMEAT.auth.loginWithPassword(u, p); location.reload();`.
3. **One consistent key prefix.** Read keys exactly as you write them — never a bare key vs a prefixed one.
4. **No external CDN scripts** — the inline-app CSP blocks them. Load only `/v1/libs`, `/v1/cortex`,
   same-origin. Inline your CSS.
5. **One canonical app filename**, reused on every publish; document it at the top of the HTML.
6. **Escape every interpolated string** in the app (XSS + `[object Object]` / raw-key leaks).
7. **`session.fetch` returns parsed JSON** — use `.data`, don't call `.json()`.
8. **Extension install + cortex install are owner-gated.** Prefer no extension; for cortex, the crew
   needs the agent-write grant deployed.

## Gates (the deterministic "catch")
- **Syntax** (`node --check`) on the cortex lib + the app's inline `<script>` — runs INSIDE
  `install_cortex` / `publish_app` (returns `PRE-INSTALL/PRE-PUBLISH BLOCKED` to fix before shipping).
- **Render** (Playwright headless) — loads the app, asserts no console errors + real content.
- **Authed content** — delegated to the **web-tester** crew (logs in as the owner, walks each feature).
  This is the real proof; the local render only proves "loads without errors".

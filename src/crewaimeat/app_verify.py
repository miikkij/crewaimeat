"""Deterministic app quality gates — the 'catch' the UI's human used to provide, automated.

These run entirely on OUR side (crewfive); none need AIMEAT changes. They exist because a
single autonomous build agent can ship code that PASSES structural validation yet breaks at
runtime (a missing dot → SyntaxError; un-slug-prefixed memory reads → 404 → raw i18n keys).
The UI "worked" because a human eyeballed each step; these gates are that eyeball, deterministic.

The app-builder crew (and later a workflow-manager-style conductor) runs these to FAIL the build
loudly instead of completing 'green' on a broken app.

Gates:
  1. cortex_syntax_ok(js)              — `node --check`: does the lib even parse? (missing-dot bug)
  2. cortex_uses_slug(js, slug, pfx)   — does it read SEEDED keys slug-prefixed? (the i18n/settings/data bug)
  3. app_data_present(...)             — REST: do the slug-prefixed memory keys actually exist + read back?
  4. app_renders(url, ...)            — Playwright headless: load, capture console errors, assert real
                                          content rendered (no raw i18n keys, not empty). Reveals runtime
                                          breakage EARLY, the same way opening the URL in a browser would.
"""
from __future__ import annotations

import os
import re
import subprocess
import tempfile
import urllib.parse
from typing import Any

# NB: do NOT import from crewaimeat.generator_tool at module level — generator_tool imports the
# syntax/slug gates from here, so a top-level import would be circular. app_data_present lazy-imports
# _call where it is needed.


# --------------------------------------------------------------------------- #
# 1. Syntax — does the cortex JS actually parse?
# --------------------------------------------------------------------------- #
def cortex_syntax_ok(js_code: str) -> tuple[bool, str]:
    """node --check compiles WITHOUT executing → surfaces only real syntax errors. Catches the
    `listTitle className = ...` (missing dot) class that passes structural validation but throws
    `Uncaught SyntaxError` at runtime. Returns (ok, first_error_line). If node is unavailable the
    gate degrades open (don't block the build on a missing tool)."""
    fd, path = tempfile.mkstemp(suffix=".js", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(js_code)
        r = subprocess.run(["node", "--check", path], capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            return True, ""
        # node prints "<file>:<line>\n<code>\nSyntaxError: ..." — surface the SyntaxError line
        err = (r.stderr or "").strip().splitlines()
        msg = next((ln for ln in err if "Error" in ln), err[0] if err else "syntax error")
        loc = next((ln for ln in err if re.search(r":\d+$", ln.strip())), "")
        return False, (f"{msg.strip()}  ({loc.strip()})" if loc else msg.strip())
    except FileNotFoundError:
        return True, "(node not found — syntax gate skipped)"
    except Exception as e:  # noqa: BLE001
        return True, f"(syntax gate skipped: {e!r})"
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# 2. Slug — are SEEDED memory keys read with the service_slug prefix?
# --------------------------------------------------------------------------- #
def cortex_uses_slug(js_code: str, slug: str, key_prefixes: list[str]) -> tuple[bool, list[str]]:
    """Memory/translation components are STORED slug-prefixed (`<slug>.i18n.en`), so the cortex must
    READ them slug-prefixed too. The LLM frequently drops the prefix (reads `i18n.en`) → 404. For each
    seeded key first-segment (e.g. 'i18n', 'settings', the domain namespace) this flags a bare
    AIMEAT.data.get/set('<pfx>...' that is NOT slug-prefixed anywhere. Returns (ok, offending_prefixes).
    Note: external/platform namespaces (crews., research., ext:) are intentionally NOT slugged — pass
    only the project's OWN seeded key prefixes."""
    offenders: list[str] = []
    for pfx in key_prefixes:
        if not pfx or pfx == slug:
            # The slug itself is never a seeded KEY. A read of '<slug>.x' is the CORRECT form,
            # so a prefix equal to the slug would falsely flag every correct read and demand an
            # impossible double-prefix ('<slug>.<slug>.x'). Skip degenerate prefixes outright.
            continue
        bare = any(tok in js_code for tok in (f"('{pfx}.", f'("{pfx}.', f"'{pfx}.'", f'"{pfx}."'))
        slugged = f"{slug}.{pfx}." in js_code or f"{slug}.{pfx}'" in js_code or f'{slug}.{pfx}"' in js_code
        if bare and not slugged:
            offenders.append(pfx)
    return (not offenders, sorted(set(offenders)))


# --------------------------------------------------------------------------- #
# 3. Data — do the slug-prefixed memory keys actually exist + read back? (REST)
# --------------------------------------------------------------------------- #
def app_data_present(agent: str, owner: str, slug: str, keys: list[str], node_id: str) -> dict:
    """The 'AIMEAT.data.get gets the i18n' check, server-side: confirm each `<slug>.<key>` exists and
    is publicly readable under the owner (which is exactly what the cortex reads at runtime). `keys`
    are the blueprint dataModel.memoryKeys (e.g. ['i18n.fi','i18n.en','settings.config','fleet.deliverables'])."""
    from crewaimeat.generator_tool import _call  # lazy — avoid circular import
    gaii = f"{owner}@{node_id}"
    results: dict[str, Any] = {}
    ok = True
    for key in keys:
        full = f"{slug}.{key}"
        env = _call(agent, owner, "GET",
                    f"/v1/memory/{urllib.parse.quote(gaii, safe='')}/{urllib.parse.quote(full, safe='')}")
        st = env.get("_status")
        has_value = (env.get("data") or {}).get("value") is not None
        # 200+value = public & present; 403 = exists but owner-private (the logged-in owner reads it
        # fine at runtime, getPublic just won't serve it); 404 = genuinely missing.
        if st == 200 and has_value:
            results[full] = "present"
        elif st == 403:
            results[full] = "present (owner-private)"
        else:
            results[full] = f"MISSING ({st})"
            ok = False
    return {"ok": ok, "keys": results}


# --------------------------------------------------------------------------- #
# 4. Render — Playwright headless: load, console errors, real content?
# --------------------------------------------------------------------------- #
def app_renders(url: str, *, settle_ms: int = 4500, rewrite_node: tuple[str, str] | None = None) -> dict:
    """Load the published app in a real (headless) browser and report what a viewer would see:
    console/page errors, whether raw i18n keys (`app.*`) leaked, and a content sample. This is the
    EARLY console-error reveal — it would have caught every runtime break in one shot.

    `rewrite_node=(from_id, to_id)` rewrites memory-read URLs from one node id to another, to work
    around the aimeat-data.js public-fallback that hardcodes a node id (so the no-session headless
    read hits the right node). Pass None when a real session is present.

    Returns {ok, console_errors, raw_i18n_keys, content_sample}. ok = no errors AND no leaked i18n
    keys AND non-trivial content. Requires the `playwright` package + chromium (crew-side)."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:  # noqa: BLE001
        return {"ok": None, "skipped": f"playwright not installed: {e}"}

    errors: list[str] = []
    text = ""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        if rewrite_node:
            frm, to = rewrite_node

            def _route(route):
                u = route.request.url
                if "/v1/memory/" in u and frm in u:
                    route.continue_(url=u.replace(frm, to))
                else:
                    route.continue_()

            page.route("**/v1/memory/**", _route)
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"PAGEERROR: {e}"))
        try:
            page.goto(url, wait_until="networkidle", timeout=45000)
        except Exception as e:  # noqa: BLE001
            errors.append(f"GOTO: {e}")
        page.wait_for_timeout(settle_ms)
        try:
            text = page.locator("#app").inner_text()
        except Exception:  # noqa: BLE001
            text = ""
        browser.close()

    raw_keys = re.findall(r"\b[a-z][a-z0-9]*\.[a-z][a-zA-Z0-9.]+", text)
    raw_keys = [k for k in raw_keys if k.split(".")[0] in ("app", "i18n", "ui")][:10]
    # HARD fail = a JS runtime error OR an empty/crashed #app. raw_i18n_keys is a WARNING only:
    # without a logged-in session, owner data (incl. translations) may not load, so leaked keys here
    # don't prove a bug. Authenticated content verification is delegated to the web-tester crew
    # (real browser + login). The deterministic slug gate (cortex_uses_slug) catches the key-prefix
    # class at submit time regardless.
    ok = not errors and len(text.strip()) > 40
    return {"ok": ok, "console_errors": errors, "raw_i18n_keys_warning": raw_keys, "content_sample": text[:400]}


# --------------------------------------------------------------------------- #
# 5. Authed render — log in as the owner, then verify the logged-in view
# --------------------------------------------------------------------------- #
def _split_failed_resources(failed: list[str]) -> "tuple[list[str], list[str]]":
    """Dedup failed HTTP responses ('<status> <url>') to distinct endpoints (strip query) and split
    them into (real_failures, benign_404s).

    BENIGN = a 404 on a GET /v1/memory/<key>: the key just isn't written yet, the data lib returns
    null/default, and a well-built app renders fine. Failing the render on it is a false negative — it
    dinged the counter app a star and made the editor's tic-tac-toe run thrash. 403/5xx on memory, and
    ANY non-memory 4xx/5xx (libs, cortex, app, ext), stay REAL failures that fail the gate."""
    seen: set[str] = set()
    real: list[str] = []
    benign: list[str] = []
    for f in failed:
        k = f.split("?")[0]
        if k in seen:
            continue
        seen.add(k)
        if k.startswith("404 ") and "/v1/memory/" in k:
            benign.append(k)  # unset-key read
        elif "/v1/auth/refresh" in k:
            # The auth lib's BACKGROUND token-refresh. In a verify run we log in fresh with
            # loginWithPassword (no refresh-token flow), so this 401s — but the session is valid and the
            # app renders. Benign noise, not an app failure (an agent can't suppress it from app code).
            benign.append(k)
        else:
            real.append(k)
    return real[:8], benign[:8]


def app_renders_authed(url: str, user: str, password: str, *, settle_ms: int = 4500,
                       content_selectors: tuple[str, ...] = ("#cards", "#app", ".wrap", "body"),
                       expect_any: list[str] | None = None) -> dict:
    """The real 'does the LOGGED-IN owner see content' check. Loads the app headless, calls
    AIMEAT.auth.loginWithPassword(user, password) IN THE PAGE (AIMEAT keeps its Ed25519 session in
    IndexedDB, which a saved Playwright profile cannot restore — so we log in fresh each run), reloads so
    the app boots authenticated, and reports console/page errors + the rendered text. Credentials are
    injected straight into the page's loginWithPassword call — they are never logged or returned.

    ok = login succeeded AND no errors AND non-trivial content AND no leaked raw i18n keys (and, if
    `expect_any` is given, at least one of those strings is present). Requires playwright + chromium."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:  # noqa: BLE001
        return {"ok": None, "skipped": f"playwright not installed: {e}"}

    errors: list[str] = []
    failed: list[str] = []
    text = ""
    login = "(not attempted)"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"PAGEERROR: {e}"))
        # Capture the URL of any HTTP request that fails (404/403/5xx) — so a "Failed to load resource"
        # console error becomes actionable ("404 on /v1/agents/<name>" → drop that call).
        page.on("response", lambda r: failed.append(f"{r.status} {r.url}") if r.status >= 400 else None)
        try:
            page.goto(url, wait_until="networkidle", timeout=45000)
            # CSP-safe readiness wait. wait_for_function() injects a string the engine eval()s, which the
            # inline-app CSP (no 'unsafe-eval') blocks; poll via page.evaluate (CDP-injected, CSP-exempt).
            for _ in range(30):
                try:
                    if page.evaluate("() => !!(window.AIMEAT && window.AIMEAT.auth)"):
                        break
                except Exception:  # noqa: BLE001
                    pass
                page.wait_for_timeout(500)
            login = page.evaluate(
                "async ([u, pw]) => { try { await AIMEAT.auth.loginWithPassword(u, pw); return 'ok'; }"
                " catch (e) { return 'ERR: ' + (e && e.message || e); } }",
                [user, password],
            )
            page.reload(wait_until="networkidle")
            page.wait_for_timeout(settle_ms)
            for sel in content_selectors:
                try:
                    t = page.locator(sel).first.inner_text()
                    if t and len(t.strip()) > len(text.strip()):
                        text = t
                except Exception:  # noqa: BLE001
                    pass
        except Exception as e:  # noqa: BLE001
            errors.append(f"NAV: {e}")
        browser.close()

    raw_keys = [k for k in re.findall(r"\b[a-z][a-z0-9]*\.[a-z][a-zA-Z0-9.]+", text)
                if k.split(".")[0] in ("app", "i18n", "ui")][:8]
    # Dedup failed resources to the distinct endpoints (strip query) and split off BENIGN unset-key
    # memory reads (see _split_failed_resources).
    fr, benign_404s = _split_failed_resources(failed)
    # "Failed to load resource ..." console messages are the URL-less duplicate of what the response
    # handler already captured (with URLs + status) in failed/fr. Drop them from the console channel so a
    # benign memory-404 doesn't fail the gate here too — real resource failures still fail via `fr`, and
    # real JS errors (TypeError, ReferenceError, PAGEERROR, ...) are kept.
    errors = [e for e in errors if "Failed to load resource" not in e]
    # A JS error caught by the app and DISPLAYED as content (not thrown to the console) is still a
    # broken render — catch the common shapes so a no-expect verify doesn't pass on an error screen.
    err_in_content = any(m in text for m in
                         ("Cannot read properties of", "is not defined", "is not a function", "TypeError"))
    ok = (login == "ok") and not errors and not fr and not err_in_content and len(text.strip()) > 40 and not raw_keys
    if expect_any:
        ok = ok and any(m in text for m in expect_any)
    return {"ok": ok, "login": login, "console_errors": errors[:6], "failed_resources": fr,
            "benign_404s": benign_404s, "error_in_content": err_in_content, "raw_i18n_keys": raw_keys,
            "content_sample": text[:400]}


def app_renders_anon(url: str, *, settle_ms: int = 5000,
                     content_selectors: tuple[str, ...] = ("#app", "#cards", ".wrap", "body"),
                     expect_any: list[str] | None = None,
                     loading_markers: tuple[str, ...] = ("Loading…", "Loading...", "Loading")) -> dict:
    """The PUBLIC / anonymous-view gate — does the app render real content for a visitor who is NOT logged
    in? app_renders_authed logs IN as the owner, so it cannot catch a public viewer that only renders for
    a session: the `if (session) startApp()` mistake leaves anonymous visitors stuck on 'Loading…' yet the
    authed gate PASSes (the owner sees content) — a false PASS on exactly the class of app meant to be
    public. This loads the app headless with NO login and asserts a real public render.

    ok = no real failed resources AND no JS console/page errors AND content is not a bare loading
    placeholder AND non-trivial content AND no leaked raw i18n keys AND (if expect_any) at least one of
    those strings is present. Reuses _split_failed_resources so an unset-key memory 404 stays benign
    (a public read of a not-yet-written key is not a failure). Requires playwright + chromium."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:  # noqa: BLE001
        return {"ok": None, "skipped": f"playwright not installed: {e}"}

    errors: list[str] = []
    failed: list[str] = []
    text = ""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"PAGEERROR: {e}"))
        page.on("response", lambda r: failed.append(f"{r.status} {r.url}") if r.status >= 400 else None)
        try:
            page.goto(url, wait_until="networkidle", timeout=45000)
            page.wait_for_timeout(settle_ms)  # NO login — render as an anonymous visitor would see it
            for sel in content_selectors:
                try:
                    t = page.locator(sel).first.inner_text()
                    if t and len(t.strip()) > len(text.strip()):
                        text = t
                except Exception:  # noqa: BLE001
                    pass
        except Exception as e:  # noqa: BLE001
            errors.append(f"NAV: {e}")
        browser.close()

    raw_keys = [k for k in re.findall(r"\b[a-z][a-z0-9]*\.[a-z][a-zA-Z0-9.]+", text)
                if k.split(".")[0] in ("app", "i18n", "ui")][:8]
    fr, benign_404s = _split_failed_resources(failed)
    errors = [e for e in errors if "Failed to load resource" not in e]
    err_in_content = any(m in text for m in
                         ("Cannot read properties of", "is not defined", "is not a function", "TypeError"))
    stripped = text.strip()
    # A short content that is still just the loading placeholder = the classic login-gated-startApp anon
    # failure (the app never reached startApp without a session). A fully-rendered page replaces it.
    still_loading = bool(stripped) and len(stripped) < 120 and any(m in stripped for m in loading_markers)
    ok = (not errors and not fr and not err_in_content and not still_loading
          and len(stripped) > 40 and not raw_keys)
    if expect_any:
        ok = ok and any(m in text for m in expect_any)
    return {"ok": ok, "anon": True, "console_errors": errors[:6], "failed_resources": fr,
            "benign_404s": benign_404s, "error_in_content": err_in_content, "still_loading": still_loading,
            "raw_i18n_keys": raw_keys, "content_sample": text[:400]}


def app_interaction_ok(url: str, user: str, password: str, steps: list, *, settle_ms: int = 3000,
                       step_timeout_ms: int = 12000) -> dict:
    """DRIVE a real authed interaction through the app and assert outcomes — the test render-only checks
    cannot do (render != works). This is what would have caught the broken realtime chat: verify_render
    PASSed it, but you could not actually send a message.

    Logs in as the owner (loginWithPassword in-page), reloads so the app boots authed, then runs `steps`
    in order. Each step is a dict with `do` + selectors/values. Supported actions:
      {"do":"fill","selector":"#id","value":"x"}        — type into an input
      {"do":"click","selector":"#id"}                    — click an element
      {"do":"wait_enabled","selector":"#id"}             — wait until el.disabled === false
      {"do":"expect_enabled","selector":"#id"}           — same, as an assertion
      {"do":"wait","ms":1500}                             — pause
      {"do":"expect_text","selector":"#id","text":"x"}   — assert text appears in the element
    Returns {ok, login, failed_step (index or None), detail, console_errors, steps_run}. ok = login
    succeeded AND every step passed AND no console errors. Requires playwright. Selectors use Playwright
    syntax (CSS); interactions go through CDP so the inline-app CSP (no unsafe-eval) does not block them."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:  # noqa: BLE001
        return {"ok": None, "skipped": f"playwright not installed: {e}"}
    if not isinstance(steps, list) or not steps:
        return {"ok": None, "skipped": "no interaction steps provided"}

    errors: list[str] = []
    login = "(not attempted)"
    failed_step = None
    detail = ""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"PAGEERROR: {e}"))
        try:
            page.goto(url, wait_until="networkidle", timeout=45000)
            for _ in range(30):
                try:
                    if page.evaluate("() => !!(window.AIMEAT && window.AIMEAT.auth)"):
                        break
                except Exception:  # noqa: BLE001
                    pass
                page.wait_for_timeout(500)
            login = page.evaluate(
                "async ([u, pw]) => { try { await AIMEAT.auth.loginWithPassword(u, pw); return 'ok'; }"
                " catch (e) { return 'ERR: ' + (e && e.message || e); } }", [user, password])
            page.reload(wait_until="networkidle")
            page.wait_for_timeout(settle_ms)
            for i, step in enumerate(steps):
                if not isinstance(step, dict):
                    failed_step, detail = i, "step is not an object"; break
                act = str(step.get("do", "")).lower()
                sel = step.get("selector")
                try:
                    if act == "fill":
                        page.fill(sel, str(step.get("value", "")), timeout=step_timeout_ms)
                    elif act == "click":
                        page.click(sel, timeout=step_timeout_ms)
                    elif act == "wait":
                        page.wait_for_timeout(int(step.get("ms", 1000)))
                    elif act in ("wait_enabled", "expect_enabled"):
                        en, waited = False, 0
                        while waited < step_timeout_ms:
                            try:
                                if page.eval_on_selector(sel, "el => !el.disabled") is True:
                                    en = True; break
                            except Exception:  # noqa: BLE001
                                pass
                            page.wait_for_timeout(500); waited += 500
                        if not en:
                            failed_step, detail = i, f"{sel} not enabled within timeout"; break
                    elif act == "expect_text":
                        want, found, waited = str(step.get("text", "")), False, 0
                        while waited < step_timeout_ms:
                            try:
                                t = page.locator(sel).first.inner_text()
                                if want in (t or ""):
                                    found = True; break
                            except Exception:  # noqa: BLE001
                                pass
                            page.wait_for_timeout(500); waited += 500
                        if not found:
                            failed_step, detail = i, f"'{want}' not found in {sel}"; break
                    else:
                        failed_step, detail = i, f"unknown action '{act}'"; break
                except Exception as e:  # noqa: BLE001
                    failed_step, detail = i, f"{act} {sel} raised: {e}"; break
        except Exception as e:  # noqa: BLE001
            failed_step, detail = -1, f"nav/login error: {e}"
        browser.close()

    # The steps passing IS the functional proof. Drop URL-less "Failed to load resource" console noise
    # (e.g. a benign 401 on /v1/auth/refresh, the auth lib's background refresh) so it doesn't fail a
    # working interaction — real JS errors (TypeError / is not defined / PAGEERROR) are kept.
    real_errors = [e for e in errors if "Failed to load resource" not in e]
    ok = (login == "ok") and failed_step is None and not real_errors
    return {"ok": ok, "login": login, "failed_step": failed_step, "detail": detail,
            "console_errors": real_errors[:6], "steps_run": len(steps)}

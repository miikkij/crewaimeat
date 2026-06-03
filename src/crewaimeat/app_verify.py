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
    # Dedup failed resources to the distinct endpoints (strip query) — the actionable signal.
    seen: set[str] = set()
    fr: list[str] = []
    for f in failed:
        k = f.split("?")[0]
        if k not in seen:
            seen.add(k)
            fr.append(k)
    fr = fr[:8]
    # A JS error caught by the app and DISPLAYED as content (not thrown to the console) is still a
    # broken render — catch the common shapes so a no-expect verify doesn't pass on an error screen.
    err_in_content = any(m in text for m in
                         ("Cannot read properties of", "is not defined", "is not a function", "TypeError"))
    ok = (login == "ok") and not errors and not fr and not err_in_content and len(text.strip()) > 40 and not raw_keys
    if expect_any:
        ok = ok and any(m in text for m in expect_any)
    return {"ok": ok, "login": login, "console_errors": errors[:6], "failed_resources": fr,
            "error_in_content": err_in_content, "raw_i18n_keys": raw_keys, "content_sample": text[:400]}

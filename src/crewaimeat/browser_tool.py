"""Playwright browser tool for crewaimeat — plan-then-execute, concurrency-safe.

A CrewAI tool that runs an ORDERED list of browser actions in ONE headless Chromium session
(navigate / get_content / click / fill / wait / screenshot[+describe]). Designed for the daemon's
concurrent pool: it uses the SYNC Playwright API (each worker thread gets its own driver), launches a
fresh browser per call (no shared browser across threads), and persists login state only when an
explicit ``profile`` is given (``logs/.browser/<profile>.json``) — so the default is a clean, race-free
session. Stateful actions (click/fill) are never blindly retried; the sequence stops on their failure
and returns partial results so the planning agent can adapt. A domain allowlist
(``BROWSER_ALLOWED_DOMAINS``) keeps it from navigating anywhere unintended.

The ``screenshot`` action can optionally ``describe`` the image with a vision-language model
(qwen-vl by default) via OpenRouter — far more useful than OCR for "what is on this page" questions:
it reads layout, buttons, error banners, and rendered state, not just literal text.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

import requests
from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from crewaimeat.ledger_report import report_llm_usage

# Safe-to-retry (idempotent reads/waits); stateful actions must NOT be blindly retried.
_RETRYABLE = {"navigate", "get_content", "wait"}

# Default vision-language model on OpenRouter (qwen-vl). Near-free (~$0.13/M tokens) and strong on
# UI screenshots; override with VISION_MODEL (e.g. qwen/qwen3-vl-8b-instruct is cheaper still).
_DEFAULT_VISION_MODEL = "qwen/qwen3-vl-30b-a3b-instruct"


def _describe_image(path: str, prompt: str) -> str:
    """Describe a screenshot with a vision-language model via OpenRouter.

    Reuses OPENROUTER_API_KEY / OPENROUTER_BASE_URL; the model id comes from VISION_MODEL
    (default qwen-vl, free). Sends the PNG inline as a base64 data URI. Returns the model's
    description, or an explanatory message on any failure (never raises).
    """
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        return "(describe unavailable — OPENROUTER_API_KEY not set)"
    base = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
    # VISION_MODEL is the OpenRouter REST id ("provider/model"); strip a litellm-style "openrouter/" prefix.
    model = os.getenv("VISION_MODEL", _DEFAULT_VISION_MODEL)
    if model.startswith("openrouter/"):
        model = model[len("openrouter/") :]
    try:
        data_uri = "data:image/png;base64," + base64.b64encode(Path(path).read_bytes()).decode()
        body = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                }
            ],
            # Authoritative cost for the ledger (this direct call bypasses CrewAI's hook).
            "usage": {"include": True},
        }
        r = requests.post(
            f"{base}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                # OpenRouter app attribution — without these the dashboard shows App "Unknown".
                # Cosmetic only (does not affect auth or routing), but keeps our usage labelled.
                "HTTP-Referer": "https://github.com/crewaimeat",
                "X-Title": "crewaimeat web-tester",
            },
            json=body,
            timeout=60,
        )
        if r.status_code != 200:
            return f"(describe failed — vision model {model} returned HTTP {r.status_code}: {r.text[:200]})"
        resp_json = r.json()
        report_llm_usage(model, resp_json.get("usage"))
        return (resp_json["choices"][0]["message"]["content"] or "").strip() or "(describe returned empty)"
    except Exception as exc:  # noqa: BLE001 — vision is best-effort; never crash the action
        return f"(describe failed: {exc})"


class BrowserAction(BaseModel):
    action: Literal["navigate", "get_content", "click", "fill", "wait", "screenshot"]
    url: str | None = Field(None, description="URL for navigate / optional pre-nav for get_content")
    selector: str | None = Field(None, description="CSS selector for click / fill")
    value: str | None = Field(None, description="Value to type for fill")
    timeout_ms: int = Field(15000, description="Per-action timeout in milliseconds")
    describe: bool = Field(False, description="screenshot only: also describe the image with a vision model (qwen-vl)")
    describe_prompt: str | None = Field(
        None, description="screenshot+describe only: what to ask the vision model (default: describe the page)"
    )


class BrowserPlanInput(BaseModel):
    actions: list[BrowserAction] = Field(..., description="Ordered actions run in ONE browser session.")
    profile: str | None = Field(
        None,
        description="Named login profile to load+save (persists cookies across runs). Omit for a clean isolated session.",
    )
    headed: bool = Field(False, description="Show a visible browser window (debug). Default headless.")


class PlaywrightBrowserTool(BaseTool):
    """Drive a real headless browser through an ordered action plan (plan-then-execute)."""

    name: str = "Browser"
    description: str = (
        "Use a real headless browser to test or operate a web app: navigate, read JS-rendered content, "
        "click elements, fill form fields, wait, and screenshot (optionally describe the image with a "
        "vision model). Provide the FULL ordered list of actions in one call (plan-then-execute) — they "
        "run in a single browser session. Returns a per-action ✓/✗ report. For login flows that should "
        "persist across calls, pass a `profile` name."
    )
    args_schema: type[BaseModel] = BrowserPlanInput

    # Config — overridable when a crew constructs the tool.
    storage_dir: str = "logs/.browser"
    allowed_domains: tuple[str, ...] = ()  # empty = allow all; else only these hosts may be navigated

    # --- helpers ---
    def _domain_ok(self, url: str) -> bool:
        if not self.allowed_domains:
            return True
        host = (urlparse(url).hostname or "").lower()
        return any(host == d or host.endswith("." + d) for d in self.allowed_domains)

    def _storage_path(self, profile: str | None) -> str | None:
        if not profile:
            return None
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in profile)
        d = Path(self.storage_dir)
        d.mkdir(parents=True, exist_ok=True)
        return str(d / f"{safe}.json")

    def _do(self, page, a: BrowserAction) -> str:
        from playwright.sync_api import Error as PWError
        from playwright.sync_api import TimeoutError as PWTimeout

        attempts = 2 if a.action in _RETRYABLE else 1
        last = ""
        for _ in range(attempts):
            try:
                if a.action == "navigate":
                    if not a.url:
                        return "✗ navigate: url missing"
                    if not self._domain_ok(a.url):
                        return f"✗ navigate refused: {a.url} not in allowed domains"
                    page.goto(a.url, timeout=a.timeout_ms, wait_until="domcontentloaded")
                    return f"✓ navigated: {a.url}"
                if a.action == "get_content":
                    if a.url:
                        if not self._domain_ok(a.url):
                            return f"✗ get_content refused: {a.url} not in allowed domains"
                        page.goto(a.url, timeout=a.timeout_ms, wait_until="domcontentloaded")
                    return "✓ content:\n" + page.inner_text("body")[:7000]
                if a.action == "click":
                    if not a.selector:
                        return "✗ click: selector missing"
                    page.click(a.selector, timeout=a.timeout_ms)
                    return f"✓ clicked: {a.selector}"
                if a.action == "fill":
                    if not a.selector:
                        return "✗ fill: selector missing"
                    page.fill(a.selector, a.value or "", timeout=a.timeout_ms)
                    return f"✓ filled: {a.selector}"
                if a.action == "wait":
                    page.wait_for_timeout(a.timeout_ms)
                    return "✓ waited"
                if a.action == "screenshot":
                    shot = Path(self.storage_dir) / "shots"
                    shot.mkdir(parents=True, exist_ok=True)
                    path = str(shot / f"shot-{abs(hash((a.url, a.selector))) % 10**8}.png")
                    page.screenshot(path=path, full_page=True)
                    out = f"✓ screenshot: {path}"
                    if a.describe:
                        prompt = a.describe_prompt or (
                            "Describe what is visible on this web page: layout, key text, buttons, form "
                            "fields, and any error or success messages. Be concise and concrete."
                        )
                        out += "\nVision (qwen-vl):\n" + _describe_image(path, prompt)
                    return out
                return f"✗ unknown action: {a.action}"
            except PWTimeout:
                last = f"✗ timeout: {a.action} (selector={a.selector}, url={a.url})"
            except PWError as exc:
                last = f"✗ playwright error: {a.action}: {exc} (selector={a.selector})"
            except Exception as exc:  # noqa: BLE001
                last = f"✗ error: {a.action}: {exc}"
        return last

    def _run(self, actions: list, profile: str | None = None, headed: bool = False) -> str:
        from playwright.sync_api import sync_playwright

        acts = [a if isinstance(a, BrowserAction) else BrowserAction(**a) for a in actions]
        storage = self._storage_path(profile)
        results: list[str] = []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=not headed)
                ctx = browser.new_context(storage_state=storage if storage and os.path.exists(storage) else None)
                page = ctx.new_page()
                try:
                    for a in acts:
                        r = self._do(page, a)
                        results.append(r)
                        # Stop on a stateful-action failure — never blind-retry the whole sequence.
                        if r.startswith("✗") and a.action not in _RETRYABLE:
                            results.append("→ stopped after a stateful-action failure; adjust the plan and retry.")
                            break
                    if storage:
                        ctx.storage_state(path=storage)
                finally:
                    ctx.close()
                    browser.close()
        except Exception as exc:  # noqa: BLE001 — never crash the agent
            results.append(f"✗ browser session error: {exc}")
        return "\n".join(results) if results else "(no actions)"

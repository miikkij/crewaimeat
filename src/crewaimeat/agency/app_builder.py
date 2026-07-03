"""app_builder — deterministically build an AIMEAT app that SHOWS a running agent's data.

The non-LLM half of the "Build an app to show this data" feature. A brain publishes plain-prose
deliverables to memory keys under a known prefix (`brain_templates.publish_key_base`, e.g.
`watch.<agent>.YYYY-MM-DD`). This module fills a pre-baked HTML template (`app_templates/`) with that
prefix + a few literals and publishes it inline under the owner via `author_tool.publish_app_html` — no
LLM, no cortex. The app's visibility MIRRORS the brain's data visibility: `owner` -> a login-gated
dashboard (only the owner sees it, reads its own namespace with `AIMEAT.data.list`); `public` -> a public
viewer anyone can open (reads via `getPublic`).

Verification degrades gracefully (the appliance stores no aimeat.io password, and a non-dev machine may
lack Playwright): public apps verify with `app_renders_anon`; owner apps get an HTTP smoke check and are
marked "unverified" (the owner logs in as themselves to view). A hard render failure never advertises a
broken URL.

    from crewaimeat.agency import app_builder
    app_builder.data_status("news-watcher")               # is there data to show yet?
    app_builder.build_data_app("news-watcher", on_step=print)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from crewaimeat import author_tool, brain_templates, brains
from crewaimeat.agency import apps, events

_TEMPLATES = Path(__file__).parent / "app_templates"


def data_status(agent: str) -> dict:
    """What data (if any) the agent has published under its key prefix — the pre-build readiness check and
    the source of the keys/publisher a public app needs. Returns
    {ready, count, prefix, keys, publisher_gaii, visibility}. Never raises."""
    brain = brains.get_brain(agent)
    if not brain:
        return {"ready": False, "count": 0, "prefix": None, "keys": [], "publisher_gaii": None, "visibility": "owner"}
    base = brain_templates.publish_key_base(agent, brain)
    visibility = (brain.get("policy") or {}).get("visibility") or "owner"
    keys: list[str] = []
    publisher = None
    try:
        from crewaimeat.aimeat_crew import _aimeat_call

        r = _aimeat_call(agent, "aimeat_memory_list", {})
        items = (r.get("items") if isinstance(r, dict) else None) or []
        for it in items:
            k = it.get("key") or ""
            if k == base or k.startswith(base + "."):
                keys.append(k)
                if publisher is None:
                    publisher = it.get("owner_gaii") or it.get("gaii")
    except Exception:  # noqa: BLE001 — no daemon / node unreachable -> just "no data yet"
        pass
    keys.sort(reverse=True)
    return {
        "ready": bool(keys),
        "count": len(keys),
        "prefix": base,
        "keys": keys,
        "publisher_gaii": publisher,
        "visibility": visibility,
    }


def render_template(
    variant: str,
    *,
    agent: str,
    prefix: str,
    title: str,
    lang: str,
    key_mode: str,
    publisher: str | None = None,
    keys: list | None = None,
) -> str:
    """Load a pre-baked template and inject the placeholders as JS literals (json.dumps — no quoting bugs).
    `variant` is 'dashboard' (owner) or 'public_viewer' (public)."""
    path = _TEMPLATES / f"{variant}.html"
    html = path.read_text(encoding="utf-8")
    repl = {
        "__AGENT_JSON__": json.dumps(agent),
        "__PREFIX_JSON__": json.dumps(prefix),
        "__TITLE_JSON__": json.dumps(title),
        "__LANG_JSON__": json.dumps(lang if lang in ("en", "fi") else "en"),
        "__KEYMODE_JSON__": json.dumps(key_mode or "date"),
        "__PUBLISHER_JSON__": json.dumps(publisher or ""),
        "__KEYS_JSON__": json.dumps(list(keys or [])),
    }
    for token, value in repl.items():
        html = html.replace(token, value)
    return html


def _smoke_ok(agent: str, owner: str | None, url: str) -> bool:
    """HTTP smoke: the published app is served (200) and is actually our template (loads aimeat-auth.js).
    Uses the agent's token so an owner-served app isn't a false 401. Never raises."""
    try:
        import requests

        from crewaimeat.generator_tool import _token

        tok, _u = _token(agent, owner)
        headers = {"Authorization": f"Bearer {tok}"} if tok else {}
        r = requests.get(url, headers=headers, timeout=30)
        return r.status_code == 200 and "aimeat-auth.js" in (r.text or "")
    except Exception:  # noqa: BLE001
        return False


def _verify(variant: str, url: str, title: str, agent: str, owner: str | None):
    """Verify the built app. Returns True (rendered OK), False (hard render failure), or None (published +
    served but not render-verified — the owner opens it to check). Degrades: public -> app_renders_anon;
    owner -> authed render only if creds are set, else HTTP smoke -> None."""
    from crewaimeat import app_verify

    if variant == "public_viewer":
        try:
            return app_verify.app_renders_anon(url, expect_any=[title]).get("ok")
        except Exception:  # noqa: BLE001
            return None
    user, pw = os.getenv("AIMEAT_APP_LOGIN_USER"), os.getenv("AIMEAT_APP_LOGIN_PASSWORD")
    if user and pw:
        try:
            return app_verify.app_renders_authed(url, user, pw, expect_any=[title]).get("ok")
        except Exception:  # noqa: BLE001
            return None
    return None if _smoke_ok(agent, owner, url) else False


def build_data_app(agent: str, owner: str | None = None, *, lang: str = "en", on_step=None) -> dict:
    """Build (or rebuild in place) the data app for `agent`. Returns a status dict:
    {status: 'no_brain'|'no_data'|'failed'|'live', ...}. Records an event + persists the app pointer on a
    successful publish. `on_step(msg)` gets coarse progress. Never raises."""
    step = on_step or (lambda _s: None)
    try:
        brain = brains.get_brain(agent)
        if not brain:
            return {"status": "no_brain"}
        policy = brain.get("policy") or {}
        visibility = (policy.get("visibility") or "owner").strip().lower()
        key_mode = (policy.get("key_mode") or "date").strip().lower()
        title = (brain.get("title") or agent).strip() or agent

        step("checking data")
        ds = data_status(agent)
        if not ds["ready"]:
            return {"status": "no_data", "prefix": ds["prefix"]}

        variant = "public_viewer" if visibility == "public" else "dashboard"
        step("rendering")
        html = render_template(
            variant,
            agent=agent,
            prefix=ds["prefix"],
            title=title,
            lang=lang,
            key_mode=key_mode,
            publisher=ds["publisher_gaii"],
            keys=ds["keys"],
        )
        filename = f"{brains.slug_agent_name(agent)}-dashboard.html"
        description = f'Live data from your "{agent}" agent.'

        step("publishing")
        ok, url = author_tool.publish_app_html(
            agent, owner, filename, html, name=title, description=description, category="utility"
        )
        if not ok:
            events.record(agent, "app_build_failed", {"error": url})
            return {"status": "failed", "error": url}

        step("verifying")
        verified = _verify(variant, url, title, agent, owner)
        status = "failed" if verified is False else "live"
        apps.set_app(
            agent, filename=filename, url=url, variant=variant, visibility=visibility, status=status, verified=verified
        )
        events.record(agent, "app_built", {"url": url, "variant": variant, "verified": verified})
        return {
            "status": status,
            "url": url,
            "filename": filename,
            "variant": variant,
            "visibility": visibility,
            "verified": verified,
            "count": ds["count"],
        }
    except Exception as e:  # noqa: BLE001 — a build must not crash the cockpit thread
        events.record(agent, "app_build_failed", {"error": repr(e)})
        return {"status": "failed", "error": repr(e)}

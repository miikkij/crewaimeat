"""The health view: is everything this machine needs in order, and if not, what fixes it.

Every row is {id, level: ok|warn|error, title, detail, fix?}. `fix` names an action the UI offers as a
button (`key`, `serve`, `start:<agent>`, `reconnect:<agent>`, `open:<url>`). A check that could not be
made is a row saying so — never a silently missing row, and never a green one. Rows are written in the
person's language (fi/en); raw node errors are appended as they came, so nothing is paraphrased away.
"""

from __future__ import annotations

from typing import Any

import requests

from crewaimeat.agency2 import connect, engine, node, paths, procs, store

OPENROUTER_KEY_URL = "https://openrouter.ai/api/v1/key"

_M = {
    "fi": {
        "engine": "Agenttimoottori",
        "engine_ok": "liitin {v} — sovelluksen mukana",
        "engine_dev": "liitin {v} — tämän koneen oma (kehitysajo)",
        "engine_missing": "puuttuu: node={node} liitin={conn}",
        "or": "OpenRouter-avain",
        "or_used": "käytetty ${u:.2f}",
        "or_left": ", jäljellä ${r:.2f}",
        "no_key": "avainta ei ole tallennettu",
        "or_refused": "OpenRouter hylkäsi avaimen (peruttu tai kirjoitusvirhe)",
        "or_down": "OpenRouter ei vastannut: {e}",
        "or_http": "OpenRouter vastasi HTTP {s}",
        "serve": "Yhteyspalvelu",
        "serve_down": "ei käynnissä — agentit eivät tavoita palvelimiaan",
        "inst_down": "ei vastaa: {d}",
        "inst_direct": "tavoitettavissa, mutta ilman suoraa yhteyttä: agentit huomaavat uuden työn noin 30 sekunnin "
        "viiveellä (palvelimella AIMEAT_CONNECT_TUNNEL_ENABLED on pois päältä)",
        "inst_ok": "node {n}",
        "waiting": "odottaa hyväksyntää — koodi {c}",
        "not_connected": "ei vielä liitetty palvelimeensa",
        "not_served": "yhteyspalvelu ei kanna tätä agenttia",
        "refused": "palvelin hylkäsi agentin tunnuksen: {e}",
        "scope": "hyväksytty ilman oikeutta {s} — liitä uudelleen ja valitse se hyväksyntäsivulla",
        "mode": "tila on '{m}': jokainen tehtävä odottaa, kunnes painat palvelimella Aloita",
        "stopped": "ei käynnissä tällä koneella",
        "no_def": "ei käynnissä: agentilla ei ole vielä määritelmää — kuvaile se agentin sivulla",
        "ok": "käynnissä, yhteys: {t}",
    },
    "en": {
        "engine": "Agent engine",
        "engine_ok": "connector {v} — bundled with the app",
        "engine_dev": "connector {v} — this machine's own (dev run)",
        "engine_missing": "missing: node={node} connector={conn}",
        "or": "OpenRouter key",
        "or_used": "used ${u:.2f}",
        "or_left": ", ${r:.2f} left",
        "no_key": "no key saved",
        "or_refused": "OpenRouter refused the key (revoked or mistyped)",
        "or_down": "OpenRouter did not answer: {e}",
        "or_http": "OpenRouter answered HTTP {s}",
        "serve": "Connection service",
        "serve_down": "not running — agents cannot reach their instances",
        "inst_down": "does not answer: {d}",
        "inst_direct": "reachable, but without the live connection: agents notice new work within ~30 s instead of "
        "at once (the instance has AIMEAT_CONNECT_TUNNEL_ENABLED off)",
        "inst_ok": "node {n}",
        "waiting": "waiting for approval — code {c}",
        "not_connected": "not connected to its instance yet",
        "not_served": "the connection service does not carry this agent",
        "refused": "the instance refused this agent's key: {e}",
        "scope": "approved without {s} — reconnect and tick it on the approval page",
        "mode": "mode is '{m}': each task waits until you press Start on the instance",
        "stopped": "not running on this machine",
        "no_def": "not running: the agent has no definition yet — describe it on its page",
        "ok": "running, {t} connection",
    },
}


def _row(id_: str, level: str, title: str, detail: str = "", fix: str | None = None) -> dict:
    return {"id": id_, "level": level, "title": title, "detail": detail, "fix": fix}


def openrouter(key: str | None = None, *, timeout: float = 10, lang: str = "en") -> dict:
    """{ok, detail, usage, limit_remaining} for the saved (or given) key — OpenRouter's own answer."""
    m = _M.get(lang, _M["en"])
    key = key if key is not None else paths.get_env_key()
    if not key:
        return {"ok": False, "detail": m["no_key"]}
    try:
        r = requests.get(OPENROUTER_KEY_URL, headers={"Authorization": f"Bearer {key}"}, timeout=timeout)
    except requests.RequestException as exc:
        return {"ok": False, "detail": m["or_down"].format(e=type(exc).__name__)}
    if r.status_code in (401, 403):
        return {"ok": False, "detail": m["or_refused"]}
    if r.status_code != 200:
        return {"ok": False, "detail": m["or_http"].format(s=r.status_code)}
    d: dict[str, Any] = (r.json() or {}).get("data") or {}
    return {
        "ok": True,
        "detail": d.get("label") or "",
        "usage": d.get("usage"),
        "limit": d.get("limit"),
        "limit_remaining": d.get("limit_remaining"),
    }


def check(
    lang: str = "fi",
    *,
    openrouter_fn=None,
    health_fn=node.health,
    roster_fn=node.roster,
    running_fn=procs.running,
    log_fn=lambda name: procs.tail(name, 6000),
) -> list[dict]:
    m = _M.get(lang, _M["en"])
    rows: list[dict] = []

    eng = engine.status()
    v = eng["connector_version"] or "?"
    if eng["ok"]:
        rows.append(
            _row(
                "engine",
                "ok" if eng["bundled"] else "warn",
                m["engine"],
                m["engine_ok" if eng["bundled"] else "engine_dev"].format(v=v),
            )
        )
    else:
        rows.append(
            _row("engine", "error", m["engine"], m["engine_missing"].format(node=eng["node"], conn=eng["connector"]))
        )

    o = (openrouter_fn or (lambda: openrouter(lang=lang)))()
    if o["ok"]:
        rem = o.get("limit_remaining")
        detail = m["or_used"].format(u=o.get("usage") or 0)
        if isinstance(rem, (int, float)):
            detail += m["or_left"].format(r=rem)
        low = isinstance(rem, (int, float)) and rem < 1
        rows.append(
            _row(
                "openrouter",
                "warn" if low else "ok",
                m["or"],
                detail,
                "open:https://openrouter.ai/credits" if low else None,
            )
        )
    else:
        rows.append(_row("openrouter", "error", m["or"], o["detail"], "key"))

    served = node.served_agents()
    if store.agents() and node.serve_port() is None:
        rows.append(_row("serve", "error", m["serve"], m["serve_down"], "serve"))

    for inst in store.instances():
        h = health_fn(inst["url"])
        mine = [a for a in store.agents() if a["instance"] == inst["url"]]
        transports = {served.get(a["name"], {}).get("transport") for a in mine} - {None}
        rid = f"instance:{inst['url']}"
        if not h["ok"]:
            rows.append(_row(rid, "error", inst["label"], m["inst_down"].format(d=h["detail"]), f"open:{inst['url']}"))
        elif "direct" in transports:
            rows.append(_row(rid, "warn", inst["label"], m["inst_direct"]))
        else:
            rows.append(_row(rid, "ok", inst["label"], m["inst_ok"].format(n=h["node_id"])))

    for a in store.agents():
        name = a["name"]
        rid = f"agent:{name}"
        reg = connect.state(name)
        if not a.get("connected"):
            if reg and reg.get("status") == "waiting":
                rows.append(
                    _row(rid, "warn", name, m["waiting"].format(c=reg.get("code")), f"open:{reg.get('verify_url')}")
                )
            else:
                rows.append(_row(rid, "error", name, m["not_connected"], f"reconnect:{name}"))
            continue
        if name not in served:
            rows.append(_row(rid, "error", name, m["not_served"], "serve"))
            continue
        try:
            me = next((r for r in roster_fn(name) if r.get("name") == name), None)
        except node.Refused as exc:
            fix = f"reconnect:{name}" if exc.status in (401, 403) else None
            rows.append(_row(rid, "error", name, m["refused"].format(e=exc), fix))
            continue
        except node.NoDaemon as exc:
            rows.append(_row(rid, "error", name, str(exc), "serve"))
            continue
        problems: list[str] = []
        fix = None
        scopes = set((me or {}).get("default_scopes") or [])
        missing = [s for s in connect.REQUIRED_SCOPES if s not in scopes and "*" not in scopes]
        if missing:
            problems.append(m["scope"].format(s=", ".join(missing)))
            fix = f"reconnect:{name}"
        if me and me.get("mode") != "task-runner":
            problems.append(m["mode"].format(m=me.get("mode")))
        if not running_fn(name):
            if "holds no crew definition" in log_fn(name):
                problems.append(m["no_def"])
                fix = fix or f"agent:{name}"
            else:
                problems.append(m["stopped"])
                fix = fix or f"start:{name}"
        if problems:
            level = "error" if fix == f"reconnect:{name}" else "warn"
            rows.append(_row(rid, level, name, "; ".join(problems), fix))
        else:
            rows.append(_row(rid, "ok", name, m["ok"].format(t=served[name].get("transport"))))
    return rows

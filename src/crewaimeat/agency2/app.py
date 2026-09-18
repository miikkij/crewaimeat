"""agency 2.0's local server (127.0.0.1 only), behind the same per-launch token as 0.8.x.

The Tauri shell mints the token, passes it as AIMEAT_AGENCY_TOKEN and opens `/?boot=<token>`; every
`/api/*` call carries it as a bearer. Standalone: `python -m crewaimeat.agency2` prints the boot URL.
"""

from __future__ import annotations

import os
import secrets
import threading
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from crewaimeat.agency2 import author, connect, engine, health, migrate, node, paths, procs, schedule, store, trial

TOKEN_ENV = "AIMEAT_AGENCY_TOKEN"
VERSION = "2.0.0"
_STATIC = Path(__file__).parent / "static"


# ── bodies ──────────────────────────────────────────────────────────────────


class KeyIn(BaseModel):
    key: str


class InstanceIn(BaseModel):
    url: str
    owner: str


class AuthorIn(BaseModel):
    name: str
    description: str
    lang: str = "fi"
    edit: bool = False  # True = change the agent's CURRENT definition (read from its instance)


class TrialIn(BaseModel):
    doc: dict
    prompt: str
    as_agent: str | None = None


class CreateIn(BaseModel):
    name: str
    instance: str
    description: str = ""
    doc: dict


class RunHereIn(BaseModel):
    name: str
    instance: str


class PublishIn(BaseModel):
    doc: dict


class UrlIn(BaseModel):
    url: str


class ScheduleIn(BaseModel):
    preset: str  # daily | weekdays | weekly | hourly
    time: str = "07:00"
    weekday: int = 1  # ISO: 1 = Monday … 7 = Sunday
    what: str
    timezone: str = "Europe/Helsinki"


class EnabledIn(BaseModel):
    enabled: bool


# ── helpers ─────────────────────────────────────────────────────────────────


def _bad(detail: str, code: int = 400) -> HTTPException:
    return HTTPException(status_code=code, detail=detail)


def _stage(name: str, doc: dict) -> Path:
    """Leave the FIRST definition where the runtime publishes it from on its first start."""
    from crewaimeat.forge_json import _doc_base

    d = paths.data_dir() / "crew_defs"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{_doc_base(name)}.json"
    paths.write_json(p, dict(doc, agent_name=name))
    return p


def _after_approval(name: str, st: dict | None) -> None:
    """Approved → the daemon must reload its agent set, and every runtime reattach to the new daemon."""
    if not st or st.get("status") != "approved":
        return
    store.update_agent(name, connected=True)
    procs.restart_serve()
    for a in store.agents():
        if a.get("connected") and a.get("autostart", True):
            procs.stop(a["name"])
            procs.start(a["name"])


def _autostart() -> None:
    if not paths.get_env_key():
        print("[agency2] autostart waits for the OpenRouter key (a runtime cannot start without a model)", flush=True)
        return
    try:
        if any(a.get("connected") for a in store.agents()):
            procs.ensure_serve()
            for a in store.agents():
                if a.get("connected") and a.get("autostart", True):
                    procs.start(a["name"])
    except Exception as exc:  # noqa: BLE001 — shown in the health view (serve / not running rows)
        print(f"[agency2] autostart: {type(exc).__name__}: {exc}", flush=True)


def _agent_view(a: dict, served: dict) -> dict:
    st = connect.state(a["name"])
    return {
        **a,
        "running": bool(procs.running(a["name"])),
        "served": a["name"] in served,
        "transport": served.get(a["name"], {}).get("transport"),
        "registration": st,
    }


def create_app(token: str | None = None) -> FastAPI:
    app = FastAPI(title="aimeat-agency 2.0", version=VERSION)
    app.state.token = token or os.environ.get(TOKEN_ENV) or secrets.token_urlsafe(32)

    def _check(authorization: str | None = Header(default=None)) -> None:
        got = authorization[7:].strip() if authorization and authorization.lower().startswith("bearer ") else ""
        if not (got and secrets.compare_digest(got, app.state.token)):
            raise HTTPException(status_code=401, detail="missing or invalid agency token")

    auth = [Depends(_check)]

    @app.get("/healthz")
    def healthz() -> dict:  # open: liveness only, no secrets — the shell polls it for readiness
        return {"ok": True, "version": VERSION}

    @app.get("/", response_class=HTMLResponse)
    def index(boot: str | None = Query(default=None)) -> str:
        if not (boot and secrets.compare_digest(boot.strip(), app.state.token)):
            raise _bad("open agency through the app window (or the ?boot= address printed at start)", 401)
        html = (_STATIC / "index.html").read_text(encoding="utf-8")
        return html.replace("__AGENCY_TOKEN__", app.state.token).replace("__AGENCY_VERSION__", VERSION)

    @app.get("/api/state", dependencies=auth)
    def state() -> dict:
        served = node.served_agents()
        return {
            "version": VERSION,
            "has_key": bool(paths.get_env_key()),
            "engine": engine.status(),
            "instances": store.instances(),
            "agents": [_agent_view(a, served) for a in store.agents()],
            "migration": migrate.status(),
            "required_scopes": list(connect.REQUIRED_SCOPES),
        }

    # ── setup ──

    @app.post("/api/key", dependencies=auth)
    def set_key(body: KeyIn) -> dict:
        key = body.key.strip()
        chk = health.openrouter(key)
        if not chk["ok"]:
            raise _bad(chk["detail"])
        paths.set_env_key(key)
        # A runtime cannot start without a model (the README expansion calls it at start), so agents
        # that autostarted before the key existed are down now. Bring them up with the key.
        threading.Thread(target=_autostart, name="start-after-key", daemon=True).start()
        return chk

    @app.post("/api/instances", dependencies=auth)
    def add_instance(body: InstanceIn) -> dict:
        try:
            url = store.normalize_url(body.url)
        except store.StoreError as exc:
            raise _bad(str(exc)) from exc
        h = node.health(url)
        if not h["ok"]:
            raise _bad(f"{url} does not answer as an AIMEAT instance: {h['detail']}")
        try:
            return store.add_instance(url, body.owner, node_id=h["node_id"])
        except store.StoreError as exc:
            raise _bad(str(exc)) from exc

    @app.delete("/api/instances", dependencies=auth)
    def remove_instance(url: str) -> dict:
        try:
            store.remove_instance(url)
        except store.StoreError as exc:
            raise _bad(str(exc)) from exc
        return {"ok": True}

    # ── agents: the roster of every instance, marked with where each one runs ──

    @app.get("/api/agents", dependencies=auth)
    def agents() -> dict:
        served = node.served_agents()
        local = {a["name"]: a for a in store.agents()}
        out = []
        for inst in store.instances():
            asker = next(
                (
                    a
                    for a in local.values()
                    if a["instance"] == inst["url"] and a.get("connected") and a["name"] in served
                ),
                None,
            )
            roster, note = [], None
            if asker:
                try:
                    roster = node.roster(asker["name"])
                except (node.Refused, node.NoDaemon) as exc:
                    note = str(exc)
            else:
                note = "no connected agent on this machine yet — the instance's list appears after the first one"
            rows = []
            seen = set()
            for r in roster:
                nm = r.get("name")
                seen.add(nm)
                lo = local.get(nm) if local.get(nm, {}).get("instance") == inst["url"] else None
                channel = ((r.get("health") or {}).get("delivery") or {}).get("channel")
                rows.append(
                    {
                        "name": nm,
                        "here": bool(lo),
                        "running_here": bool(lo and procs.running(nm)),
                        "connected_elsewhere": (not lo) and channel == "socket",
                        "mode": r.get("mode"),
                        "last_seen": r.get("last_seen"),
                    }
                )
            for nm, lo in local.items():
                if lo["instance"] == inst["url"] and nm not in seen:
                    rows.append(
                        {
                            "name": nm,
                            "here": True,
                            "running_here": bool(procs.running(nm)),
                            "connected_elsewhere": False,
                            "mode": None,
                            "last_seen": None,
                            "pending": not lo.get("connected"),
                        }
                    )
            out.append(
                {"instance": inst, "agents": sorted(rows, key=lambda x: (not x["here"], x["name"])), "note": note}
            )
        return {"instances": out}

    @app.get("/api/agents/{name}", dependencies=auth)
    def agent_detail(name: str, lang: str = "fi") -> dict:
        a = store.agent(name)
        if not a:
            raise _bad(f"no agent '{name}' on this machine", 404)
        view = _agent_view(a, node.served_agents())
        view["log"] = procs.tail(name, 12_000)
        if a.get("connected"):
            try:
                view["tasks"] = node.tasks(name)
                for t in view["tasks"][:3]:
                    if t.get("deliverableKey"):
                        t["output"] = node.deliverable_text(name, t["deliverableKey"])
            except (node.Refused, node.NoDaemon) as exc:
                view["tasks_error"] = str(exc)
            try:
                view["runtime"] = node.runtime_report(name)
            except (node.Refused, node.NoDaemon) as exc:
                view["runtime_error"] = str(exc)
            try:
                from crewaimeat.json_agent import load_def

                doc, rev = load_def(name)
                view["definition"] = {"revision": rev, "summary": author.summary(doc, lang), "doc": doc}
            except Exception as exc:  # noqa: BLE001 — shown in place of the definition
                view["definition_error"] = f"{type(exc).__name__}: {exc}"
        return view

    @app.post("/api/author", dependencies=auth)
    def author_route(body: AuthorIn) -> dict:
        try:
            name = store.check_agent_name(body.name)
        except store.StoreError as exc:
            raise _bad(str(exc)) from exc
        if not paths.get_env_key():
            raise _bad("save the OpenRouter key first")
        current = None
        if body.edit:
            from crewaimeat.json_agent import load_def

            try:
                current, _ = load_def(name)
            except Exception as exc:  # noqa: BLE001
                raise _bad(f"could not read the current definition: {exc}") from exc
        res = author.author(name, body.description, lang=body.lang, current=current)
        res["summary"] = author.summary(res["doc"], body.lang) if res.get("doc") else None
        return res

    @app.post("/api/trials", dependencies=auth)
    def trial_start(body: TrialIn) -> dict:
        if not body.prompt.strip():
            raise _bad("write what the agent should do in this trial")
        return {"id": trial.start(body.doc, body.prompt, as_agent=body.as_agent)}

    @app.get("/api/trials/{tid}", dependencies=auth)
    def trial_get(tid: str) -> dict:
        j = trial.get(tid)
        if j is None:
            raise _bad("no such trial", 404)
        return j

    @app.post("/api/agents", dependencies=auth)
    def create(body: CreateIn) -> dict:
        from crewaimeat.crew_def import validate_crew_doc

        try:
            row = store.add_agent(body.name, body.instance, description=body.description)
        except store.StoreError as exc:
            raise _bad(str(exc)) from exc
        doc = dict(body.doc, agent_name=row["name"])
        errs = validate_crew_doc(doc)
        if errs:
            store.remove_agent(row["name"])
            raise _bad("the definition is not valid: " + "; ".join(errs))
        _stage(row["name"], doc)
        return connect.start(row["name"], row["instance"], row["owner"], on_done=_after_approval)

    @app.post("/api/agents/run-here", dependencies=auth)
    def run_here(body: RunHereIn) -> dict:
        try:
            row = store.add_agent(body.name, body.instance)
        except store.StoreError as exc:
            raise _bad(str(exc)) from exc
        return connect.start(row["name"], row["instance"], row["owner"], on_done=_after_approval)

    @app.post("/api/agents/{name}/reconnect", dependencies=auth)
    def reconnect(name: str) -> dict:
        a = store.agent(name)
        if not a:
            raise _bad(f"no agent '{name}' on this machine", 404)
        store.update_agent(name, connected=False)
        return connect.start(name, a["instance"], a["owner"], on_done=_after_approval, fresh=True)

    @app.post("/api/agents/{name}/start", dependencies=auth)
    def start(name: str) -> dict:
        a = store.agent(name)
        if not a or not a.get("connected"):
            raise _bad("connect the agent first")
        procs.ensure_serve()
        store.update_agent(name, autostart=True)
        return procs.start(name)

    @app.post("/api/agents/{name}/stop", dependencies=auth)
    def stop(name: str) -> dict:
        if not store.agent(name):
            raise _bad(f"no agent '{name}' on this machine", 404)
        store.update_agent(name, autostart=False)
        return {"stopped": procs.stop(name)}

    @app.post("/api/agents/{name}/define", dependencies=auth)
    def define(name: str, body: PublishIn) -> dict:
        """The FIRST definition for an agent that is connected here but has none (e.g. one made on the
        node). It is staged and the runtime restarted: an agent publishes its own first definition,
        and only into an EMPTY key (`json_agent.seed_from_staged`), so an existing one is never
        overwritten from here."""
        from crewaimeat.crew_def import validate_crew_doc

        a = store.agent(name)
        if not a or not a.get("connected"):
            raise _bad("connect the agent first")
        doc = dict(body.doc, agent_name=name)
        errs = validate_crew_doc(doc)
        if errs:
            raise _bad("the definition is not valid: " + "; ".join(errs))
        _stage(name, doc)
        procs.stop(name)
        store.update_agent(name, autostart=True)
        return procs.start(name)

    @app.post("/api/agents/{name}/publish", dependencies=auth)
    def publish(name: str, body: PublishIn) -> dict:
        """A change to a RUNNING agent goes through the node's publish route: the agent's own runtime
        validates it, the node numbers the revision and wakes the runtime (crew_registry docstring)."""
        from crewaimeat.crew_registry import publish_crew_def_live

        a = store.agent(name)
        if not a or not a.get("connected"):
            raise _bad("connect the agent first")
        if not procs.running(name):
            raise _bad("start the agent first — its own runtime checks a change before the instance takes it")
        ok, key, detail = publish_crew_def_live(dict(body.doc, agent_name=name), agent=name)
        if not ok:
            raise _bad(detail)
        return {"ok": True, "key": key, "detail": detail}

    @app.delete("/api/agents/{name}", dependencies=auth)
    def remove(name: str) -> dict:
        """Stops it HERE and forgets it on this machine. The agent and its definition stay on the instance."""
        if not store.agent(name):
            raise _bad(f"no agent '{name}' on this machine", 404)
        procs.stop(name)
        store.remove_agent(name)
        return {"ok": True}

    # ── schedules: the node's clock, the agent's own agent_task records ──

    def _connected(name: str) -> dict:
        a = store.agent(name)
        if not a or not a.get("connected"):
            raise _bad("connect the agent first")
        return a

    def _node_error(exc: Exception) -> HTTPException:
        if isinstance(exc, node.Refused) and exc.code in ("SCOPE_DENIED", "INSUFFICIENT_SCOPE", "ACCESS_DENIED"):
            need = ", ".join(connect.REQUIRED_SCOPES)
            return _bad(f"the agent was approved without the permissions schedules need ({need}) — reconnect it: {exc}")
        return _bad(str(exc), 502)

    @app.get("/api/agents/{name}/schedules", dependencies=auth)
    def schedules(name: str, lang: str = "fi") -> dict:
        _connected(name)
        try:
            return {"schedules": schedule.list_for(name, lang)}
        except (node.Refused, node.NoDaemon) as exc:
            raise _node_error(exc) from exc

    @app.post("/api/agents/{name}/schedules", dependencies=auth)
    def schedule_create(name: str, body: ScheduleIn) -> dict:
        _connected(name)
        try:
            return {
                "ok": True,
                "schedule": schedule.create(
                    name,
                    preset=body.preset,
                    time=body.time,
                    weekday=body.weekday,
                    what=body.what,
                    timezone=body.timezone,
                ),
            }
        except schedule.ScheduleError as exc:
            raise _bad(str(exc)) from exc
        except (node.Refused, node.NoDaemon) as exc:
            raise _node_error(exc) from exc

    @app.patch("/api/agents/{name}/schedules/{sid}", dependencies=auth)
    def schedule_enable(name: str, sid: str, body: EnabledIn) -> dict:
        _connected(name)
        try:
            return {"ok": True, "result": schedule.set_enabled(name, sid, body.enabled)}
        except (node.Refused, node.NoDaemon) as exc:
            raise _node_error(exc) from exc

    @app.delete("/api/agents/{name}/schedules/{sid}", dependencies=auth)
    def schedule_delete(name: str, sid: str) -> dict:
        _connected(name)
        try:
            return {"ok": True, "result": schedule.delete(name, sid)}
        except (node.Refused, node.NoDaemon) as exc:
            raise _node_error(exc) from exc

    @app.get("/api/health", dependencies=auth)
    def health_route(lang: str = "fi") -> dict:
        return {"rows": health.check(lang)}

    @app.post("/api/serve", dependencies=auth)
    def serve() -> dict:
        doc = procs.restart_serve()
        for a in store.agents():
            if a.get("connected") and a.get("autostart", True):
                procs.stop(a["name"])
                procs.start(a["name"])
        return {"port": doc.get("port")}

    @app.get("/api/migration", dependencies=auth)
    def migration() -> dict:
        return migrate.status()

    @app.post("/api/migration/ack", dependencies=auth)
    def migration_ack() -> dict:
        return migrate.acknowledge()

    @app.post("/api/open", dependencies=auth)
    def open_url(body: UrlIn) -> dict:
        import webbrowser

        if not body.url.startswith(("http://", "https://")):
            raise _bad("only http(s) addresses")
        webbrowser.open(body.url)
        return {"ok": True}

    @app.post("/api/shutdown", dependencies=auth)
    def shutdown() -> dict:
        stopped = [a["name"] for a in store.agents() if procs.stop(a["name"])]
        procs.stop_serve()

        def _exit() -> None:
            import time

            time.sleep(0.5)
            os._exit(0)

        threading.Thread(target=_exit, daemon=True).start()
        return {"stopped": stopped}

    return app


def main() -> None:
    import uvicorn

    engine.apply_to_process()
    engine.openrouter_only()  # before anything imports crewai (it would load a stray .env)
    shell = bool(os.environ.get(TOKEN_ENV))
    token = os.environ.get(TOKEN_ENV) or secrets.token_urlsafe(32)
    host = "127.0.0.1"
    port = int(os.environ.get("AIMEAT_AGENCY_PORT", "8753"))
    print(f"[aimeat-agency 2.0] http://{host}:{port}" + ("" if shell else f"/?boot={token}"), flush=True)
    if not os.environ.get("PYTEST_CURRENT_TEST"):
        threading.Thread(target=_autostart, name="autostart", daemon=True).start()
    uvicorn.run(create_app(token), host=host, port=port, log_level="warning")

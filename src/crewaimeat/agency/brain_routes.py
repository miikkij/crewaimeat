"""Authenticated cockpit routes for brain management."""

from __future__ import annotations

from fastapi import HTTPException

from crewaimeat import brains
from crewaimeat.agency import apps, events
from crewaimeat.agency.api_models import BrainEdit, BrainIn, RollbackIn


def mount(app, require_token, _safe_agent, _brain_diff):
    @app.get("/api/brains", dependencies=[require_token])
    def list_brains() -> dict:
        return {"brains": brains.list_brains()}

    @app.post("/api/brains", dependencies=[require_token])
    def create_brain(body: BrainIn) -> dict:
        # The agent name becomes the connector identity, which must be 3-64 lowercase alphanumeric + hyphens
        # (the connector rejects e.g. 'Mapmaker' and device-auth then fails). Slug it at the boundary.
        name = brains.slug_agent_name(body.agent_name)
        if len(name) < 3:
            raise HTTPException(
                status_code=400, detail="agent name must be 3–64 lowercase letters, numbers, or hyphens"
            )
        try:
            prev = brains.get_brain(name)
            saved = brains.save_brain(name, body.template_id, prose=body.prose, policy=body.policy, title=body.title)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        events.record(
            saved["agent_name"], "brain_saved", {"version": saved["version"], "changed": _brain_diff(prev, saved)}
        )
        return saved

    @app.get("/api/brains/{agent}", dependencies=[require_token])
    def get_brain(agent: str) -> dict:
        b = brains.get_brain(agent)
        if b is None:
            raise HTTPException(status_code=404, detail=f"no brain '{agent}'")
        return b

    @app.patch("/api/brains/{agent}", dependencies=[require_token])
    def edit_brain(agent: str, body: BrainEdit) -> dict:
        b = brains.get_brain(agent)
        if b is None:
            raise HTTPException(status_code=404, detail=f"no brain '{agent}'")
        # keep the same template; save_brain falls back to existing prose/policy when a field is omitted
        saved = brains.save_brain(agent, b["template_id"], prose=body.prose, policy=body.policy, title=body.title)
        events.record(agent, "brain_saved", {"version": saved["version"], "changed": _brain_diff(b, saved)})
        return saved

    @app.delete("/api/brains/{agent}", dependencies=[require_token])
    def delete_brain(agent: str) -> dict:
        agent = _safe_agent(agent)  # globs + os.remove()s token files below
        # Stop the crew first so we don't orphan a running daemon, then drop the brain + its model override.
        from crewaimeat import llm
        from crewaimeat.tui import actions

        try:
            actions.stop_crew(agent)
        except Exception:  # noqa: BLE001 — not running / already stopped is fine
            pass
        deleted = brains.delete_brain(agent)
        try:
            llm.clear_override(agent)
        except Exception:  # noqa: BLE001
            pass
        # Remove the connector TOKEN(s) too — otherwise the serve daemon keeps loading a deleted agent from
        # its leftover token file (the 'news-paska is still served though I deleted it' zombie).
        try:
            from crewaimeat._home import aimeat_home

            # Same rule as the log tail: compare against the names the token store actually holds rather
            # than globbing a pattern built from the URL. What we unlink comes from the listing.
            for tokf in (aimeat_home() / "tokens").glob("*.token"):
                if tokf.name.split("@", 1)[0] != agent:
                    continue
                try:
                    tokf.unlink()
                except OSError:
                    pass
        except Exception:  # noqa: BLE001
            pass
        try:
            apps.clear_app(agent)  # forget any built data-app pointer for this agent
        except Exception:  # noqa: BLE001
            pass
        return {"deleted": deleted}

    @app.get("/api/brains/{agent}/history", dependencies=[require_token])
    def brain_history(agent: str) -> dict:
        return {"versions": brains.history(agent)}

    @app.post("/api/brains/{agent}/rollback", dependencies=[require_token])
    def rollback_brain(agent: str, body: RollbackIn) -> dict:
        try:
            restored = brains.rollback(agent, body.version)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        events.record(agent, "rolled_back", {"to_version": body.version, "version": restored["version"]})
        return restored

    @app.post("/api/brains/{agent}/instantiate", dependencies=[require_token])
    def instantiate_brain(agent: str) -> dict:
        if brains.get_brain(agent) is None:
            raise HTTPException(status_code=404, detail=f"no brain '{agent}'")
        return {"stub": brains.write_crew_stub(agent)}

    @app.get("/api/brains/{agent}/dry-run", dependencies=[require_token])
    def dry_run(agent: str) -> dict:
        """A node-independent PLAN PREVIEW: build the crew from the brain and report what it WOULD run —
        the roster (roles + tools) and each task's resolved description. (The full PROPOSE phase, with a
        real spend estimate, runs against the node once the agent is live — that is a later step.)"""
        from crewaimeat.aimeat_crew import BuildContext

        b = brains.get_brain(agent)
        if b is None:
            raise HTTPException(status_code=404, detail=f"no brain '{agent}'")
        from crewaimeat import llm

        ov = llm.agent_override(agent) or {}
        model = ov.get("model") or "(routed by llm_providers.json)"
        spec = brains.build_crewspec(agent)
        ctx = BuildContext(task={}, prompt="", llm=str(model), today="(current time injected at run)")
        agents, tasks = spec.build_domain(ctx)
        return {
            "agent": agent,
            "template_id": b["template_id"],
            "model": model,
            "agents": [{"role": a.role, "goal": a.goal, "tools": [t.name for t in a.tools]} for a in agents],
            "tasks": [{"description": t.description, "expected_output": t.expected_output} for t in tasks],
            "note": "plan preview (no LLM, no spend). Full PROPOSE runs live once the agent is started.",
        }

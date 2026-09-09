"""Authenticated cockpit routes for local and node memory."""

from __future__ import annotations

from fastapi import HTTPException, Query

from crewaimeat import local_memory
from crewaimeat.agency.api_models import PublishIn


def mount(app, require_token):
    @app.get("/api/memory/{agent}", dependencies=[require_token])
    def memory(
        agent: str,
        topic: str | None = None,
        event: str | None = None,
        source: str | None = None,
        status: str | None = None,
        tag: str | None = None,
        limit: int = 50,
    ) -> dict:
        return {
            "records": local_memory.browse(
                agent, topic=topic, event=event, source=source, status=status, tag=tag, limit=limit
            )
        }

    @app.get("/api/memory/{agent}/facets", dependencies=[require_token])
    def memory_facets(agent: str) -> dict:
        return local_memory.facets(agent)

    @app.get("/api/memory/{agent}/record/{rid}", dependencies=[require_token])
    def memory_record(agent: str, rid: str) -> dict:
        r = local_memory.recall(agent, rid)
        if r is None:
            raise HTTPException(status_code=404, detail=f"no record '{rid}'")
        return r

    @app.post("/api/memory/{agent}/publish", dependencies=[require_token])
    def memory_publish(agent: str, body: PublishIn) -> dict:
        res = local_memory.publish(agent, body.id, key=body.key, visibility=body.visibility)
        if not res.get("ok"):
            raise HTTPException(status_code=400, detail=res.get("error", "publish failed"))
        return res

    @app.get("/api/sync/{agent}", dependencies=[require_token])
    def sync_view(agent: str) -> dict:
        """The Sync view's data: local scratch vs what's ACTUALLY published on aimeat.io. Published is read
        from the NODE's own memory keys (not just the local tier) — so it includes the deliverables the
        scaffold publishes directly (crews.<agent>.…latest_output, watch.<agent>.…). Internal keys
        (.live / config / readme / offers / statistics) are filtered out so only real outputs show."""
        from crewaimeat.aimeat_crew import _aimeat_call

        raw = local_memory.browse(agent, status="raw", limit=1000)
        r = _aimeat_call(agent, "aimeat_memory_list", {})
        items = (r.get("items") if isinstance(r, dict) else None) or []
        node = []
        for it in items:
            k = it.get("key") or ""
            if not k:
                continue
            internal = (
                k.endswith(".live")
                or ".statistics" in k
                or k.startswith("agents.config")
                or k.endswith(".readme")
                or k.endswith(".offers")
                or k.endswith(".runtime")
            )
            is_output = (".latest_output" in k) or k.startswith(f"watch.{agent}") or it.get("visibility") == "public"
            if is_output and not internal:
                node.append(
                    {
                        "key": k,
                        "visibility": it.get("visibility"),
                        "updated": it.get("updated_at"),
                        "created": it.get("created_at"),
                    }
                )
        node.sort(key=lambda x: x.get("updated") or "", reverse=True)
        return {
            "agent": agent,
            "raw_count": len(raw),
            "in_sync": len(raw) == 0,
            "published_count": len(node),
            "published": node,
            "attached": r is not None,
        }

    @app.get("/api/agents/{agent}/key", dependencies=[require_token])
    def read_node_key(agent: str, key: str = Query(...)) -> dict:
        """Read one of the agent's published memory keys ON THE NODE — so the Sync view can show the actual
        deliverable (a news summary, etc.) for any key."""
        import json as _json

        from crewaimeat.aimeat_crew import _aimeat_call

        r = _aimeat_call(agent, "aimeat_memory_read", {"key": key})
        val = (r.get("value") if isinstance(r, dict) else r) if r else None
        text = (
            val if isinstance(val, str) else (None if val is None else _json.dumps(val, ensure_ascii=False, indent=1))
        )
        return {"key": key, "value": text}

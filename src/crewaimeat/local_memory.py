"""local_memory — the agent's own DURABLE, PRIVATE working memory (the two-tier-memory keystone).

The keystone of aimeat-agency's two-tier model: **local is scratch/staging, the node is home.** A crew
writes everything it finds — raw material, transient state, half-formed thinking — to this local store
freely. Only the *refined, finished* subset is then PUBLISHED UPWARD to the owner's node memory (the
canonical home). Publishing is explicit (manual-select by default, or a brain rule like
"auto-publish anything tagged `final`") — there is no automatic leak outward, the mirror of "no
automatic data import".

Where it lives: one SQLite file under AIMEAT_HOME (`local_memory.db`, per-repo, gitignored) — same
zero-infra pattern as `session_store`: stdlib `sqlite3`, WAL so readers never block the one writer, a
fresh short-lived connection per call (the fleet host runs agents as threads), JSON bodies. Unlike
`session_store` (ephemeral, TTL-pruned conversation state) this is the agent's *brain/scratch* and is
NOT auto-expired.

This module is pure storage + an explicit publish verb. The "which records flow upward" policy (manual
pick vs a tag rule) lives in the caller (the brain/scaffold), not here. The publish verb reuses the
exact same `aimeat_memory_write` path the scaffold's deliverable-publish uses, so a published local
record becomes an ordinary owner memory key the rest of the system already understands.

    from crewaimeat import local_memory as lm
    rid = lm.remember("news-watcher", {"finding": "..."}, topic="funding", source="techcrunch")
    lm.browse("news-watcher", topic="funding", status="raw")        # faceted read (time/topic/event/source)
    lm.publish("news-watcher", rid, key="news.2026-06-28.funding")  # refine → push upward → mark published
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid

from crewaimeat._home import aimeat_home

# Record lifecycle: born "raw" (local scratch); flips to "published" once mirrored upward to the node.
RAW = "raw"
PUBLISHED = "published"

_COLUMNS = (
    "agent",
    "id",
    "ts",
    "topic",
    "event",
    "source",
    "body",
    "status",
    "published_at",
    "key",
    "visibility",
    "tags",
)


def _db_path() -> str:
    home = aimeat_home()
    os.makedirs(home, exist_ok=True)
    return os.path.join(home, "local_memory.db")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_db_path(), timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute(
        "CREATE TABLE IF NOT EXISTS records ("
        "agent TEXT NOT NULL, id TEXT NOT NULL, ts REAL NOT NULL, "
        "topic TEXT, event TEXT, source TEXT, body TEXT, "
        "status TEXT NOT NULL DEFAULT 'raw', published_at REAL, "
        "key TEXT, visibility TEXT, tags TEXT, "
        "PRIMARY KEY(agent, id))"
    )
    # Faceted browse is by (agent, time) and (agent, topic/event/source) — index the hot path.
    c.execute("CREATE INDEX IF NOT EXISTS records_agent_ts ON records(agent, ts DESC)")
    return c


def _row_to_record(row: sqlite3.Row | tuple) -> dict:
    d = dict(zip(_COLUMNS, row))
    try:
        d["body"] = json.loads(d["body"]) if d["body"] is not None else None
    except (ValueError, TypeError):
        pass  # leave as the raw stored string if it somehow isn't JSON
    try:
        d["tags"] = json.loads(d["tags"]) if d["tags"] else []
    except (ValueError, TypeError):
        d["tags"] = []
    return d


def remember(
    agent: str,
    body,
    *,
    id: str | None = None,
    topic: str | None = None,
    event: str | None = None,
    source: str | None = None,
    tags: list[str] | None = None,
    status: str = RAW,
) -> str:
    """Write (or overwrite, when `id` is given) one local record for `agent`. Returns its id.

    `body` is any JSON-able value (dict/list/str/number) — store findings freely. `topic`/`event`/
    `source` are the browse facets. New records are `raw` (local-only) until `publish()` mirrors them
    upward. Re-`remember`ing the same id preserves its publish state (status/published_at/key) so an
    in-place edit of a still-raw record stays raw and an edit of a published one keeps its link.
    """
    if not agent or not str(agent).strip():
        raise ValueError("agent is required")
    rid = id or uuid.uuid4().hex[:12]
    now = time.time()
    body_txt = json.dumps(body, ensure_ascii=False)
    tags_txt = json.dumps(list(tags or []), ensure_ascii=False)
    with _conn() as c:
        prev = c.execute(
            "SELECT ts, status, published_at, key, visibility FROM records WHERE agent=? AND id=?",
            (agent, rid),
        ).fetchone()
        if prev is None:
            c.execute(
                "INSERT INTO records(agent, id, ts, topic, event, source, body, status, tags) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (agent, rid, now, topic, event, source, body_txt, status, tags_txt),
            )
        else:
            # Preserve ts (creation time) and the existing publish linkage; update content + facets.
            c.execute(
                "UPDATE records SET topic=?, event=?, source=?, body=?, tags=? WHERE agent=? AND id=?",
                (topic, event, source, body_txt, tags_txt, agent, rid),
            )
    return rid


def recall(agent: str, id: str) -> dict | None:
    """Read one record by id, or None if absent."""
    with _conn() as c:
        row = c.execute(f"SELECT {', '.join(_COLUMNS)} FROM records WHERE agent=? AND id=?", (agent, id)).fetchone()
    return _row_to_record(row) if row else None


def browse(
    agent: str,
    *,
    topic: str | None = None,
    event: str | None = None,
    source: str | None = None,
    status: str | None = None,
    since: float | None = None,
    until: float | None = None,
    tag: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Faceted, newest-first read. Filter by any of topic / event / source / status / time window
    (`since`/`until` are epoch seconds). `tag` filters to records carrying that tag. Pure SQL except the
    tag match (kept in Python so we don't depend on the SQLite JSON1 extension)."""
    where = ["agent=?"]
    args: list = [agent]
    for col, val in (("topic", topic), ("event", event), ("source", source), ("status", status)):
        if val is not None:
            where.append(f"{col}=?")
            args.append(val)
    if since is not None:
        where.append("ts>=?")
        args.append(since)
    if until is not None:
        where.append("ts<=?")
        args.append(until)
    sql = f"SELECT {', '.join(_COLUMNS)} FROM records WHERE {' AND '.join(where)} ORDER BY ts DESC LIMIT ?"
    # Over-fetch when tag-filtering so the LIMIT still applies after the Python tag pass.
    args.append(limit if tag is None else max(limit * 4, limit))
    with _conn() as c:
        rows = c.execute(sql, args).fetchall()
    out = [_row_to_record(r) for r in rows]
    if tag is not None:
        out = [r for r in out if tag in (r.get("tags") or [])][:limit]
    return out


def facets(agent: str) -> dict:
    """The browse facets for `agent`, each a value→count map: by `topic`, `event`, `source`, `status`,
    and `day` (UTC date). Feeds the cockpit's memory-browser facet rails."""
    out: dict[str, dict] = {}
    with _conn() as c:
        for facet, expr in (
            ("topic", "topic"),
            ("event", "event"),
            ("source", "source"),
            ("status", "status"),
            ("day", "date(ts, 'unixepoch')"),
        ):
            rows = c.execute(
                f"SELECT {expr} AS v, COUNT(*) AS n FROM records WHERE agent=? AND {expr} IS NOT NULL "
                "GROUP BY v ORDER BY n DESC",
                (agent,),
            ).fetchall()
            out[facet] = {str(v): n for (v, n) in rows}
    return out


def mark_published(agent: str, id: str, *, key: str | None = None, visibility: str | None = None) -> bool:
    """Flip a record to `published` and stamp when + where it went. Returns False if the id is unknown.
    Usually you call `publish()` (which does the upward write THEN this); use this directly only to
    record a publish that happened by another path."""
    now = time.time()
    with _conn() as c:
        cur = c.execute(
            "UPDATE records SET status=?, published_at=?, key=COALESCE(?, key), "
            "visibility=COALESCE(?, visibility) WHERE agent=? AND id=?",
            (PUBLISHED, now, key, visibility, agent, id),
        )
        return cur.rowcount > 0


def forget(agent: str, id: str | None = None) -> int:
    """Delete one record (by id) or ALL of the agent's records (id=None). Returns rows removed."""
    with _conn() as c:
        if id is None:
            cur = c.execute("DELETE FROM records WHERE agent=?", (agent,))
        else:
            cur = c.execute("DELETE FROM records WHERE agent=? AND id=?", (agent, id))
        return cur.rowcount


def publish(agent: str, id: str, *, key: str, visibility: str = "owner") -> dict:
    """Refine → push UPWARD: mirror a local record's body to the owner's node memory at `key`, then mark
    it published. This is the ONLY path local scratch leaves the machine, and it is explicit by design.

    Reuses the connector's `aimeat_memory_write` (the same path the scaffold's deliverable-publish uses),
    imported lazily so this module stays usable/testable without the connector. `visibility`: 'owner'
    (default — owner + same-owner agents) or 'public' (anyone, no login). Returns
    {ok, id, key, visibility} or {ok: False, error}."""
    vis = (visibility or "owner").strip().lower()
    if vis not in ("owner", "public"):
        return {"ok": False, "error": "visibility must be 'owner' or 'public'"}
    if not key or not str(key).strip():
        return {"ok": False, "error": "key is required (the owner memory key to publish to)"}
    rec = recall(agent, id)
    if rec is None:
        return {"ok": False, "error": f"no local record '{id}' for agent '{agent}'"}

    from crewaimeat.aimeat_crew import _aimeat_call  # lazy: keep local_memory connector-free to import

    r = _aimeat_call(agent, "aimeat_memory_write", {"key": key, "value": rec["body"], "visibility": vis})
    if r is None:
        return {"ok": False, "error": f"memory_write returned nothing for '{key}'"}
    mark_published(agent, id, key=key, visibility=vis)
    return {"ok": True, "id": id, "key": key, "visibility": vis}


def make_local_memory_tools(agent_name: str) -> list:
    """crewai tools over this agent's LOCAL tier — give them to any crew that should keep its own working
    memory and choose what to publish upward (mirrors `make_memory_tools`, but local).

        from crewaimeat.local_memory import make_local_memory_tools
        agent = Agent(..., tools=[*make_local_memory_tools(AGENT_NAME)], llm=ctx.llm)
    """
    from crewai.tools import tool  # lazy: the storage API above stays importable without crewai

    def _parse_body(value: str):
        sv = value.strip() if isinstance(value, str) else value
        if isinstance(sv, str) and sv[:1] in ("{", "["):
            try:
                return json.loads(sv)
            except Exception:  # noqa: BLE001 — not JSON, keep the text
                return value
        return value

    @tool("remember")
    def remember_tool(body: str, topic: str = "", event: str = "", source: str = "", tags: str = "") -> str:
        """Save something to YOUR OWN private local memory (scratch/working notes — stays on this machine,
        NOT published). Use freely for findings, raw material, half-formed thinking. `body` is the content
        (plain text, or a JSON object/array string for structured data). `topic`/`event`/`source` are
        optional labels you can browse by later. `tags` is an optional comma-separated list. Returns the
        record id (keep it if you want to publish this later)."""
        tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()]
        rid = remember(
            agent_name,
            _parse_body(body),
            topic=(topic or None),
            event=(event or None),
            source=(source or None),
            tags=tag_list,
        )
        return f"OK: remembered locally as id '{rid}' (status=raw, not published)."

    @tool("browse_memory")
    def browse_tool(
        topic: str = "", event: str = "", source: str = "", status: str = "", tag: str = "", limit: int = 20
    ) -> str:
        """Browse YOUR local memory, newest first. Filter by any of topic / event / source / status
        ('raw' = local-only, 'published' = already mirrored upward) / tag. Returns a compact list of
        `id | status | topic | source | <preview>` — read a full record with recall_memory(id)."""
        recs = browse(
            agent_name,
            topic=(topic or None),
            event=(event or None),
            source=(source or None),
            status=(status or None),
            tag=(tag or None),
            limit=limit,
        )
        if not recs:
            return "No local records match."
        rows = []
        for r in recs:
            b = r["body"]
            preview = (b if isinstance(b, str) else json.dumps(b, ensure_ascii=False))[:80]
            rows.append(f"- {r['id']} | {r['status']} | {r.get('topic') or '-'} | {r.get('source') or '-'} | {preview}")
        return "your local memory:\n" + "\n".join(rows)

    @tool("recall_memory")
    def recall_tool(id: str) -> str:
        """Read one full local record by its id (from remember/browse). Returns the stored body."""
        r = recall(agent_name, id)
        if r is None:
            return f"NOT FOUND: no local record '{id}'."
        b = r["body"]
        out = b if isinstance(b, str) else json.dumps(b, ensure_ascii=False)
        return f"record '{id}' (status={r['status']}):\n{out[:8000]}"

    @tool("publish_memory")
    def publish_tool(id: str, key: str, visibility: str = "owner") -> str:
        """PUBLISH a local record UPWARD to the node — the only way local scratch leaves this machine, so
        do it ONLY for refined, finished output worth keeping. `key` is the owner memory key to write
        (e.g. 'news.2026-06-28.funding'). visibility: 'owner' (owner + same-owner agents) or 'public'
        (anyone, no login). Marks the record published. Returns OK or the error."""
        res = publish(agent_name, id, key=key, visibility=visibility)
        return (
            f"OK: published '{id}' -> '{res['key']}' (visibility={res['visibility']})."
            if res.get("ok")
            else f"FAILED: {res.get('error')}"
        )

    tools = [remember_tool, browse_tool, recall_tool, publish_tool]
    for _t in tools:  # live local state — never serve a cached result
        try:
            _t.cache_function = lambda *_a, **_k: False
        except Exception:  # noqa: BLE001
            pass
    return tools

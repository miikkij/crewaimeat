"""events — a per-agent ACTIVITY LOG (audit trail) for the agency cockpit.

Every meaningful thing the operator does to an agent is recorded here: brain saved (with what changed),
connected (device-auth), started / stopped / restarted, rolled back, dry-run. The Manage page's History
reads this so the operator can see "what has happened to this agent, and when" — not just brain
versions. Same zero-infra pattern as `local_memory`/`session_store`: one SQLite file under AIMEAT_HOME,
WAL, a fresh connection per call, JSON detail.

    from crewaimeat.agency import events
    events.record("news-watcher", "started")
    events.record("news-watcher", "brain_saved", {"version": 3, "changed": ["prose", "policy.autonomy"]})
    events.activity("news-watcher")   # newest-first timeline
"""

from __future__ import annotations

import json
import os
import sqlite3
import time

from crewaimeat._home import aimeat_home


def _db_path() -> str:
    home = aimeat_home()
    os.makedirs(home, exist_ok=True)
    return os.path.join(home, "events.db")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_db_path(), timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute(
        "CREATE TABLE IF NOT EXISTS events (agent TEXT NOT NULL, ts REAL NOT NULL, kind TEXT NOT NULL, detail TEXT)"
    )
    c.execute("CREATE INDEX IF NOT EXISTS events_agent_ts ON events(agent, ts DESC)")
    return c


def record(agent: str, kind: str, detail: dict | None = None) -> None:
    """Append one activity event for `agent` (e.g. kind='started', or 'brain_saved' with a detail dict).
    Best-effort: never raise into the caller (an audit write must not break the action it logs)."""
    try:
        with _conn() as c:
            c.execute(
                "INSERT INTO events(agent, ts, kind, detail) VALUES(?,?,?,?)",
                (agent, time.time(), kind, json.dumps(detail or {}, ensure_ascii=False)),
            )
    except Exception:  # noqa: BLE001 — logging the action must not fail the action
        pass


def activity(agent: str, limit: int = 100) -> list[dict]:
    """The agent's activity timeline, newest first: [{ts, kind, detail}]."""
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT ts, kind, detail FROM events WHERE agent=? ORDER BY ts DESC LIMIT ?",
                (agent, limit),
            ).fetchall()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for ts, kind, detail in rows:
        try:
            d = json.loads(detail) if detail else {}
        except (ValueError, TypeError):
            d = {}
        out.append({"ts": ts, "kind": kind, "detail": d})
    return out


def has_kind(agent: str, kind: str) -> bool:
    """Whether the agent already has an event of this kind (used to record 'connected' only once)."""
    try:
        with _conn() as c:
            r = c.execute("SELECT 1 FROM events WHERE agent=? AND kind=? LIMIT 1", (agent, kind)).fetchone()
        return r is not None
    except Exception:  # noqa: BLE001
        return False

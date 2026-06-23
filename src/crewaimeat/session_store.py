"""session_store — a tiny LOCAL SQLite key/value store scoped per (agent, conversation).

Crews that hold a multi-turn DM conversation need to remember state BETWEEN turns: the on_dm events are
separate wakes, so an "ask the user → they answer later" flow must persist what it found (e.g. the search
candidates it offered) until the answer arrives. This is that store — zero-infra: one SQLite file under
AIMEAT_HOME (per-repo, gitignored), stdlib `sqlite3`, JSON values, pruned by TTL.

Thread-safe by design: a fresh short-lived connection per call (the fleet host runs agents as threads),
WAL mode so readers never block the one writer. Values are JSON, so store dicts/lists freely.

    from crewaimeat import session_store as ss
    ss.session_set("concierge", conv_id, "doc_candidates", {"ext": "pdf", "items": [...]})
    pending = ss.session_get("concierge", conv_id, "doc_candidates")   # -> the dict, or None
    ss.session_clear("concierge", conv_id, "doc_candidates")           # one key (or all for the conv)
"""

from __future__ import annotations

import json
import os
import sqlite3
import time

from crewaimeat._home import aimeat_home

_TTL_SECONDS = 7 * 24 * 3600  # forget conversation state older than a week


def _db_path() -> str:
    home = aimeat_home()
    os.makedirs(home, exist_ok=True)
    return os.path.join(home, "sessions.db")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_db_path(), timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute(
        "CREATE TABLE IF NOT EXISTS sessions "
        "(agent TEXT, conv TEXT, key TEXT, value TEXT, updated REAL, PRIMARY KEY(agent, conv, key))"
    )
    return c


def session_set(agent: str, conv: str, key: str, value) -> None:
    """Upsert one JSON-able value for (agent, conversation, key). Prunes anything past the TTL."""
    now = time.time()
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO sessions(agent, conv, key, value, updated) VALUES(?,?,?,?,?)",
            (agent, conv, key, json.dumps(value), now),
        )
        c.execute("DELETE FROM sessions WHERE updated < ?", (now - _TTL_SECONDS,))


def session_get(agent: str, conv: str, key: str, default=None):
    """Read the value for (agent, conversation, key), or `default` if absent/unparseable."""
    with _conn() as c:
        row = c.execute("SELECT value FROM sessions WHERE agent=? AND conv=? AND key=?", (agent, conv, key)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row[0])
    except (ValueError, TypeError):
        return default


def session_clear(agent: str, conv: str, key: str | None = None) -> None:
    """Delete one key for the conversation, or ALL of the conversation's state when key is None."""
    with _conn() as c:
        if key is None:
            c.execute("DELETE FROM sessions WHERE agent=? AND conv=?", (agent, conv))
        else:
            c.execute("DELETE FROM sessions WHERE agent=? AND conv=? AND key=?", (agent, conv, key))

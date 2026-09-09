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
from crewaimeat._sqlite import database

_TTL_SECONDS = 7 * 24 * 3600  # forget conversation state older than a week
_LEGACY_CONFIG_CONVS = ("_briefing", "_sanomat_desk")


def _db_path() -> str:
    home = aimeat_home()
    os.makedirs(home, exist_ok=True)
    return os.path.join(home, "sessions.db")


def _schema(c: sqlite3.Connection) -> None:
    c.execute(
        "CREATE TABLE IF NOT EXISTS sessions "
        "(agent TEXT, conv TEXT, key TEXT, value TEXT, updated REAL, PRIMARY KEY(agent, conv, key))"
    )
    c.execute(
        "CREATE TABLE IF NOT EXISTS preferences "
        "(agent TEXT, namespace TEXT, key TEXT, value TEXT, PRIMARY KEY(agent, namespace, key))"
    )


def _conn():
    return database(_db_path(), _schema)


def session_set(agent: str, conv: str, key: str, value) -> None:
    """Upsert one JSON-able value for (agent, conversation, key). Prunes anything past the TTL."""
    now = time.time()
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO sessions(agent, conv, key, value, updated) VALUES(?,?,?,?,?)",
            (agent, conv, key, json.dumps(value), now),
        )
        # Preserve pre-migration preferences until their first durable read, even if they are old.
        c.execute(
            "DELETE FROM sessions WHERE updated <= ? AND NOT (conv IN (?, ?) AND key='config')",
            (now - _TTL_SECONDS, *_LEGACY_CONFIG_CONVS),
        )


def session_get(agent: str, conv: str, key: str, default=None):
    """Read unexpired conversation state; expiry is independent of later writes."""
    with _conn() as c:
        row = c.execute(
            "SELECT value FROM sessions WHERE agent=? AND conv=? AND key=? AND updated > ?",
            (agent, conv, key, time.time() - _TTL_SECONDS),
        ).fetchone()
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


def session_consume(agent: str, conv: str, key: str, expected) -> bool:
    """Atomically consume exactly the unexpired value read by the caller, at most once.

    Another worker may have consumed or replaced it since the read. The conditional DELETE makes
    either race a miss, so an old approval cannot remove or authorize a newer pending action.
    """
    with _conn() as c:
        result = c.execute(
            "DELETE FROM sessions WHERE agent=? AND conv=? AND key=? AND value=? AND updated > ?",
            (agent, conv, key, json.dumps(expected), time.time() - _TTL_SECONDS),
        )
        return result.rowcount == 1


def preference_get(agent: str, namespace: str, key: str, default=None):
    """Read durable preferences, migrating the two historical pseudo-conversations lazily."""
    with _conn() as c:
        if namespace in _LEGACY_CONFIG_CONVS and key == "config":
            c.execute(
                "INSERT OR IGNORE INTO preferences(agent, namespace, key, value) "
                "SELECT agent, conv, key, value FROM sessions WHERE agent=? AND conv=? AND key=?",
                (agent, namespace, key),
            )
            c.execute("DELETE FROM sessions WHERE agent=? AND conv=? AND key=?", (agent, namespace, key))
        row = c.execute(
            "SELECT value FROM preferences WHERE agent=? AND namespace=? AND key=?", (agent, namespace, key)
        ).fetchone()
    if row is None:
        return default
    return json.loads(row[0])


def preference_set(agent: str, namespace: str, key: str, value) -> None:
    """Save preferences until the owner changes or deletes them; conversation TTL does not apply."""
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO preferences(agent, namespace, key, value) VALUES(?,?,?,?)",
            (agent, namespace, key, json.dumps(value)),
        )

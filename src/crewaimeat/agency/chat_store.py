"""chat_store — conversation history for the aimeat-agency copilot.

Append-only chat turns per session (mirrors the `events` audit store: one SQLite file under AIMEAT_HOME,
WAL, fresh connection per call, best-effort writes). The UI rehydrates the pane from `history`; the
advisor gets a small windowed tail as context. `actions` (the propose-then-confirm buttons an assistant
turn suggested) are stored as JSON so the pane can re-render them.

    from crewaimeat.agency import chat_store
    chat_store.append("sess-1", "user", "what can I build?")
    chat_store.append("sess-1", "assistant", "You could…", actions=[{"kind": "build_app", "agent": "x"}])
    chat_store.history("sess-1")          # newest LAST (chat order)
    chat_store.window("sess-1", turns=6)  # the last N turns for the LLM prompt
"""

from __future__ import annotations

import json
import os
import sqlite3
import time

from crewaimeat._home import aimeat_home

_KEEP_PER_SESSION = 400  # keep a deep tail; the LLM only ever sees `window(...)`


def _db_path() -> str:
    home = aimeat_home()
    os.makedirs(home, exist_ok=True)
    return os.path.join(home, "chat.db")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_db_path(), timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute(
        "CREATE TABLE IF NOT EXISTS messages ("
        "session_id TEXT NOT NULL, ts REAL NOT NULL, role TEXT NOT NULL, text TEXT, actions TEXT)"
    )
    c.execute("CREATE INDEX IF NOT EXISTS messages_session_ts ON messages(session_id, ts)")
    return c


def append(session_id: str, role: str, text: str, actions: list | None = None) -> None:
    """Append one turn (role='user'|'assistant'), trimming the session to the newest _KEEP_PER_SESSION.
    Best-effort: an audit write must never break the chat it logs."""
    try:
        with _conn() as c:
            c.execute(
                "INSERT INTO messages(session_id, ts, role, text, actions) VALUES(?,?,?,?,?)",
                (session_id, time.time(), role, text or "", json.dumps(actions or [], ensure_ascii=False)),
            )
            c.execute(
                "DELETE FROM messages WHERE session_id=? AND rowid IN ("
                "SELECT rowid FROM messages WHERE session_id=? ORDER BY ts DESC, rowid DESC LIMIT -1 OFFSET ?)",
                (session_id, session_id, _KEEP_PER_SESSION),
            )
    except Exception:  # noqa: BLE001
        pass


def history(session_id: str, limit: int = 200) -> list[dict]:
    """The session's turns in chat order (oldest first): [{ts, role, text, actions}]."""
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT ts, role, text, actions FROM messages WHERE session_id=? ORDER BY ts, rowid LIMIT ?",
                (session_id, limit),
            ).fetchall()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for ts, role, text, actions in rows:
        try:
            acts = json.loads(actions) if actions else []
        except (ValueError, TypeError):
            acts = []
        out.append({"ts": ts, "role": role, "text": text or "", "actions": acts})
    return out


def window(session_id: str, turns: int = 6) -> list[dict]:
    """The last `turns` turns (role+text only) for the LLM prompt — small, to fit a local model's context."""
    rows = history(session_id, limit=turns)
    return [{"role": r["role"], "text": r["text"]} for r in rows]

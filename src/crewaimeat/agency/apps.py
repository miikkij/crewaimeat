"""apps — a per-agent pointer to the AIMEAT app the appliance built to SHOW that agent's data.

When the operator clicks "Build an app to show this data", the cockpit publishes a data dashboard on
aimeat.io and records here where it lives, so the Manage/Sync UI can offer "Open app" / "Rebuild" and the
next-steps journey can mark the `data_app` step done. One row per agent (rebuild updates it in place, the
same filename → the node keeps the app's version history). Same zero-infra pattern as `events`: one
SQLite file under AIMEAT_HOME, WAL, a fresh connection per call.

    from crewaimeat.agency import apps
    apps.set_app("news-watcher", filename="news-watcher-dashboard.html", url="https://…", variant="dashboard",
                 visibility="owner", status="live", verified=None)
    apps.get_app("news-watcher")   # -> {agent, filename, url, variant, visibility, status, verified, built_ts}
"""

from __future__ import annotations

import os
import sqlite3
import time

from crewaimeat._home import aimeat_home

_COLUMNS = ("agent", "filename", "url", "variant", "visibility", "status", "verified", "built_ts")


def _db_path() -> str:
    home = aimeat_home()
    os.makedirs(home, exist_ok=True)
    return os.path.join(home, "agency_apps.db")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(_db_path(), timeout=10)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute(
        "CREATE TABLE IF NOT EXISTS apps ("
        "agent TEXT PRIMARY KEY, filename TEXT, url TEXT, variant TEXT, visibility TEXT, "
        "status TEXT, verified INTEGER, built_ts REAL)"
    )
    return c


def _verified_to_db(v) -> int | None:
    """True -> 1, False -> 0, None/unknown -> NULL (unverified)."""
    if v is True:
        return 1
    if v is False:
        return 0
    return None


def _row_to_app(row) -> dict:
    d = dict(zip(_COLUMNS, row))
    d["verified"] = {1: True, 0: False}.get(d.get("verified"))  # NULL -> None (unverified)
    return d


def set_app(
    agent: str,
    *,
    filename: str,
    url: str,
    variant: str,
    visibility: str,
    status: str = "live",
    verified=None,
) -> dict:
    """Upsert the app pointer for `agent`. `verified` is True (rendered OK), False (render failed), or
    None (published but not headless-verified — the owner opens it to check). Returns the stored row."""
    now = time.time()
    with _conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO apps(agent, filename, url, variant, visibility, status, verified, built_ts) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (agent, filename, url, variant, visibility, status, _verified_to_db(verified), now),
        )
    return get_app(agent)  # type: ignore[return-value]


def get_app(agent: str) -> dict | None:
    """The app pointer for `agent`, or None if no app has been built."""
    try:
        with _conn() as c:
            row = c.execute(f"SELECT {', '.join(_COLUMNS)} FROM apps WHERE agent=?", (agent,)).fetchone()
        return _row_to_app(row) if row else None
    except Exception:  # noqa: BLE001
        return None


def clear_app(agent: str) -> bool:
    """Forget the app pointer for `agent` (on brain delete). True if one existed."""
    try:
        with _conn() as c:
            cur = c.execute("DELETE FROM apps WHERE agent=?", (agent,))
            return cur.rowcount > 0
    except Exception:  # noqa: BLE001
        return False

"""Moving from 0.8.x: say plainly that this is a new app, and carry the person's own words across.

0.8.x kept "brains" (template + the person's prose + policy) in `<AIMEAT_HOME>/brains.db`. 2.0 has no
brains: an agent is a description turned into a definition on the node. So the first 2.0 start shows a
full-screen notice before anything else, lists the old agents, and offers "make again" with the old
prose prefilled as the description. Nothing old is deleted — `brains.db` is only read.
"""

from __future__ import annotations

import sqlite3
import time

from crewaimeat.agency2 import paths


def _db():
    return paths.aimeat_home() / "brains.db"


def _ack_path():
    return paths.state_dir() / "migration.json"


def old_agents() -> list[dict]:
    db = _db()
    if not db.is_file():
        return []
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT agent_name, template_id, prose, title FROM brains ORDER BY agent_name"
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error as exc:
        return [
            {"name": "?", "template": "", "description": "", "title": f"the old agent list could not be read: {exc}"}
        ]
    return [{"name": r[0], "template": r[1] or "", "description": r[2] or "", "title": r[3] or ""} for r in rows]


def status() -> dict:
    ack = paths.read_json(_ack_path(), None)
    olds = old_agents()
    return {"pending": bool(olds) and not ack, "old_agents": olds, "acknowledged": ack}


def acknowledge() -> dict:
    v = {"at": time.time()}
    paths.write_json(_ack_path(), v)
    return v

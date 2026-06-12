"""Durable per-machine run markers for recurring contract work.

The platform read can lag or FREEZE behind our own publishes (observed live: a record
stuck at an old snapshot while its publishes reached v5), so neither a record's
`last_run` nor the output listing alone may decide whether recurring work is due —
after a daemon restart a stale read makes everything look due again (the "6 market-scan
mails in one day" incident). These markers are the machine's own truth: written after a
successful run, consulted before re-running. They complement (never replace) the
workspace-side state, exactly like the ledger inbox's `.processed.json`.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path


def _path(name: str) -> Path:
    return Path("logs") / f".{name}_runs.json"


def _load(name: str) -> dict:
    try:
        return json.loads(_path(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def last_local_run(name: str, rid: str) -> datetime.datetime | None:
    ts = _load(name).get(rid)
    if not ts:
        return None
    try:
        return datetime.datetime.fromisoformat(ts)
    except ValueError:
        return None


def mark_local_run(name: str, rid: str) -> None:
    runs = _load(name)
    runs[rid] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    p = _path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(runs, indent=0, sort_keys=True), encoding="utf-8")


def ran_within(name: str, rid: str, hours: float) -> bool:
    """True when THIS MACHINE ran `rid` within the window — a stale platform read must
    never re-trigger work the machine knows it just did."""
    last = last_local_run(name, rid)
    if last is None:
        return False
    age = datetime.datetime.now(datetime.timezone.utc) - last
    return age.total_seconds() < hours * 3600

"""Tests for mroom-researcher's reliable-trigger logic (_requested + _is_stale) — the catch-up that
re-picks-up a stranded `requested` / stale `in-progress` claim so a lost run never waits for a human nudge
(AIMEAT dev directive, doc-qld5qo5). Output-existence dedup keeps re-claiming a stale claim safe."""

from __future__ import annotations

import datetime

from crewaimeat import mroom_researcher as mr


def _iso(minutes_ago: int) -> str:
    t = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=minutes_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def test_requested_picks_requested_and_stale_inprogress():
    room = {
        "objects": {
            "research-request": [
                {"id": "a", "status": "requested"},  # fresh request -> fulfil
                {"id": "b", "status": "in-progress", "_updatedAt": _iso(30)},  # stale claim (dead run) -> re-pick
                {"id": "c", "status": "in-progress", "_updatedAt": _iso(1)},  # fresh claim -> leave (in flight)
                {"id": "d", "status": "done"},  # done -> skip
                {"id": "e", "status": "requested"},  # already has a result -> skip
            ],
            "research-result": [{"id": "res-e"}],
        }
    }
    assert [r["id"] for r in mr._requested(room)] == ["a", "b"]


def test_is_stale():
    assert mr._is_stale({"_updatedAt": _iso(30)}) is True
    assert mr._is_stale({"_updatedAt": _iso(1)}) is False
    assert mr._is_stale({}) is True  # missing timestamp -> treat as stale (re-pick; dedup keeps it safe)
    assert mr._is_stale({"_updatedAt": "garbage"}) is True

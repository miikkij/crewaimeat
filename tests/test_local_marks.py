"""local_marks floor — the durable per-machine run markers (no network)."""

import datetime
import json

from crewaimeat import local_marks


def test_marker_roundtrip_and_window(tmp_path, monkeypatch):
    monkeypatch.setattr(local_marks, "_path", lambda name: tmp_path / f".{name}_runs.json")
    assert local_marks.last_local_run("x", "r1") is None
    assert not local_marks.ran_within("x", "r1", 24)
    local_marks.mark_local_run("x", "r1")
    assert local_marks.ran_within("x", "r1", 24)
    assert not local_marks.ran_within("x", "r2", 24)  # other ids unaffected


def test_old_marker_does_not_block_next_period(tmp_path, monkeypatch):
    monkeypatch.setattr(local_marks, "_path", lambda name: tmp_path / f".{name}_runs.json")
    old = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=200)).isoformat()
    (tmp_path / ".x_runs.json").write_text(json.dumps({"r1": old}), encoding="utf-8")
    assert not local_marks.ran_within("x", "r1", 168)  # a week-old run -> due again


def test_corrupt_marker_file_fails_open(tmp_path, monkeypatch):
    monkeypatch.setattr(local_marks, "_path", lambda name: tmp_path / f".{name}_runs.json")
    (tmp_path / ".x_runs.json").write_text("EI JSONIA", encoding="utf-8")
    assert not local_marks.ran_within("x", "r1", 24)  # unreadable -> don't block work
    local_marks.mark_local_run("x", "r1")             # and writing repairs the file
    assert local_marks.ran_within("x", "r1", 24)

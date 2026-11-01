"""The run record (tests/run_record.py): a failure is written down, and an unlisted skip fails a locked run.

Each test runs a small inner pytest session in-process (pytester) with the plugin, its paths pointed into
tmp_path, and the plugin's state swapped for fresh copies so the outer run's record is not touched.
"""

from __future__ import annotations

import json

import pytest
import run_record

pytest_plugins = ["pytester"]


@pytest.fixture
def inner(pytester, tmp_path, monkeypatch):
    monkeypatch.setattr(run_record, "FAILURE_LOG", tmp_path / "failures.jsonl")
    monkeypatch.setattr(run_record, "SESSIONS", tmp_path / "sessions")
    monkeypatch.setattr(run_record, "ALLOWLIST", tmp_path / "allow.json")
    monkeypatch.setattr(run_record, "_RUN", {"id": "", "index": 0, "previous": None, "commit": None})
    monkeypatch.setattr(run_record, "_SKIPPED", {})
    monkeypatch.setattr(run_record, "_commit", lambda: "abc1234")
    (tmp_path / "allow.json").write_text(json.dumps({"allowed": []}), encoding="utf-8")
    pytester.makepyfile(
        test_inner="""
import pytest

def test_first():
    pass

def test_breaks():
    assert 1 == 2, "the reason it broke"

def test_not_here():
    pytest.skip("nothing to run it with")
"""
    )
    return pytester


def test_a_failure_is_written_with_its_reason_and_its_place(inner, tmp_path):
    inner.runpytest_inprocess("-p", "run_record", "-p", "no:cacheprovider")
    rows = [json.loads(line) for line in (tmp_path / "failures.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [r["test"] for r in rows] == ["test_inner.py::test_breaks"]
    row = rows[0]
    assert row["phase"] == "call" and "the reason it broke" in row["longrepr"]
    assert "the reason it broke" in row["traceback_full"] and "test_inner.py" in row["traceback_full"]
    assert row["previous_test"] == "test_inner.py::test_first" and row["index"] == 1
    assert row["commit"] == "abc1234" and row["run"]
    assert (tmp_path / "sessions").is_dir() and not list((tmp_path / "sessions").glob("*.json")), "its own file goes"


def test_an_unlisted_skip_fails_a_locked_run(inner, monkeypatch):
    monkeypatch.setattr(run_record, "SKIP_LOCK", True)
    result = inner.runpytest_inprocess("-p", "run_record", "-p", "no:cacheprovider", "-k", "not breaks")
    assert result.ret == 1
    result.stdout.fnmatch_lines(["*SKIPS NOT IN tests/skip-allowlist.json*", "*test_not_here: nothing to run it with*"])


def test_a_listed_skip_passes_a_locked_run(inner, tmp_path, monkeypatch):
    monkeypatch.setattr(run_record, "SKIP_LOCK", True)
    allow = {"allowed": [{"test": "test_inner.py::test_not_here", "reason": "no tool here"}]}
    (tmp_path / "allow.json").write_text(json.dumps(allow), encoding="utf-8")
    assert inner.runpytest_inprocess("-p", "run_record", "-p", "no:cacheprovider", "-k", "not breaks").ret == 0


def test_without_the_lock_a_skip_is_only_shown(inner, monkeypatch):
    monkeypatch.setattr(run_record, "SKIP_LOCK", False)  # the outer run may be locked (CI is)
    assert inner.runpytest_inprocess("-p", "run_record", "-p", "no:cacheprovider", "-k", "not breaks").ret == 0


def test_the_allowlist_names_each_test_once_with_a_reason():
    doc = json.loads(run_record.ALLOWLIST.read_text(encoding="utf-8"))
    tests = [e["test"] for e in doc["allowed"]]
    assert len(tests) == len(set(tests))
    assert all(e.get("reason", "").strip() for e in doc["allowed"]), "a skip is allowed only with its reason"

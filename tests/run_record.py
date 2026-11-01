"""What a test run leaves behind: every failure written down, and every skip accounted for.

Loaded by conftest (`pytest_plugins`). Two jobs, both from 2026-11-01:

FAILURES ARE WRITTEN DOWN. `tests/test_forge_eval.py::test_grade_pure_reasoning_passes_with_no_tools`
failed once in a full run and passed in every one of nine reruns, and nothing was left to say why: the
terminal had scrolled past it. Each failure is now one JSON line in `logs/test-failures.jsonl`
(CREWAIMEAT_TEST_FAILURE_LOG overrides the path) with what an order or load question needs: the test,
the phase, the whole traceback and captured output, the test that ran just before it, its place in
the run, the run's arguments, the commit, and the other pytest runs that were going at the same time
on this machine. CI uploads the file when a job fails.

SKIPS ARE ACCOUNTED FOR. CI was green while 76 tests were skipped, among them every live suite, which
had stopped in its fixture for three weeks and hid a production fault (973bf75). With
CREWAIMEAT_SKIP_LOCK=1 (CI sets it) a skip that `tests/skip-allowlist.json` does not name FAILS the run.
A new skip therefore arrives as an edit of that file, with its reason, where a reviewer sees it; an
entry that skipped nowhere is named at the end so the list can shrink.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FAILURE_LOG = Path(os.environ.get("CREWAIMEAT_TEST_FAILURE_LOG") or ROOT / "logs" / "test-failures.jsonl")
SESSIONS = ROOT / "logs" / ".pytest-sessions"
ALLOWLIST = Path(__file__).resolve().parent / "skip-allowlist.json"
SKIP_LOCK = os.environ.get("CREWAIMEAT_SKIP_LOCK") == "1"

_RUN = {"id": "", "index": 0, "previous": None, "commit": None}
_SKIPPED: dict[str, str] = {}  # nodeid -> reason
_FULL: dict[tuple[str, str], str] = {}  # (nodeid, phase) -> the traceback with no frame hidden


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _commit() -> str | None:
    # Read before any test runs: conftest refuses a subprocess inside a test, not here.
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=10
        )
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001 — no git is "unknown", which the record says
        return None


def _other_sessions() -> list[dict]:
    """The other pytest runs that announced themselves in this checkout. A run that crashed leaves its
    file behind, so each entry carries when it started and the reader judges."""
    out = []
    for f in sorted(SESSIONS.glob("*.json")) if SESSIONS.is_dir() else []:
        if f.stem == _RUN["id"]:
            continue
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return out


def _load() -> dict:
    load: dict = {"cpus": os.cpu_count()}
    if hasattr(os, "getloadavg"):
        load["loadavg"] = os.getloadavg()
    return load


def pytest_sessionstart(session):
    _RUN["id"] = f"{datetime.now(UTC):%Y%m%dT%H%M%S}-{os.getpid()}"
    _RUN["commit"] = _commit()
    try:
        SESSIONS.mkdir(parents=True, exist_ok=True)
        (SESSIONS / f"{_RUN['id']}.json").write_text(
            json.dumps({"run": _RUN["id"], "pid": os.getpid(), "started": _now(), "argv": sys.argv[1:]}),
            encoding="utf-8",
        )
    except OSError:
        pass  # a read-only checkout records no session; the failure log below says so if it cannot write


def _write_failure(report) -> None:
    record = {
        "at": _now(),
        "run": _RUN["id"],
        "test": report.nodeid,
        "phase": report.when,
        "index": _RUN["index"],
        "previous_test": _RUN["previous"],
        "duration_s": round(getattr(report, "duration", 0.0), 3),
        "longrepr": str(report.longrepr),
        # pytest hides its own frames from longrepr; this keeps them. On 2026-11-01 one failure was
        # followed by 450 errors that each showed only `contextlib.__exit__` raising a
        # UnicodeDecodeError, with no word of whose context manager it was.
        "traceback_full": _FULL.pop((report.nodeid, report.when), None),
        "stdout": getattr(report, "capstdout", ""),
        "stderr": getattr(report, "capstderr", ""),
        "argv": sys.argv[1:],
        "commit": _RUN["commit"],
        "host": platform.node(),
        "platform": sys.platform,
        "python": platform.python_version(),
        "load": _load(),
        "other_sessions": _other_sessions(),
    }
    try:
        FAILURE_LOG.parent.mkdir(parents=True, exist_ok=True)
        with FAILURE_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"\n[run_record] could not write {FAILURE_LOG}: {exc}", file=sys.stderr)


def _skip_reason(report) -> str:
    longrepr = report.longrepr
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        return str(longrepr[2]).removeprefix("Skipped: ")
    return str(longrepr)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if call.excinfo is not None and report.failed:
        _FULL[(item.nodeid, call.when)] = "".join(traceback.format_exception(call.excinfo.value))
    return report


def pytest_runtest_logreport(report):
    if report.failed:
        _write_failure(report)
    elif report.skipped and not hasattr(report, "wasxfail"):
        _SKIPPED[report.nodeid] = _skip_reason(report)
    if report.when == "teardown":
        _RUN["index"] += 1
        _RUN["previous"] = report.nodeid


def pytest_collectreport(report):
    if report.failed:
        _write_failure(report)
    elif report.skipped:
        _SKIPPED[report.nodeid] = _skip_reason(report)


def _allowed() -> dict[str, str]:
    doc = json.loads(ALLOWLIST.read_text(encoding="utf-8"))
    return {e["test"]: e["reason"] for e in doc["allowed"]}


def _unaccounted() -> tuple[list[tuple[str, str]], list[str]]:
    allowed = _allowed()
    unaccounted = sorted((t, r) for t, r in _SKIPPED.items() if t not in allowed)
    unused = sorted(t for t in allowed if t not in _SKIPPED)
    return unaccounted, unused


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    if os.path.exists(FAILURE_LOG) and (terminalreporter.stats.get("failed") or terminalreporter.stats.get("error")):
        terminalreporter.write_line(f"failures written to {FAILURE_LOG} (run {_RUN['id']})")
    if not SKIP_LOCK:
        return
    unaccounted, unused = _unaccounted()
    if unaccounted:
        terminalreporter.section("SKIPS NOT IN tests/skip-allowlist.json", sep="=", red=True, bold=True)
        for test, reason in unaccounted:
            terminalreporter.write_line(f"{test}: {reason}")
        terminalreporter.write_line(
            "A skipped test is a test that did not run. Make it run here, or name it in "
            "tests/skip-allowlist.json with the reason it cannot."
        )
    if unused:
        terminalreporter.write_line(
            "skip-allowlist entries that skipped nowhere in this run (remove them once no job needs them): "
            + ", ".join(unused)
        )


def pytest_sessionfinish(session, exitstatus):
    try:
        (SESSIONS / f"{_RUN['id']}.json").unlink(missing_ok=True)
    except OSError:
        pass
    if SKIP_LOCK and _unaccounted()[0] and session.exitstatus == 0:
        session.exitstatus = 1


def pytest_report_header(config):
    lock = "ON: a skip not in tests/skip-allowlist.json fails the run" if SKIP_LOCK else "off"
    return f"run record: failures -> {FAILURE_LOG}; skip lock {lock}"

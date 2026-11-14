"""The serve watchdog starts no serve daemon on a place with no agent, and backs off a daemon that
keeps failing to start.

Measured on pannu-01 2026-11-14: on every pooled fleet place (no agent yet) the watchdog restarted
`aimeat connect serve` about every 27 s, which exits with "No agents configured"; 133-157 failed starts
an hour per place, each a Node boot at the tier's CPU cap.
"""

from __future__ import annotations

import sys
import types

import pytest

from crewaimeat import serve_watchdog as sw


class _Stop(Exception):
    pass


def _run_ticks(monkeypatch, tmp_path, ticks, ensure, on_tick=None):
    """Run the watchdog loop for `ticks` sleeps with `ensure` standing in for ensure_single_serve.
    Returns the sleep durations."""
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)  # the singleton lock lives under ./logs/.locks
    fake_guard = types.ModuleType("crewaimeat.serve_guard")
    fake_guard.ensure_single_serve = ensure
    monkeypatch.setitem(sys.modules, "crewaimeat.serve_guard", fake_guard)
    monkeypatch.setattr("crewaimeat._home.aimeat_home", lambda: tmp_path)
    slept: list[int] = []

    def fake_sleep(s):
        slept.append(s)
        if on_tick:
            on_tick(len(slept))
        if len(slept) >= ticks:
            raise _Stop

    monkeypatch.setattr(sw.time, "sleep", fake_sleep)
    with pytest.raises(_Stop):
        sw.run()
    return slept


def test_credential_detection(tmp_path):
    assert not sw.has_agent_credential(tmp_path)  # no tokens/ or keys/ at all
    (tmp_path / "tokens").mkdir()
    (tmp_path / "keys").mkdir()
    assert not sw.has_agent_credential(tmp_path)  # the empty dirs a fresh place has
    (tmp_path / "tokens" / "retired").mkdir()
    (tmp_path / "tokens" / "retired" / "old@alice.token").write_text("t", encoding="utf-8")
    (tmp_path / "tokens" / "empty@alice.token").write_text("", encoding="utf-8")
    (tmp_path / "keys" / "notes.txt").write_text("x", encoding="utf-8")
    assert not sw.has_agent_credential(tmp_path)  # retired, empty, and unrelated files do not count
    (tmp_path / "keys" / "helper@alice.key").write_text("k", encoding="utf-8")
    assert sw.has_agent_credential(tmp_path)
    (tmp_path / "keys" / "helper@alice.key").unlink()
    (tmp_path / "tokens" / "helper@alice.token").write_text("t", encoding="utf-8")
    assert sw.has_agent_credential(tmp_path)


def test_retry_delay_backs_off_to_a_cap():
    assert sw.retry_delay(0) == sw.INTERVAL
    assert sw.retry_delay(1) == sw.INTERVAL  # a crash of a running daemon is restarted at once
    assert sw.retry_delay(2) == sw.INTERVAL * 2
    assert sw.retry_delay(3) == sw.INTERVAL * 4
    assert sw.retry_delay(50) == sw.MAX_BACKOFF


def test_no_agent_no_daemon_until_one_is_enrolled(monkeypatch, tmp_path, capsys):
    calls = []

    def ensure():
        calls.append(1)
        return {"pid": 4242, "port": 0, "agents": [{"agent": "helper"}]}

    def enrol_on_tick(n):
        if n == 5:  # the first agent arrives after five quiet ticks
            (tmp_path / "keys").mkdir(exist_ok=True)
            (tmp_path / "keys" / "helper@alice.key").write_text("k", encoding="utf-8")

    slept = _run_ticks(monkeypatch, tmp_path, 7, ensure, enrol_on_tick)
    assert calls == [1, 1]  # nothing for five ticks, then the daemon on each following tick
    assert slept == [sw.INTERVAL] * 7
    out = capsys.readouterr().out
    assert out.count("no agent credential") == 1  # said once, not every tick
    assert "an agent credential appeared" in out


def test_a_daemon_that_cannot_start_is_retried_less_and_less(monkeypatch, tmp_path):
    (tmp_path / "tokens").mkdir()
    (tmp_path / "tokens" / "helper@alice.token").write_text("t", encoding="utf-8")

    def ensure():
        raise RuntimeError("daemon exited with code 1")

    slept = _run_ticks(monkeypatch, tmp_path, 6, ensure)
    assert slept == [20, 40, 80, 160, 300, 300]

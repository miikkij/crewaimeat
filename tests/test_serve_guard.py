"""serve_guard — the EXACTLY-ONE serve daemon guard, esp. the pid-registry that reaps our own stale
daemons even when the Windows env-read (_process_aimeat_home) returns None (the duplicate-daemon bug)."""

from __future__ import annotations

import crewaimeat.serve_guard as g


def _home(monkeypatch, tmp_path):
    monkeypatch.setattr(g, "_aimeat_home", lambda: tmp_path)
    return tmp_path


def test_registry_roundtrip_prunes_dead(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    monkeypatch.setattr(g, "_serve_pids", lambda: [10, 11])  # only 10,11 are live serves
    g._record_daemon(10)
    g._record_daemon(99)  # 99 not a live serve -> pruned on record
    assert g._load_registry() == {10}


def test_reap_kills_registered_stray_when_env_unreadable(monkeypatch, tmp_path):
    """The bug: a same-home duplicate whose env can't be read survived. Registry catches it."""
    _home(monkeypatch, tmp_path)
    monkeypatch.setattr(g, "_serve_pids", lambda: [100, 200])
    # env-read FAILS for both (None) — the old code would reap NEITHER
    monkeypatch.setattr(g, "_process_aimeat_home", lambda pid: None)
    g._save_registry({100, 200})  # both previously recorded as OURS
    killed: list = []
    monkeypatch.setattr(g, "_kill", lambda pid: killed.append(pid))
    reaped = g._reap_duplicates(keep_pid=200)
    assert reaped == 1 and killed == [100]  # the stray we recorded is reaped despite the unreadable env


def test_reap_respects_env_when_readable(monkeypatch, tmp_path):
    home = _home(monkeypatch, tmp_path)
    our = g._norm(home)
    monkeypatch.setattr(g, "_serve_pids", lambda: [1, 2])
    monkeypatch.setattr(g, "_process_aimeat_home", lambda pid: our)  # both same home
    g._save_registry(set())
    killed: list = []
    monkeypatch.setattr(g, "_kill", lambda pid: killed.append(pid))
    assert g._reap_duplicates(keep_pid=1) == 1 and killed == [2]


def test_pid_reuse_guard_spares_other_home(monkeypatch, tmp_path):
    """A registered pid whose env now POSITIVELY reads as a DIFFERENT home (pid reused) is NOT reaped."""
    home = _home(monkeypatch, tmp_path)
    our = g._norm(home)
    monkeypatch.setattr(g, "_serve_pids", lambda: [5, 6])
    # pid 6 was recorded by us, but its env now reads as ANOTHER home -> the pid was reused -> spare it
    monkeypatch.setattr(g, "_process_aimeat_home", lambda pid: our if pid == 5 else "C:\\other\\home")
    g._save_registry({5, 6})
    killed: list = []
    monkeypatch.setattr(g, "_kill", lambda pid: killed.append(pid))
    assert g._reap_duplicates(keep_pid=5) == 0 and killed == []  # 6 spared (different home)


def test_no_reap_when_single(monkeypatch, tmp_path):
    _home(monkeypatch, tmp_path)
    monkeypatch.setattr(g, "_serve_pids", lambda: [7])
    monkeypatch.setattr(g, "_process_aimeat_home", lambda pid: None)
    g._save_registry({7})
    monkeypatch.setattr(g, "_kill", lambda pid: (_ for _ in ()).throw(AssertionError("should not kill")))
    assert g._reap_duplicates(keep_pid=None) == 0

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


def test_this_home_serve_pids_scopes_by_home(monkeypatch, tmp_path):
    """The home-scoping terminate_fleet.ps1 relies on: only OUR home's serve pids, never another fleet's.
    An unreadable env (None) is treated as NOT ours (fail-safe) so we never kill a foreign/dev daemon."""
    home = _home(monkeypatch, tmp_path)
    our = g._norm(home)
    monkeypatch.setattr(g, "_serve_pids", lambda: [1, 2, 3])
    monkeypatch.setattr(g, "_process_aimeat_home", lambda pid: {1: our, 2: "C:\\dev\\other", 3: None}[pid])
    assert g.this_home_serve_pids() == [1]


# ── Fix C: serve.json's owner is re-asserted atomically after a reap ──────────────────────────────
import json  # noqa: E402

import aimeat_crewai.mcp_client as mc  # noqa: E402


def test_assert_serve_json_owner_rewrites_stale(monkeypatch, tmp_path):
    """A reaped LOSER may leave serve.json naming a now-dead pid — the exact stale window a crew's
    ensure_serve(auto_start=False) crashes on. After the reap we re-point it at the kept LIVE daemon."""
    sj = tmp_path / "serve.json"
    sj.write_text(json.dumps({"pid": 999, "port": 1, "agents": []}), encoding="utf-8")  # stale: dead 999
    monkeypatch.setattr(mc, "serve_discovery_path", lambda: sj)
    monkeypatch.setattr(mc, "_pid_alive", lambda pid: pid == 42)
    monkeypatch.setattr(mc, "_probe_serve", lambda port, pid, timeout=2.0: pid == 42)
    doc = {"pid": 42, "port": 1234, "agents": [{"agent": "w"}], "_reaped_duplicates": 1}
    assert g._assert_serve_json_owner(doc) is True
    written = json.loads(sj.read_text(encoding="utf-8"))
    assert written["pid"] == 42 and written["port"] == 1234
    assert "_reaped_duplicates" not in written  # internal markers stripped from the published file


def test_assert_serve_json_owner_noop_when_correct(monkeypatch, tmp_path):
    sj = tmp_path / "serve.json"
    sj.write_text(json.dumps({"pid": 42, "port": 1234, "agents": []}), encoding="utf-8")
    monkeypatch.setattr(mc, "serve_discovery_path", lambda: sj)
    monkeypatch.setattr(mc, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(
        mc, "_probe_serve", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not re-probe"))
    )
    before = sj.read_text(encoding="utf-8")
    assert g._assert_serve_json_owner({"pid": 42, "port": 1234, "agents": []}) is True
    assert sj.read_text(encoding="utf-8") == before  # already correct -> untouched


def test_assert_serve_json_owner_skips_dead_daemon(monkeypatch, tmp_path):
    """Never publish a doc for a daemon we cannot PROVE is live right now."""
    sj = tmp_path / "serve.json"
    sj.write_text(json.dumps({"pid": 999, "port": 1, "agents": []}), encoding="utf-8")
    monkeypatch.setattr(mc, "serve_discovery_path", lambda: sj)
    monkeypatch.setattr(mc, "_pid_alive", lambda pid: False)
    monkeypatch.setattr(mc, "_probe_serve", lambda *a, **k: False)
    before = sj.read_text(encoding="utf-8")
    assert g._assert_serve_json_owner({"pid": 42, "port": 1234, "agents": []}) is False
    assert sj.read_text(encoding="utf-8") == before

"""fleet_state pure-layer floor — deterministic, no OS, no network. Feeds fake process command
lines + a fake node index and asserts the status derivation, including the 'stale-heartbeat' case
(connector/daemon up locally but the node's last_seen is old)."""

import datetime

from crewaimeat.tui import fleet_state as fs

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 6, 15, 18, 0, tzinfo=UTC)


# ── age_seconds ───────────────────────────────────────────────────────────────
def test_age_seconds_handles_z_offset_and_missing():
    assert fs.age_seconds("2026-06-15T17:30:00Z", NOW) == 1800
    assert fs.age_seconds("2026-06-15T17:30:00+00:00", NOW) == 1800
    assert fs.age_seconds(None, NOW) is None
    assert fs.age_seconds("not-a-date", NOW) is None


# ── tally_processes ─────────────────────────────────────────────────────────────
def test_tally_counts_watchdog_daemon_and_zombies():
    cmds = [
        "pwsh -File scripts/watchdog.ps1 crews/news_fetcher_crew.py",      # watchdog
        "python .venv/Scripts/... crews/news_fetcher_crew.py",            # daemon
        "python crews/deleted_crew.py",                                    # zombie (no file)
        "node aimeat.js connect serve --http",                             # serve (ignored here)
    ]
    tally, zombies = fs.tally_processes(cmds, {"news_fetcher_crew.py"})
    assert tally["news_fetcher_crew.py"] == {"watchdog": 1, "daemon": 1}
    assert zombies == ["deleted_crew.py"]


# ── derive_status (every branch) ────────────────────────────────────────────────
def test_derive_status_branches():
    base = dict(lock=False, in_tunnel=True, age_s=10.0)
    assert fs.derive_status(watchdog=2, daemon=1, **base) == "DUPLICATE"
    assert fs.derive_status(watchdog=0, daemon=1, **base) == "orphan"
    assert fs.derive_status(watchdog=1, daemon=1, **base) == "running"
    # daemon up locally but node hasn't heard from it in > stale window -> stale-heartbeat
    assert fs.derive_status(watchdog=1, daemon=1, lock=True, in_tunnel=True, age_s=99999) == "stale-heartbeat"
    assert fs.derive_status(watchdog=0, daemon=0, lock=True, in_tunnel=False, age_s=None) == "down (stale lock)"
    assert fs.derive_status(watchdog=0, daemon=0, lock=False, in_tunnel=False, age_s=None) == "down"


# ── build_rows (integration of the pure pieces) ─────────────────────────────────
def test_build_rows_running_stale_and_zombie():
    roster = {"news-fetcher": "news_fetcher_crew.py", "image-maker": "image_maker_crew.py"}
    tally = {
        "news_fetcher_crew.py": {"watchdog": 1, "daemon": 1},
        "image_maker_crew.py": {"watchdog": 1, "daemon": 1},
        "ghost_crew.py": {"watchdog": 0, "daemon": 1},  # zombie: running, no crew file on disk
    }
    node_index = {
        "news-fetcher": {"last_seen": "2026-06-15T17:59:00Z", "mode": "task-runner"},   # 1 min -> fresh
        "image-maker": {"last_seen": "2026-06-13T22:59:00Z", "mode": "task-runner"},    # ~1.8 d -> stale
    }
    rows = fs.build_rows(roster=roster, tally=tally, locks={"news-fetcher"},
                         tunnel={"news-fetcher", "image-maker"}, node_index=node_index, now=NOW)
    by = {r.agent: r for r in rows}
    assert by["news-fetcher"].status == "running"
    assert by["image-maker"].status == "stale-heartbeat"   # the "orange" case
    assert by["ghost"].status == "zombie" and by["ghost"].crew_file is None


def test_build_rows_down_when_no_process():
    rows = fs.build_rows(roster={"sleepy": "sleepy_crew.py"}, tally={}, locks=set(),
                         tunnel=set(), node_index={}, now=NOW)
    assert rows[0].status == "down"


# ── serve_tunnel_agents ─────────────────────────────────────────────────────────
def test_serve_tunnel_agents_from_agents_and_principals():
    doc = {"agents": [{"agent": "a"}], "principals": [{"type": "agent", "id": "b"}, {"type": "x", "id": "c"}]}
    assert fs.serve_tunnel_agents(doc) == {"a", "b"}


# ── build_snapshot (the real default path, collectors monkeypatched) ─────────────
def test_build_snapshot_with_cached_node_index_skips_network(monkeypatch):
    """Exercises the real build_snapshot signature the TUI uses (node_index=...). Monkeypatches the
    OS collectors; a non-None node_index must be used WITHOUT calling collect_node_index."""
    monkeypatch.setattr(fs, "collect_cmdlines", lambda: [
        "pwsh scripts/watchdog.ps1 crews/news_fetcher_crew.py",
        "python crews/news_fetcher_crew.py",
        "node aimeat.js connect serve --http",
    ])
    monkeypatch.setattr(fs, "collect_roster", lambda: {"news-fetcher": "news_fetcher_crew.py"})
    monkeypatch.setattr(fs, "collect_locks", lambda: {"news-fetcher"})
    monkeypatch.setattr(fs, "collect_serve", lambda: {"pid": 99648, "port": 52813,
                                                      "agents": [{"agent": "news-fetcher"}]})
    def _boom(_caller):  # must NOT be called when node_index is provided
        raise AssertionError("collect_node_index called despite cached node_index")
    monkeypatch.setattr(fs, "collect_node_index", _boom)

    snap = fs.build_snapshot(now=NOW, node_index={"news-fetcher": {"last_seen": "2026-06-15T17:59:00Z",
                                                                   "mode": "task-runner"}})
    assert snap.serve_pid == 99648 and snap.serve_port == 52813
    assert snap.n_connectors == 1 and snap.n_locks == 1
    assert len(snap.rows) == 1 and snap.rows[0].status == "running"

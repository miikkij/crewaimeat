"""forge.stop_crew internals — the pure pid classifier (watchdog vs daemon) and the stop flow with
the kill function stubbed (no real process is touched)."""

from crewaimeat import forge


def test_classify_crew_pids_splits_watchdog_and_daemon():
    entries = [
        (101, "pwsh -File scripts/watchdog.ps1 crews/news_fetcher_crew.py"),
        (202, "python .venv/Scripts/python.exe crews/news_fetcher_crew.py"),
        (303, "bash scripts/watchdog.sh crews/news_fetcher_crew.py"),
    ]
    wd, dae = forge._classify_crew_pids(entries)
    assert set(wd) == {101, 303} and dae == [202]


def test_stop_crew_kills_watchdog_before_daemon(monkeypatch):
    order = []
    monkeypatch.setattr(forge, "_crew_proc_entries", lambda fname: [
        (202, "python crews/news_fetcher_crew.py"),
        (101, "pwsh scripts/watchdog.ps1 crews/news_fetcher_crew.py"),
    ])
    monkeypatch.setattr(forge, "_kill_pid_tree", lambda pid: order.append(pid))
    msg = forge.stop_crew("news-fetcher")
    assert order == [101, 202]  # watchdog (101) killed FIRST, then daemon (202)
    assert "1 watchdog + 1 daemon" in msg


def test_stop_crew_nothing_running(monkeypatch):
    monkeypatch.setattr(forge, "_crew_proc_entries", lambda fname: [])
    assert "not running" in forge.stop_crew("news-fetcher")


def test_action_targets_are_plain_callables():
    """The functions the TUI's actions call MUST be plain callables, not @tool objects — a Tool
    object is not callable ('Tool' object is not callable at runtime). This catches the regression
    where start/reauth pointed at the @tool wrappers instead of their plain twins."""
    import crewaimeat.serve_guard as sg
    for fn in (forge.start_crew, forge.stop_crew, forge.recycle_crew, forge.reauth,
               forge.reconcile_fleet, sg.ensure_single_serve):
        assert callable(fn), f"{fn!r} is not callable (a @tool object leaked into the action path?)"

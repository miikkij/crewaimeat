"""fleet TUI render helpers — pure, no Textual, no terminal."""

from crewaimeat.tui import render
from crewaimeat.tui.fleet_state import AgentRow, FleetSnapshot


def _row(
    agent="x",
    status="running",
    wd=1,
    dae=1,
    lock=True,
    tunnel=True,
    age=30.0,
    crew_file="x_crew.py",
    mode="task-runner",
    last_seen="2026-06-15T17:59:00Z",
):
    return AgentRow(
        agent=agent,
        crew_file=crew_file,
        watchdog_procs=wd,
        daemon_procs=dae,
        lock=lock,
        in_tunnel=tunnel,
        last_seen=last_seen,
        last_seen_age_s=age,
        mode=mode,
        status=status,
    )


def test_format_age_buckets():
    assert render.format_age(None) == "—"
    assert render.format_age(30) == "30s"
    assert render.format_age(120) == "2m"
    assert render.format_age(7200) == "2.0h"
    assert render.format_age(180000) == "2.1d"


def test_status_markup_colors_known_and_unknown():
    assert render.status_markup("running") == "[green]running[/]"
    assert render.status_markup("DUPLICATE") == "[bold red]DUPLICATE[/]"
    assert render.status_markup("weird") == "[white]weird[/]"


def test_row_cells_shape_is_plain():
    cells = render.row_cells(_row(status="stale-heartbeat"))
    assert len(cells) == len(render.COLUMNS)
    assert cells[0] == "x" and cells[1] == "stale-heartbeat"  # status PLAIN (app colors it)
    assert cells[2] == "1/1" and cells[3] == "✓"


def test_statusbar_flags_duplicate_and_zombie():
    snap = FleetSnapshot(
        serve_pid=99648,
        serve_port=52813,
        n_watchdogs=3,
        n_connectors=1,
        n_locks=3,
        zombies=["ghost"],
        rows=[_row(status="DUPLICATE", agent="dup"), _row(status="running")],
    )
    txt = render.statusbar_text(snap)
    assert "pid 99648:52813" in txt
    assert "DUPLICATE: dup" in txt and "zombie: ghost" in txt


def test_statusbar_serve_down():
    snap = FleetSnapshot(serve_pid=None, serve_port=None, n_watchdogs=0, n_connectors=0, n_locks=0, zombies=[], rows=[])
    assert "DOWN" in render.statusbar_text(snap)


def test_detail_lines_none_and_row():
    assert render.detail_lines(None) == ["(no agent selected)"]
    lines = render.detail_lines(_row(agent="news-fetcher"))
    joined = "\n".join(lines)
    assert "news-fetcher" in joined and "last_seen:" in joined


def test_overview_lines_includes_basics_and_readme():
    j = "\n".join(render.overview_lines(_row(agent="news-fetcher"), "Hello from README"))
    assert "news-fetcher" in j and "── README ──" in j and "Hello from README" in j
    assert "(no README)" in "\n".join(render.overview_lines(_row(), None))
    assert render.overview_lines(None, None) == ["(no agent selected)"]


def test_finnish_chrome():
    assert render.columns("fi")[0] == "agentti"
    snap = FleetSnapshot(serve_pid=1, serve_port=2, n_watchdogs=1, n_connectors=0, n_locks=1, zombies=[], rows=[])
    sb = render.statusbar_text(snap, "fi")
    assert "ajossa" in sb and "vahdit" in sb and "lukot" in sb
    d = "\n".join(render.detail_lines(_row(agent="x"), "fi"))
    assert "moodi" in d and "tunneli" in d
    assert "asetukset" in "\n".join(render.meta_lines("content", ["xai:grok"], 1, 1, "fi"))

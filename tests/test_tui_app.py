"""fleet TUI app smoke test — headless via Textual's run_test, with INJECTED providers so no
process table is read and no network call is made. Proves compose + the snapshot→table/detail
render pipeline work end to end."""

import asyncio

from crewaimeat.tui.app import FleetApp
from crewaimeat.tui.fleet_state import AgentRow, FleetSnapshot
from textual.widgets import DataTable, Static


def _snap():
    rows = [
        AgentRow("news-fetcher", "news_fetcher_crew.py", 1, 1, True, True,
                 "2026-06-15T17:59:00Z", 60.0, "task-runner", "running"),
        AgentRow("image-maker", "image_maker_crew.py", 1, 1, False, True,
                 "2026-06-13T22:59:00Z", 154000.0, "task-runner", "stale-heartbeat"),
    ]
    return FleetSnapshot(serve_pid=99648, serve_port=52813, n_watchdogs=2, n_connectors=1,
                         n_locks=1, zombies=[], rows=rows)


def test_app_renders_injected_snapshot():
    async def go():
        app = FleetApp(auto_node=False, snapshot_fn=lambda ni: _snap(),
                       node_index_fn=lambda c: {})
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#agents", DataTable)
            assert table.row_count == 2
            assert tuple(c.label.plain for c in table.columns.values())[0] == "agent"
            detail = app.query_one("#detail", Static)
            # the highlighted (first) row drives the detail pane
            assert "news-fetcher" in str(detail.render())
            # statusbar reflects the snapshot
            status = app.query_one("#statusbar", Static)
            assert "running" in str(status.render())

    asyncio.run(go())

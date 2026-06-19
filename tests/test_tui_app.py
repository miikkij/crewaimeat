"""fleet TUI app smoke test — headless via Textual's run_test, with INJECTED providers so no
process table is read and no network call is made. Proves compose + the snapshot→table/detail
render pipeline work end to end."""

import asyncio

from textual.widgets import DataTable, Static, TabPane

from crewaimeat.tui.app import FleetApp
from crewaimeat.tui.fleet_state import AgentRow, FleetSnapshot


def _snap():
    rows = [
        AgentRow(
            "news-fetcher",
            "news_fetcher_crew.py",
            1,
            1,
            True,
            True,
            "2026-06-15T17:59:00Z",
            60.0,
            "task-runner",
            "running",
        ),
        AgentRow(
            "image-maker",
            "image_maker_crew.py",
            1,
            1,
            False,
            True,
            "2026-06-13T22:59:00Z",
            154000.0,
            "task-runner",
            "stale-heartbeat",
        ),
    ]
    return FleetSnapshot(
        serve_pid=99648, serve_port=52813, n_watchdogs=2, n_connectors=1, n_locks=1, zombies=[], rows=rows
    )


def test_app_renders_injected_snapshot():
    async def go():
        app = FleetApp(auto_node=False, snapshot_fn=lambda ni: _snap(), node_index_fn=lambda c: {})
        async with app.run_test() as pilot:
            await pilot.pause()
            table = app.query_one("#agents", DataTable)
            assert table.row_count == 2
            assert tuple(c.label.plain for c in table.columns.values())[0] == "agent"
            # three detail tabs exist
            assert {p.id for p in app.query(TabPane)} >= {"tab-overview", "tab-config", "tab-logs"}
            # the highlighted (first) row drives the Overview pane
            assert "news-fetcher" in str(app.query_one("#ov", Static).render())
            # Config pane shows the llm chain
            assert "llm profile" in str(app.query_one("#cfg", Static).render())
            # statusbar reflects the snapshot
            status = app.query_one("#statusbar", Static)
            assert "running" in str(status.render())

    asyncio.run(go())


def test_test_tab_exists_after_overview():
    """The Test tab is present and ordered right after Overview."""

    async def go():
        app = FleetApp(auto_node=False, snapshot_fn=lambda ni: _snap(), node_index_fn=lambda c: {})
        async with app.run_test() as pilot:
            await pilot.pause()
            ids = [p.id for p in app.query(TabPane)]
            assert "tab-test" in ids
            assert ids.index("tab-test") == ids.index("tab-overview") + 1

    asyncio.run(go())


def test_live_test_run_shows_result(monkeypatch):
    """Submitting a prompt against a running agent calls the live runner and shows its deliverable."""
    from textual.widgets import Input

    from crewaimeat.tui import test_run

    monkeypatch.setattr(
        test_run,
        "run_agent_test",
        lambda agent, prompt, **kw: {
            "ok": True,
            "task_id": "abc12345-x",
            "key": "k",
            "result": "PONG from " + agent,
            "error": None,
            "elapsed_s": 3,
        },
    )

    async def go():
        app = FleetApp(auto_node=False, snapshot_fn=lambda ni: _snap(), node_index_fn=lambda c: {})
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("t")  # switch to Test tab + focus input
            await pilot.pause()
            app.query_one("#test-input", Input).value = "ping"
            await pilot.press("enter")
            for _ in range(5):  # let the thread worker post back
                await pilot.pause()
            out = str(app.query_one("#test-out", Static).render())
            assert "PONG from news-fetcher" in out
            assert app._test_busy is False

    asyncio.run(go())


def test_test_run_guards_non_running_agent():
    """A test against a non-running agent is refused (no runner call)."""

    async def go():
        app = FleetApp(auto_node=False, snapshot_fn=lambda ni: _snap(), node_index_fn=lambda c: {})
        async with app.run_test() as pilot:
            await pilot.pause()
            app.query_one("#agents", DataTable).move_cursor(row=1)  # image-maker = stale-heartbeat
            await pilot.pause()
            app._start_test("hi")
            await pilot.pause()
            assert app._test_busy is False  # refused; nothing started

    asyncio.run(go())


def test_model_picker_opens_and_cancels(monkeypatch):
    """Pressing 'm' opens the model picker (populated from the catalogue); escape cancels."""
    from crewaimeat.tui import agent_meta
    from crewaimeat.tui.app import ModelPickScreen

    monkeypatch.setattr(
        agent_meta,
        "model_catalogue",
        lambda: [
            {
                "label": "openrouter:foo",
                "type": "openrouter",
                "id": "foo",
                "context": 131072,
                "base_url": None,
                "api_key_env": None,
                "provider": {"type": "openrouter", "name": "openrouter", "models": [{"id": "foo", "context": 131072}]},
            }
        ],
    )
    monkeypatch.setattr(agent_meta, "current_override", lambda a: None)

    async def go():
        app = FleetApp(auto_node=False, snapshot_fn=lambda ni: _snap(), node_index_fn=lambda c: {})
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("m")
            await pilot.pause()
            assert isinstance(app.screen, ModelPickScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, ModelPickScreen)

    asyncio.run(go())


def test_config_pane_shows_offers_and_contracts():
    """The Config pane surfaces the agent's offer titles and contract schemas."""

    async def go():
        rows = [
            AgentRow(
                "web-researcher",
                "web_researcher_crew.py",
                1,
                1,
                False,
                True,
                "2026-06-15T17:59:00Z",
                60.0,
                "task-runner",
                "running",
            )
        ]
        snap = FleetSnapshot(serve_pid=1, serve_port=2, n_watchdogs=1, n_connectors=1, n_locks=0, zombies=[], rows=rows)
        app = FleetApp(auto_node=False, snapshot_fn=lambda ni: snap, node_index_fn=lambda c: {})
        async with app.run_test() as pilot:
            await pilot.pause()
            cfg = str(app.query_one("#cfg", Static).render())
            assert "research" in cfg
            assert "contracts" in cfg

    asyncio.run(go())


def test_restart_key_opens_confirm_modal_and_cancels():
    """Pressing 'r' on a real crew opens the confirm modal; 'n' cancels without acting."""
    from crewaimeat.tui.app import ConfirmScreen

    async def go():
        app = FleetApp(auto_node=False, snapshot_fn=lambda ni: _snap(), node_index_fn=lambda c: {})
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("r")  # selected row 0 = news-fetcher (has a crew file)
            await pilot.pause()
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.press("n")  # cancel
            await pilot.pause()
            assert not isinstance(app.screen, ConfirmScreen)

    asyncio.run(go())

"""Startup stays interactive even when a read never returns. No fleet or network needed."""

import asyncio
import subprocess
import sys
import threading

import pytest
from test_tui_app import _snap
from textual.widgets import DataTable, Static, TabbedContent

from crewaimeat.tui import agent_meta, versions
from crewaimeat.tui.app import FleetApp
from crewaimeat.tui.intro import IntroScreen, intro_frame


async def eventually(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0.01)

    await asyncio.wait_for(wait(), timeout=5)


@pytest.fixture
def fast_details(monkeypatch):
    monkeypatch.setattr(
        FleetApp,
        "_collect_detail",
        lambda self, agent, lang: dict(readme=agent, config=agent, logs=agent, guidance=agent),
    )


def test_intro_reveals_both_words_and_fits_small_terminal():
    assert intro_frame(0, 80).plain.strip() == ""
    assert "A G E N C Y" in intro_frame(1.8, 80).plain
    assert intro_frame(0.5, 80).plain != intro_frame(1.8, 80).plain
    small = intro_frame(1.8, 24).plain
    assert "A I M E A T" in small
    assert max(map(len, small.splitlines())) <= 24


@pytest.mark.parametrize("key", ["enter", "escape", "space", None])
def test_intro_skips_or_finishes_then_opens_dashboard(key, fast_details, monkeypatch):
    if key is None:
        monkeypatch.setattr("crewaimeat.tui.intro.DURATION", 0.15)

    async def go():
        app = FleetApp(auto_node=False, snapshot_fn=lambda _: _snap())
        async with app.run_test(size=(80, 24)) as pilot:
            if key:
                assert isinstance(app.screen, IntroScreen)
                await pilot.press(key)
            await eventually(lambda: not isinstance(app.screen, IntroScreen) and app._snap is not None)
            assert app.query_one("#agents", DataTable).row_count == 2
            await pilot.press("j")
            assert app.query_one("#agents", DataTable).cursor_row == 1

    asyncio.run(go())


def test_quit_intro_never_starts_probes():
    def unexpected(_):
        pytest.fail("Quit during the intro must not start loading")

    async def go():
        app = FleetApp(snapshot_fn=unexpected, node_index_fn=unexpected)
        async with app.run_test() as pilot:
            await pilot.press("q")
            assert not app.is_running

    asyncio.run(go())


def test_slow_node_and_versions_do_not_delay_local_state_or_quit(fast_details, monkeypatch):
    release = threading.Event()
    calls = {"node": 0, "versions": 0}

    def slow(name, value):
        calls[name] += 1
        release.wait(10)
        return value

    monkeypatch.setattr(versions, "version_report", lambda: slow("versions", {}))

    async def go():
        app = FleetApp(show_intro=False, snapshot_fn=lambda _: _snap(), node_index_fn=lambda _: slow("node", {}))
        try:
            async with app.run_test() as pilot:
                await eventually(lambda: app._snap is not None and all(calls.values()))
                for _ in range(5):
                    app.action_refresh_node()
                await pilot.press("j", "c")
                assert app.query_one("#agents", DataTable).cursor_row == 1
                assert app.query_one("#detail", TabbedContent).active == "tab-config"
                assert calls == {"node": 1, "versions": 1}
                await pilot.press("q")
                assert not app.is_running
                assert not release.is_set()
        finally:
            release.set()

    asyncio.run(go())


def test_slow_model_lookup_does_not_block_navigation_or_apply_stale_details(monkeypatch):
    started, release = threading.Event(), threading.Event()
    ui_thread = threading.get_ident()
    threads = []

    def model_chain(agent):
        threads.append(threading.get_ident())
        if agent == "news-fetcher":
            started.set()
            release.wait(10)
        return agent, [agent]

    monkeypatch.setattr(agent_meta, "model_chain", model_chain)

    async def go():
        app = FleetApp(show_intro=False, auto_node=False, snapshot_fn=lambda _: _snap())
        try:
            async with app.run_test() as pilot:
                await eventually(started.is_set)
                await pilot.press("j", "f", "c")
                assert app._selected_row().agent == "image-maker"
                assert app.lang == "fi"
                assert "news-fetcher" not in str(app.query_one("#cfg", Static).render())
                release.set()
                await eventually(lambda: app._detail_data is not None)
                cfg = str(app.query_one("#cfg", Static).render())
                assert "image-maker" in cfg
                assert "news-fetcher" not in cfg
                assert all(thread != ui_thread for thread in threads)
        finally:
            release.set()

    asyncio.run(go())


def test_failed_local_probe_keeps_quit_available(fast_details):
    def broken(_):
        raise OSError("snapshot unavailable")

    async def go():
        app = FleetApp(show_intro=False, auto_node=False, snapshot_fn=broken)
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            assert "snapshot unavailable" in str(app.query_one("#statusbar", Static).render())
            await pilot.press("q")
            assert not app.is_running

    asyncio.run(go())


@pytest.mark.local_process
@pytest.mark.skipif(sys.platform == "win32", reason="The offline child guard blocks Windows asyncio's socketpair")
def test_quit_exits_python_without_waiting_for_stalled_read():
    # A run_test-only assertion misses asyncio.run's executor shutdown wait. Prove process exit.
    script = """
import asyncio
import threading
from crewaimeat.tui.app import FleetApp
started = threading.Event()
def stuck(_):
    started.set()
    threading.Event().wait(60)
async def go():
    app = FleetApp(show_intro=False, auto_node=False, snapshot_fn=stuck)
    async with app.run_test() as pilot:
        while not started.is_set():
            await asyncio.sleep(0.01)
        await pilot.press('q')
asyncio.run(go())
print('QUIT_RETURNED')
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "QUIT_RETURNED" in result.stdout

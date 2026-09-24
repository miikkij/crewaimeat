"""Health is derived from observed state; the beacon and status page never run repairs."""

import asyncio
from dataclasses import replace

import pytest
from test_tui_app import _snap
from textual.widgets import Static

from crewaimeat.tui import fleet_state, health, versions
from crewaimeat.tui.app import FleetApp
from crewaimeat.tui.health_screen import FleetHeader, HealthBeacon, HealthScreen


def healthy():
    snap = _snap()
    snap.rows = [replace(row, status="parked", parked=True, daemon_procs=0, watchdog_procs=0) for row in snap.rows]
    return snap


def current_versions():
    return {key: dict(installed="1.0", latest="1.0", update=False) for key in ("cli", "pypi")}


@pytest.fixture
def app_factory(monkeypatch):
    monkeypatch.setattr(FleetApp, "_collect_detail", lambda *a: dict(readme="", config="", logs="", guidance=""))
    monkeypatch.setattr(versions, "version_report", current_versions)
    return lambda **kw: FleetApp(
        show_intro=False, auto_node=False, snapshot_fn=lambda _: healthy(), node_index_fn=lambda _: {}, **kw
    )


@pytest.mark.parametrize("status", ["running", "running 3", "parked"])
def test_healthy_runtime_does_not_raise_alarm(status):
    snap = healthy()
    snap.rows[0].status = status
    assert health.findings(snap, current_versions(), {}) == []
    assert health.state([], set()) == "ok"
    assert health.state([], {"node"}) == "checking"


@pytest.mark.parametrize(
    ("status", "code"),
    [
        ("DUPLICATE", "duplicate"),
        ("zombie", "zombie"),
        ("stale-heartbeat", "stale"),
        ("orphan", "orphan"),
        ("attached (no runtime)", "runtime"),
        ("down (stale lock)", "lock"),
        ("down", "down"),
    ],
)
def test_runtime_problems_name_the_affected_agents_and_give_instructions(status, code):
    snap = healthy()
    snap.rows[0].status = status
    items = health.findings(snap, None, {})
    assert [item.code for item in items] == [code]
    assert items[0].subjects == ("news-fetcher",)
    assert health.state(items, {"node"}) == "alert"
    for lang in ("en", "fi"):
        report = health.report_text(items, set(), lang).plain
        assert "news-fetcher" in report
        assert "health." not in report
        assert len(report.splitlines()) >= 4


def test_missing_components_and_unavailable_reads_are_not_silently_green():
    snap = healthy()
    snap.serve_pid = None
    snap.rows = []
    items = health.findings(snap, {"cli": {}, "pypi": {}}, {"node": "connection refused"})
    assert {item.code for item in items} == {"read", "serve", "roster", "installed"}
    assert health.state(items, set()) == "alert"
    report = health.report_text(items, set(), "fi").plain
    assert "connection refused" in report
    assert "start_fleet.ps1" in report and "start_fleet.sh" in report


def test_a_saved_pid_without_a_process_is_not_healthy():
    snap = healthy()
    snap.n_connectors = 0
    assert [item.code for item in health.findings(snap, None, {})] == ["serve_process"]
    snap.n_connectors = 2
    assert [item.code for item in health.findings(snap, None, {})] == ["connectors"]


def test_updates_and_registry_outages_are_not_runtime_alarms():
    report = current_versions()
    report["cli"].update(latest="2.0", update=True)
    report["pypi"]["latest"] = None
    items = health.findings(healthy(), report, {})
    assert {item.code for item in items} == {"update", "registry"}
    assert health.state(items, set()) == "warning"


def test_report_displays_raw_error_text_without_interpreting_markup():
    report = health.report_text([health.Finding("read", ("node",), "[bold]literal[/bold]")], {"node"}, "en")
    assert "[bold]literal[/bold]" in report.plain
    assert "Still checking: server connection" in report.plain


@pytest.mark.parametrize("payload", [None, {}, {"ok": False, "agents": []}, {"data": {"error": "offline"}}])
def test_strict_node_read_distinguishes_failure_from_empty_roster(monkeypatch, payload):
    from crewaimeat import aimeat_crew

    monkeypatch.setattr(aimeat_crew, "_aimeat_call", lambda *a: payload)
    with pytest.raises(RuntimeError, match="could not be read"):
        fleet_state.collect_node_index(strict=True)
    assert fleet_state.collect_node_index() == {}
    monkeypatch.setattr(aimeat_crew, "_aimeat_call", lambda *a: {"data": {"agents": []}})
    assert fleet_state.collect_node_index(strict=True) == {}


def test_beacon_click_pulse_live_recovery_and_keyboard_status(app_factory):
    async def go():
        app = app_factory(lang="fi")
        async with app.run_test(size=(100, 34)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            beacon = app.query_one(HealthBeacon)
            assert beacon.health_state == "ok"
            assert beacon.region.right == 100 and beacon.region.y == 0
            assert beacon.content_size.height == 1  # Button's default border must not hide the light/label
            app._apply(_snap())  # one stale heartbeat
            assert beacon.health_state == "alert"
            before = beacon.has_class("lit")
            beacon._pulse()
            assert beacon.has_class("lit") != before
            await pilot.click("#health-beacon")
            await pilot.pause()
            assert isinstance(app.screen, HealthScreen)
            assert app.query_one(FleetHeader).tall is False
            content = app.screen.query_one("#health-content", Static)
            assert "image-maker" in str(content.render())
            assert "Lokit" in str(content.render())
            await pilot.press("f")
            assert "Logs" in str(content.render())
            # Fleet mutation keys do nothing while reading instructions.
            await pilot.press("s", "X", "d")
            assert isinstance(app.screen, HealthScreen)
            app._apply(healthy())
            assert beacon.health_state == "ok"
            assert not beacon.has_class("lit")
            assert "No problems found" in str(content.render())
            await pilot.press("escape", "h")
            assert isinstance(app.screen, HealthScreen)
            await pilot.press("q")
            assert not app.is_running

    asyncio.run(go())


def test_status_refresh_clears_recovered_read_error_without_closing_page(app_factory):
    async def go():
        app = app_factory()
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            app._health_errors["node"] = "temporary outage"
            app.update_health()
            await pilot.press("h")
            assert "temporary outage" in str(app.screen.query_one("#health-content", Static).render())
            await pilot.click("#health-refresh")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(app.screen, HealthScreen)
            assert not app._health_errors
            assert app.query_one(HealthBeacon).health_state == "ok"

    asyncio.run(go())

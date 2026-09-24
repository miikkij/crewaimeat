"""Clickable warning beacon and a live, read-only status screen."""

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Header, Static

from crewaimeat.tui import health, i18n


class HealthBeacon(Button):
    DEFAULT_CSS = """
    HealthBeacon {
        dock: right; width: 19; min-width: 19; height: 1;
        border: none !important; padding: 0 1; margin: 0; background: #17312f; color: #50dfd4;
    }
    HealthBeacon.alert { background: #4c1420; color: #ffaaaa; }
    HealthBeacon.alert.lit { background: #ad233b; color: white; text-style: bold; }
    HealthBeacon.warning { background: #413019; color: #eebc78; }
    HealthBeacon.checking { background: #26333f; color: #adbdca; }
    HealthBeacon:focus { text-style: bold underline; }
    """

    def __init__(self) -> None:
        super().__init__("...", id="health-beacon")
        self.health_state = "checking"

    def on_mount(self) -> None:
        self.set_interval(0.7, self._pulse)

    def _pulse(self) -> None:
        if self.health_state == "alert":
            self.toggle_class("lit")

    def set_status(self, items: list[health.Finding], pending: set[str], lang: str) -> None:
        current = health.state(items, pending)
        if current != self.health_state:
            self.set_class(False, "lit")
        self.health_state = current
        for name in ("alert", "warning", "checking", "ok"):
            self.set_class(name == current, name)
        label = i18n.t(f"health.beacon.{current}", lang)
        self.label = f"● {label}" + (f" {len(items)}" if items else "")
        self.tooltip = i18n.t("health.open", lang)


class FleetHeader(Header):
    DEFAULT_CSS = "FleetHeader HeaderClockSpace { display: none; }"

    def compose(self) -> ComposeResult:
        yield from super().compose()
        yield HealthBeacon()


class HealthScreen(ModalScreen[None]):
    CSS = """
    HealthScreen { align: center middle; background: #03080dcc; }
    #health-box { width: 92%; height: 90%; background: #09141d; border: solid #ad233b; padding: 1 2; }
    #health-top { height: 3; }
    #health-title { width: 1fr; text-style: bold; color: #ffaaaa; }
    /* Avoid tall button borders: their lower-block glyph can shift console rendering. */
    #health-box Button {
        border: none !important; height: 3; padding: 0 2;
        content-align: center middle; background: #203440; color: #dce8f0;
    }
    #health-box Button:hover { background: #304d5e; }
    #health-box Button:focus { text-style: bold underline; background: #304d5e; }
    #health-close { width: auto; min-width: 8; }
    #health-scroll { height: 1fr; }
    #health-content { height: auto; }
    #health-bottom { height: 3; margin-top: 1; }
    #health-refresh { width: auto; }
    #health-hint { width: 1fr; padding: 1 2; color: #a6b8c8; }
    """
    BINDINGS = [
        ("escape,h", "close", "Close"),
        ("r", "refresh", "Refresh"),
        ("f", "app.toggle_lang", "FI/EN"),
        ("q", "app.quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="health-box"):
            with Horizontal(id="health-top"):
                yield Static("", id="health-title")
                yield Button("Esc", id="health-close")
            with VerticalScroll(id="health-scroll"):
                yield Static("", id="health-content")
            with Horizontal(id="health-bottom"):
                yield Button("", id="health-refresh")
                yield Static("", id="health-hint")

    def on_mount(self) -> None:
        # During Mount this screen need not be app.screen yet; render it directly once.
        self.show_report(
            health.findings(self.app._snap, self.app._version_report, self.app._health_errors),
            self.app._health_pending,
            self.app.lang,
        )
        self.query_one("#health-scroll").focus()

    def show_report(self, items: list[health.Finding], pending: set[str], lang: str) -> None:
        self.query_one("#health-title", Static).update(i18n.t("health.title", lang))
        self.query_one("#health-content", Static).update(health.report_text(items, pending, lang))
        self.query_one("#health-refresh", Button).label = i18n.t("health.refresh", lang)
        self.query_one("#health-hint", Static).update(i18n.t("health.hint", lang))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == "health-close":
            self.action_close()
        elif event.button.id == "health-refresh":
            self.action_refresh()

    def action_close(self) -> None:
        self.dismiss()

    def action_refresh(self) -> None:
        self.app.action_refresh_node()

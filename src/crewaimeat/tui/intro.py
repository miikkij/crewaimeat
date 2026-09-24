"""A short, skippable ASCII reveal driven by the UI clock, never sleep()."""

from __future__ import annotations

from time import monotonic

from rich.text import Text
from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Static

from crewaimeat.tui import i18n

LOGO = (
    "    ___    ______  ____________  ______",
    "   /   |  /  _/  |/  / ____/   |/_  __/",
    "  / /| |  / // /|_/ / __/ / /| | / /   ",
    " / ___ |_/ // /  / / /___/ ___ |/ /    ",
    "/_/  |_/___/_/  /_/_____/_/  |_/_/     ",
)
DURATION = 2.1


def intro_frame(elapsed: float, width: int) -> Text:
    """Sweep cyan letters into place, then reveal the spaced AGENCY subtitle."""
    lines = LOGO if width >= 44 else ("A I M E A T",)
    span = max(map(len, lines))
    edge = int(max(0, elapsed) / 0.95 * (span + 3))
    frame = Text(justify="center", no_wrap=True)
    for line in lines:
        for x, char in enumerate(line.ljust(span)):
            if x < edge - 2:
                frame.append(char, style="bold #50dfd4")
            elif x < edge:
                frame.append("/" if char != " " else " ", style="bold white")
            else:
                frame.append(" ")
        frame.append("\n")
    subtitle = "A G E N C Y"
    visible = max(0, min(len(subtitle), int((elapsed - 0.95) * 25)))
    frame.append("\n" + subtitle[:visible].center(span), style="bold #eebc78")
    return frame


class IntroScreen(ModalScreen[None]):
    CSS = """
    IntroScreen { background: #09141d; align: center middle; }
    #intro-art { width: 100%; height: auto; text-align: center; }
    #intro-hint { dock: bottom; height: 2; text-align: center; color: #9aabb8; }
    """
    BINDINGS = [
        ("enter,escape,space", "skip", "Skip"),
        ("q", "quit_intro", "Quit"),
    ]

    def __init__(self, lang: str) -> None:
        super().__init__()
        self.lang = lang
        self._finished = False

    def compose(self) -> ComposeResult:
        yield Static("", id="intro-art")
        yield Static(i18n.t("intro.hint", self.lang), id="intro-hint")

    def on_mount(self) -> None:
        self._started = monotonic()
        self._timer = self.set_interval(1 / 30, self._tick)
        self._tick()

    def _tick(self) -> None:
        elapsed = monotonic() - self._started
        if elapsed >= DURATION:
            self.action_skip()
        else:
            self.query_one("#intro-art", Static).update(intro_frame(elapsed, self.size.width))

    def action_skip(self) -> None:
        if not self._finished:
            self._finished = True
            self._timer.stop()
            self.dismiss()

    def action_quit_intro(self) -> None:
        self.app.exit()

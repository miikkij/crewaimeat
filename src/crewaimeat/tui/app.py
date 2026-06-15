"""Read-only fleet TUI (phase 2) — a lazydocker-style monitor over fleet_state.

Layout: a status bar, an agent table (left), and a detail + log-tail pane (right). Two refresh
tiers run off the UI thread (Textual thread workers): LOCAL (~2 s — process table, locks, serve.json;
no network) and NODE (~13 s — one read-only aimeat_agents_list, cached so the fast tier never makes
a network call). Read-only: no process is started or killed here (that is the actions phase).

Run:  uv run crewaimeat-tui
"""

from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, Header, Static

from crewaimeat.tui import fleet_state as fs
from crewaimeat.tui import render


def _default_node_index(caller: str) -> dict:
    return fs.collect_node_index(caller)


def _default_snapshot(node_index: dict):
    return fs.build_snapshot(node_index=node_index)


class FleetApp(App):
    TITLE = "crewaimeat fleet"
    CSS = """
    #statusbar { height: 1; padding: 0 1; background: $panel; color: $text; }
    #agents { width: 45%; }
    #detail { width: 55%; padding: 0 1; }
    """
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("g", "refresh_node", "Refresh node"),
        ("j", "cursor_down", "Down"),
        ("k", "cursor_up", "Up"),
    ]

    def __init__(self, *, caller_agent: str = "news-fetcher", node_index_fn=None,
                 snapshot_fn=None, auto_node: bool = True) -> None:
        super().__init__()
        self.caller_agent = caller_agent
        self._node_index_fn = node_index_fn or _default_node_index
        self._snapshot_fn = snapshot_fn or _default_snapshot
        self._auto_node = auto_node
        self._node_index: dict = {}
        self._snap = None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("loading…", id="statusbar")
        with Horizontal():
            yield DataTable(id="agents", cursor_type="row", zebra_stripes=True)
            yield Static("(no agent selected)", id="detail")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#agents", DataTable)
        table.add_columns(*render.COLUMNS)
        if self._auto_node:
            self.refresh_node()                       # initial node fetch (worker)
            self.set_interval(2.0, self.refresh_local)
            self.set_interval(13.0, self.refresh_node)
        else:
            # Deterministic, synchronous initial render — used by tests (no threads, no network).
            self._apply(self._snapshot_fn(self._node_index))

    # ── refresh tiers (off the UI thread) ────────────────────────────────────
    @work(thread=True, exclusive=True, group="local")
    def refresh_local(self) -> None:
        snap = self._snapshot_fn(self._node_index)
        self.call_from_thread(self._apply, snap)

    @work(thread=True, exclusive=True, group="node")
    def refresh_node(self) -> None:
        self._node_index = self._node_index_fn(self.caller_agent)
        snap = self._snapshot_fn(self._node_index)
        self.call_from_thread(self._apply, snap)

    def action_refresh_node(self) -> None:
        self.refresh_node()

    # ── rendering ─────────────────────────────────────────────────────────────
    def _apply(self, snap) -> None:
        self._snap = snap
        self.query_one("#statusbar", Static).update(render.statusbar_text(snap))
        table = self.query_one("#agents", DataTable)
        prev = table.cursor_row
        table.clear()
        for r in snap.rows:
            cells = list(render.row_cells(r))
            cells[1] = Text.from_markup(render.status_markup(r.status))  # color the status cell
            table.add_row(*cells, key=r.agent)
        if table.row_count:
            table.move_cursor(row=min(prev or 0, table.row_count - 1))
        self._update_detail()

    def on_data_table_row_highlighted(self, _event) -> None:
        self._update_detail()

    def _update_detail(self) -> None:
        detail = self.query_one("#detail", Static)
        if not self._snap or not self._snap.rows:
            detail.update("(no agents)")
            return
        table = self.query_one("#agents", DataTable)
        idx = max(0, min(table.cursor_row or 0, len(self._snap.rows) - 1))
        row = self._snap.rows[idx]
        lines = render.detail_lines(row) + ["", "── log (tail) ──"] + self._log_tail(row.agent)
        detail.update("\n".join(lines))

    def _log_tail(self, agent: str, n: int = 12) -> list[str]:
        """Last n lines of the agent's watchdog log, if present (defensive — no log is normal)."""
        candidates = [f"{agent}.watchdog.log", f"{agent.replace('-', '_')}_crew.watchdog.log"]
        for name in candidates:
            p = Path("logs") / name
            try:
                if p.is_file():
                    return p.read_text(encoding="utf-8", errors="replace").splitlines()[-n:] or ["(empty log)"]
            except OSError:
                pass
        return ["(no log file)"]


def main() -> None:
    FleetApp().run()


if __name__ == "__main__":
    main()

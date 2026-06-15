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
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Static, TabbedContent, TabPane

from crewaimeat.tui import actions, agent_meta, fleet_state as fs, render, versions


def _default_node_index(caller: str) -> dict:
    return fs.collect_node_index(caller)


def _default_snapshot(node_index: dict):
    return fs.build_snapshot(node_index=node_index)


class ConfirmScreen(ModalScreen[bool]):
    """A small y/n modal. Every mutating action is gated behind one — no accidental restarts."""

    CSS = """
    ConfirmScreen { align: center middle; }
    #confirm-box { width: 64; height: auto; border: thick $warning; background: $surface; padding: 1 2; }
    #confirm-q { padding-bottom: 1; }
    """
    BINDINGS = [("y", "yes", "Yes"), ("n", "no", "No"), ("escape", "no", "Cancel")]

    def __init__(self, question: str) -> None:
        super().__init__()
        self._question = question

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Static(self._question, id="confirm-q")
            yield Static("[b]y[/] confirm    [b]n[/] / esc cancel")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class FleetApp(App):
    TITLE = "crewaimeat fleet"
    CSS = """
    #statusbar { height: 1; padding: 0 1; background: $panel; color: $text; }
    #agents { width: 45%; }
    #detail { width: 55%; }
    #ov, #cfg, #logs { padding: 0 1; }
    """
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("g", "refresh_node", "Refresh"),
        ("j", "cursor_down", "Down"),
        ("k", "cursor_up", "Up"),
        ("s", "start", "Start crew"),
        ("x", "stop", "Stop crew"),
        ("r", "restart", "Restart crew"),
        ("a", "reauth", "Re-auth"),
        ("S", "start_fleet", "Start fleet"),
        ("X", "stop_fleet", "Stop fleet"),
        ("R", "restart_fleet", "Restart fleet"),
        ("d", "reap", "Reap daemons"),
        ("o", "show_overview", "Overview"),
        ("c", "show_config", "Config"),
        ("l", "show_logs", "Logs"),
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
        yield Static("versions: …", id="versions")
        with Horizontal():
            yield DataTable(id="agents", cursor_type="row", zebra_stripes=True)
            with TabbedContent(id="detail"):
                with TabPane("Overview", id="tab-overview"):
                    yield Static("(no agent selected)", id="ov")
                with TabPane("Config", id="tab-config"):
                    yield Static("", id="cfg")
                with TabPane("Logs", id="tab-logs"):
                    yield Static("", id="logs")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#agents", DataTable)
        table.add_columns(*render.COLUMNS)
        if self._auto_node:
            self.refresh_node()                       # initial node fetch (worker)
            self.refresh_versions()                   # version check (worker; cached, infrequent)
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
        self.refresh_versions()

    @work(thread=True, exclusive=True, group="versions")
    def refresh_versions(self) -> None:
        vr = versions.version_report()
        self.call_from_thread(lambda: self.query_one("#versions", Static).update(render.versions_line(vr)))

    # ── navigation (vim keys; arrows work natively via DataTable) ─────────────
    def action_cursor_down(self) -> None:
        self.query_one("#agents", DataTable).action_cursor_down()

    def action_cursor_up(self) -> None:
        self.query_one("#agents", DataTable).action_cursor_up()

    # ── actions (each gated behind a confirm modal; run off the UI thread) ────
    def _selected_row(self):
        if not self._snap or not self._snap.rows:
            return None
        table = self.query_one("#agents", DataTable)
        idx = max(0, min(table.cursor_row or 0, len(self._snap.rows) - 1))
        return self._snap.rows[idx]

    def _confirm(self, question: str, label: str, fn) -> None:
        def _cb(yes: bool | None) -> None:
            if yes:
                self.notify(f"{label}…")
                self._do_action(label, fn)
        self.push_screen(ConfirmScreen(question), _cb)

    def _crew_action(self, label: str, question: str, fn) -> None:
        """Shared guard for single-crew actions: require a selected crew that has a file on disk."""
        row = self._selected_row()
        if not row or not row.crew_file:
            self.notify(f"Select a crew with a file to {label}.", severity="warning")
            return
        self._confirm(question.format(agent=row.agent), label, lambda: fn(row.agent))

    def action_start(self) -> None:
        self._crew_action("start", "Start crew '{agent}'?  (launch under the watchdog)",
                          actions.start_crew)

    def action_stop(self) -> None:
        self._crew_action("stop", "Stop crew '{agent}'?  (kill its watchdog + daemon)",
                          actions.stop_crew)

    def action_restart(self) -> None:
        self._crew_action("restart", "Restart crew '{agent}'?  (stop → relaunch)",
                          actions.restart_crew)

    def action_reauth(self) -> None:
        self._crew_action("re-auth", "Re-auth crew '{agent}'?", actions.reauth_crew)

    def action_start_fleet(self) -> None:
        self._confirm("Start the WHOLE fleet?  (ensure one serve daemon + launch every approved crew)",
                      "start-fleet", actions.start_fleet)

    def action_stop_fleet(self) -> None:
        self._confirm("STOP the whole fleet?  (kills the serve daemon + every crew)",
                      "stop-fleet", actions.stop_fleet)

    def action_restart_fleet(self) -> None:
        self._confirm("RESTART the whole fleet?  (stop everything → bring it all back up)",
                      "restart-fleet", actions.restart_fleet)

    def action_reap(self) -> None:
        self._confirm("Reap stray serve daemons (enforce exactly one)?",
                      "reap", actions.reap_serve_daemons)

    # ── detail tabs ───────────────────────────────────────────────────────────
    def action_show_overview(self) -> None:
        self.query_one("#detail", TabbedContent).active = "tab-overview"

    def action_show_config(self) -> None:
        self.query_one("#detail", TabbedContent).active = "tab-config"

    def action_show_logs(self) -> None:
        self.query_one("#detail", TabbedContent).active = "tab-logs"

    @work(thread=True, group="action")
    def _do_action(self, label: str, fn) -> None:
        try:
            msg = fn()
        except Exception as exc:  # noqa: BLE001 — surface the failure, never crash the TUI
            msg = f"{label} failed: {exc!r}"
        self.call_from_thread(self._after_action, str(msg))

    def _after_action(self, msg: str) -> None:
        self.notify(msg, timeout=10)
        self.refresh_node()  # reflect the new state

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
        ov = self.query_one("#ov", Static)
        cfg = self.query_one("#cfg", Static)
        logs = self.query_one("#logs", Static)
        if not self._snap or not self._snap.rows:
            ov.update("(no agents)")
            cfg.update("")
            logs.update("")
            return
        table = self.query_one("#agents", DataTable)
        idx = max(0, min(table.cursor_row or 0, len(self._snap.rows) - 1))
        row = self._snap.rows[idx]
        readme = None
        profile, chain, n_off, n_wf = "?", [], 0, 0
        try:  # local enrichment (README + llm chain + offers); defensive — never break the panes
            readme = agent_meta.read_readme(row.agent)
            profile, chain = agent_meta.model_chain(row.agent)
            n_off, n_wf = agent_meta.offer_summary(row.agent)
        except Exception:  # noqa: BLE001
            pass
        ov.update("\n".join(render.overview_lines(row, readme)))
        cfg.update("\n".join(render.meta_lines(profile, chain, n_off, n_wf)))
        logs.update("\n".join(self._log_tail(row.agent, n=30)))

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

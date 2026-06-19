"""Read-only fleet TUI — a lazydocker-style monitor + manager over fleet_state.

Layout: a status bar, an agent table (left), and a detail (Overview/Config/Logs tabs) + log pane
(right). Two refresh tiers run off the UI thread: LOCAL (~2 s — process table, locks, serve.json;
no network) and NODE (~13 s — one read-only aimeat_agents_list, cached). Actions (start/stop/restart
a crew or the whole fleet, reap daemons, re-auth) sit behind confirm modals. Bilingual chrome (en/fi)
— `f` toggles; default from $AIMEAT_TUI_LANG.

Run:  uv run crewaimeat-tui
"""

from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, OptionList, Static, TabbedContent, TabPane
from textual.widgets.option_list import Option

from crewaimeat.tui import actions, agent_meta, i18n, render, test_run, versions
from crewaimeat.tui import fleet_state as fs


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

    def __init__(self, question: str, hint: str = "[b]y[/] confirm    [b]n[/] / esc cancel") -> None:
        super().__init__()
        self._question = question
        self._hint = hint

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Static(self._question, id="confirm-q")
            yield Static(self._hint)

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)


class ModelPickScreen(ModalScreen[dict | None]):
    """Pick a model (or clear the override) for one agent. Dismisses with a payload describing the
    choice: {"action": "clear"} or {"action": "set", "model": <catalogue entry>}; None on cancel."""

    CSS = """
    ModelPickScreen { align: center middle; }
    #mp-box { width: 80; height: auto; max-height: 80%; border: thick $accent; background: $surface; padding: 1 2; }
    #mp-q { padding-bottom: 1; }
    #mp-list { height: auto; max-height: 20; }
    """
    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, agent: str, catalogue: list[dict], current: dict | None, lang: str) -> None:
        super().__init__()
        self._agent = agent
        self._catalogue = catalogue
        self._current = current
        self._lang = lang

    def _t(self, key: str) -> str:
        return i18n.t(key, self._lang)

    def compose(self) -> ComposeResult:
        with Vertical(id="mp-box"):
            yield Static(self._t("mp.title").format(agent=self._agent), id="mp-q")
            cur_label = (self._current or {}).get("label") if self._current else None
            opts = [Option(self._t("mp.clear"), id="__clear__")]
            for m in self._catalogue:
                mark = "● " if (cur_label and m["label"] == cur_label) else "  "
                opts.append(Option(f"{mark}{m['label']}  [dim]({m['context']//1000}k ctx)[/]", id=m["label"]))
            yield OptionList(*opts, id="mp-list")
            yield Static(self._t("mp.hint"))

    def on_mount(self) -> None:
        self.query_one("#mp-list", OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        oid = event.option.id
        if oid == "__clear__":
            self.dismiss({"action": "clear"})
            return
        for m in self._catalogue:
            if m["label"] == oid:
                self.dismiss({"action": "set", "model": m})
                return
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class FleetApp(App):
    TITLE = "crewaimeat fleet"
    CSS = """
    #statusbar { height: 1; padding: 0 1; background: $panel; color: $text; }
    #agents { width: 45%; }
    #detail { width: 55%; }
    #ov, #cfg, #logs { padding: 0 1; }
    #test-out { padding: 0 1; height: 1fr; overflow-y: auto; }
    #test-input { dock: bottom; height: 3; border: round $accent; }
    """
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("g", "refresh_node", "Refresh"),
        ("f", "toggle_lang", "FI/EN"),
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
        ("m", "pick_model", "Model"),
        ("o", "show_overview", "Overview"),
        ("t", "show_test", "Test"),
        ("c", "show_config", "Config"),
        ("l", "show_logs", "Logs"),
    ]
    TEST_TIMEOUT_S = 180
    TEST_POLL_S = 5

    def __init__(self, *, caller_agent: str = "news-fetcher", node_index_fn=None,
                 snapshot_fn=None, auto_node: bool = True, lang: str | None = None) -> None:
        super().__init__()
        self.caller_agent = caller_agent
        self._node_index_fn = node_index_fn or _default_node_index
        self._snapshot_fn = snapshot_fn or _default_snapshot
        self._auto_node = auto_node
        self._node_index: dict = {}
        self._snap = None
        self._test_busy = False
        self._test_result_shown = False   # a finished result is on screen — don't overwrite with guidance
        self._test_result_agent = None    # …for this agent (cleared when the selection moves)
        self.lang = lang or i18n.default_lang()

    def _t(self, key: str) -> str:
        return i18n.t(key, self.lang)

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("…", id="statusbar")
        yield Static(self._t("ver.loading"), id="versions")
        with Horizontal():
            yield DataTable(id="agents", cursor_type="row", zebra_stripes=True)
            with TabbedContent(id="detail"):
                with TabPane(self._t("tab.overview"), id="tab-overview"):
                    yield Static(self._t("d.none_sel"), id="ov")
                with TabPane(self._t("tab.test"), id="tab-test"):
                    yield Static(self._t("test.idle"), id="test-out")
                    yield Input(placeholder=self._t("test.placeholder"), id="test-input")
                with TabPane(self._t("tab.config"), id="tab-config"):
                    yield Static("", id="cfg")
                with TabPane(self._t("tab.logs"), id="tab-logs"):
                    yield Static("", id="logs")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#agents", DataTable).add_columns(*render.columns(self.lang))
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
        self.call_from_thread(
            lambda: self.query_one("#versions", Static).update(render.versions_line(vr, self.lang)))

    # ── language ──────────────────────────────────────────────────────────────
    def action_toggle_lang(self) -> None:
        self.lang = i18n.next_lang(self.lang)
        table = self.query_one("#agents", DataTable)
        table.clear(columns=True)
        table.add_columns(*render.columns(self.lang))
        tc = self.query_one("#detail", TabbedContent)
        for pid, key in (("tab-overview", "tab.overview"), ("tab-test", "tab.test"),
                         ("tab-config", "tab.config"), ("tab-logs", "tab.logs")):
            try:
                tc.get_tab(pid).label = self._t(key)
            except Exception:  # noqa: BLE001 — relabel is cosmetic; never crash the toggle
                pass
        try:
            self.query_one("#test-input", Input).placeholder = self._t("test.placeholder")
        except Exception:  # noqa: BLE001
            pass
        if self._snap is not None:
            self._apply(self._snap)  # re-render status bar + table + detail in the new language

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
        self.push_screen(ConfirmScreen(question, self._t("cf.yes_no")), _cb)

    def _crew_action(self, label: str, q_key: str, fn) -> None:
        """Shared guard for single-crew actions: require a selected crew that has a file on disk."""
        row = self._selected_row()
        if not row or not row.crew_file:
            self.notify(self._t("warn.select").format(action=label), severity="warning")
            return
        self._confirm(self._t(q_key).format(agent=row.agent), label, lambda: fn(row.agent))

    def action_start(self) -> None:
        self._crew_action("start", "cf.start", actions.start_crew)

    def action_stop(self) -> None:
        self._crew_action("stop", "cf.stop", actions.stop_crew)

    def action_restart(self) -> None:
        self._crew_action("restart", "cf.restart", actions.restart_crew)

    def action_reauth(self) -> None:
        self._crew_action("re-auth", "cf.reauth", actions.reauth_crew)

    def action_start_fleet(self) -> None:
        self._confirm(self._t("cf.start_fleet"), "start-fleet", actions.start_fleet)

    def action_stop_fleet(self) -> None:
        self._confirm(self._t("cf.stop_fleet"), "stop-fleet", actions.stop_fleet)

    def action_restart_fleet(self) -> None:
        self._confirm(self._t("cf.restart_fleet"), "restart-fleet", actions.restart_fleet)

    def action_reap(self) -> None:
        self._confirm(self._t("cf.reap"), "reap", actions.reap_serve_daemons)

    # ── detail tabs ───────────────────────────────────────────────────────────
    def action_show_overview(self) -> None:
        self.query_one("#detail", TabbedContent).active = "tab-overview"

    def action_show_test(self) -> None:
        self.query_one("#detail", TabbedContent).active = "tab-test"
        self.query_one("#test-input", Input).focus()

    def action_show_config(self) -> None:
        self.query_one("#detail", TabbedContent).active = "tab-config"

    def action_show_logs(self) -> None:
        self.query_one("#detail", TabbedContent).active = "tab-logs"

    # ── model override (pick a model for the selected crew, then restart it) ──
    def action_pick_model(self) -> None:
        row = self._selected_row()
        if not row or not row.crew_file:
            self.notify(self._t("warn.select").format(action="model"), severity="warning")
            return
        catalogue = agent_meta.model_catalogue()
        if not catalogue:
            self.notify(self._t("warn.no_models"), severity="warning")
            return
        current = agent_meta.current_override(row.agent)
        agent = row.agent

        def _cb(choice: dict | None) -> None:
            if not choice:
                return
            if choice.get("action") == "clear":
                self.notify(self._t("mp.cleared").format(agent=agent))
                self._do_model_change(agent, None)
            else:
                model = choice["model"]
                self.notify(self._t("mp.set").format(agent=agent, model=model["label"]))
                self._do_model_change(agent, model)

        self.push_screen(ModelPickScreen(agent, catalogue, current, self.lang), _cb)

    @work(thread=True, group="action")
    def _do_model_change(self, agent: str, model: dict | None) -> None:
        """Persist the override (or clear it) then restart the crew so it rebuilds its LLM."""
        try:
            from crewaimeat import llm
            from crewaimeat.forge import recycle_crew
            if model is None:
                llm.clear_override(agent)
            else:
                llm.save_override(agent, {"kind": "model", "label": model["label"],
                                          "provider": model["provider"]})
            msg = recycle_crew(agent)
        except Exception as exc:  # noqa: BLE001 — surface, never crash
            msg = f"model change failed: {exc!r}"
        self.call_from_thread(self._after_action, str(msg))

    # ── live agent test (create a real task, poll the deliverable) ────────────
    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "test-input":
            return
        prompt = (event.value or "").strip()
        if prompt:
            self._start_test(prompt)

    def _start_test(self, prompt: str) -> None:
        if self._test_busy:
            self.notify(self._t("test.busy"), severity="warning")
            return
        row = self._selected_row()
        if not row:
            self.notify(self._t("test.no_agent"), severity="warning")
            return
        if row.status != "running":
            self.notify(self._t("test.not_running").format(agent=row.agent), severity="warning")
            return
        self._test_busy = True
        self._test_result_shown = False
        self.query_one("#test-input", Input).value = ""
        self.query_one("#test-out", Static).update(self._t("test.running").format(agent=row.agent))
        self._run_test_worker(row.agent, prompt)

    @work(thread=True, group="test")
    def _run_test_worker(self, agent: str, prompt: str) -> None:
        head = [f"▶ {agent}: {prompt}", ""]

        def _on_update(msg: str) -> None:
            self.call_from_thread(
                lambda: self.query_one("#test-out", Static).update("\n".join(head + [msg])))

        res = test_run.run_agent_test(agent, prompt, on_update=_on_update,
                                      timeout_s=self.TEST_TIMEOUT_S, poll_s=self.TEST_POLL_S)
        self.call_from_thread(self._test_done, agent, res)

    def _test_done(self, agent: str, res: dict) -> None:
        self._test_busy = False
        self._test_result_shown = True
        self._test_result_agent = agent
        out = self.query_one("#test-out", Static)
        if res.get("ok"):
            head = self._t("test.done").format(agent=agent, secs=res.get("elapsed_s"),
                                               tid=res.get("task_id"))
            out.update(f"{head}\n\n{res.get('result') or ''}")
            self.notify(head, timeout=8)
        else:
            msg = self._t("test.failed").format(err=res.get("error"))
            out.update(msg)
            self.notify(msg, severity="warning", timeout=10)

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
        self.query_one("#statusbar", Static).update(render.statusbar_text(snap, self.lang))
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
            ov.update(self._t("d.none"))
            cfg.update("")
            logs.update("")
            return
        table = self.query_one("#agents", DataTable)
        idx = max(0, min(table.cursor_row or 0, len(self._snap.rows) - 1))
        row = self._snap.rows[idx]
        readme = None
        profile, chain, n_off, n_wf = "?", [], 0, 0
        override = offers = contracts = tags = caps = workflows = None
        try:  # local enrichment (README + llm chain + offers + identity); defensive — never break panes
            readme = agent_meta.read_readme(row.agent)
            profile, chain = agent_meta.model_chain(row.agent)
            n_off, n_wf = agent_meta.offer_summary(row.agent)
            override = agent_meta.current_override(row.agent)
            offers = agent_meta.offers_detail(row.agent)
            contracts = agent_meta.contracts_for(row.agent)
            tags, caps = agent_meta.identity(row.agent)
            workflows = agent_meta.workflows_for(row.agent)
        except Exception:  # noqa: BLE001
            pass
        ov.update("\n".join(render.overview_lines(row, readme, self.lang)))
        cfg.update("\n".join(render.meta_lines(
            profile, chain, n_off, n_wf, self.lang, override=override, offers=offers,
            contracts=contracts, tags=tags, capabilities=caps, workflows=workflows)))
        logs.update("\n".join(self._log_tail(row.agent, n=30)))
        # Test pane: per-agent "how to task me" guidance — but never clobber a running test or a
        # finished result that still belongs to the highlighted agent.
        if not self._test_busy and not (self._test_result_shown and self._test_result_agent == row.agent):
            self._test_result_shown = False
            self.query_one("#test-out", Static).update(self._test_guidance(row.agent))

    def _test_guidance(self, agent: str) -> str:
        """Idle Test-pane text: the agent's own 'How to task me' hint (so a contract agent that wants
        a request record, not a free-text brief, says so) plus the generic instructions."""
        try:
            how = agent_meta.how_to_task(agent)
        except Exception:  # noqa: BLE001
            how = None
        head = f"[b]{agent}[/] — {how}\n\n" if how else ""
        return head + self._t("test.idle")

    def _log_tail(self, agent: str, n: int = 12) -> list[str]:
        """Last n lines of the agent's watchdog log, if present (defensive — no log is normal)."""
        candidates = [f"{agent}.watchdog.log", f"{agent.replace('-', '_')}_crew.watchdog.log"]
        for name in candidates:
            p = Path("logs") / name
            try:
                if p.is_file():
                    return p.read_text(encoding="utf-8", errors="replace").splitlines()[-n:] or [self._t("log.empty")]
            except OSError:
                pass
        return [self._t("log.none")]


def main() -> None:
    FleetApp().run()


if __name__ == "__main__":
    main()

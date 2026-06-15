"""Pure presentation helpers for the fleet TUI — formatting + styling only, NO Textual import, so
they unit-test without a terminal. app.py composes these into widgets."""

from __future__ import annotations

from crewaimeat.tui.fleet_state import AgentRow, FleetSnapshot

COLUMNS = ("agent", "status", "wd/dae", "lock", "tun", "last_seen")

_STATUS_STYLE = {
    "running": "green",
    "stale-heartbeat": "yellow",
    "orphan": "dark_orange",
    "DUPLICATE": "bold red",
    "zombie": "magenta",
    "down": "dim",
    "down (stale lock)": "dim",
}


def status_style(status: str) -> str:
    return _STATUS_STYLE.get(status, "white")


def status_markup(status: str) -> str:
    return f"[{status_style(status)}]{status}[/]"


def format_age(age_s: float | None) -> str:
    """Compact human age: seconds < 90, then minutes < 90 min, then hours < 2 d, then days."""
    if age_s is None:
        return "—"
    if age_s < 90:
        return f"{int(age_s)}s"
    if age_s < 5400:
        return f"{int(age_s / 60)}m"
    if age_s < 172800:
        return f"{age_s / 3600:.1f}h"
    return f"{age_s / 86400:.1f}d"


def row_cells(r: AgentRow) -> tuple[str, ...]:
    """One table row, all PLAIN strings (status colored separately by the app via status_markup —
    keeping these markup-free makes them trivially testable)."""
    return (
        r.agent,
        r.status,
        f"{r.watchdog_procs}/{r.daemon_procs}",
        "✓" if r.lock else "·",
        "✓" if r.in_tunnel else "·",
        format_age(r.last_seen_age_s),
    )


def statusbar_text(snap: FleetSnapshot) -> str:
    serve = f"pid {snap.serve_pid}:{snap.serve_port}" if snap.serve_pid else "[bold red]DOWN[/]"
    n_run = sum(1 for r in snap.rows if r.status == "running")
    n_stale = sum(1 for r in snap.rows if r.status == "stale-heartbeat")
    warn = ""
    dups = [r.agent for r in snap.rows if r.status == "DUPLICATE"]
    if dups:
        warn += f"  [bold red]DUPLICATE: {', '.join(dups)}[/]"
    if snap.zombies:
        warn += f"  [magenta]zombie: {', '.join(snap.zombies)}[/]"
    return (f"serve {serve} · {snap.n_watchdogs} watchdogs · {snap.n_locks} locks · "
            f"[green]{n_run} running[/] · [yellow]{n_stale} stale[/]{warn}")


def detail_lines(r: AgentRow | None) -> list[str]:
    if r is None:
        return ["(no agent selected)"]
    return [
        f"agent:      {r.agent}",
        f"status:     {status_markup(r.status)}",
        f"crew file:  {r.crew_file or '(none — zombie)'}",
        f"mode:       {r.mode or '—'}",
        f"watchdog:   {r.watchdog_procs}    daemon: {r.daemon_procs}",
        f"lock:       {'yes' if r.lock else 'no'}    tunnel: {'yes' if r.in_tunnel else 'no'}",
        f"last_seen:  {r.last_seen or '—'}  ({format_age(r.last_seen_age_s)} ago)",
    ]

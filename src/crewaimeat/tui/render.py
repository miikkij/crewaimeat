"""Pure presentation helpers for the fleet TUI — formatting + styling only, NO Textual import, so
they unit-test without a terminal. app.py composes these into widgets. UI chrome is translated via
i18n (`lang` arg, default 'en'); agent names / statuses / log text are data and stay as-is."""

from __future__ import annotations

from crewaimeat.tui.fleet_state import AgentRow, FleetSnapshot
from crewaimeat.tui.i18n import t

_STATUS_STYLE = {
    "running": "green",
    "stale-heartbeat": "yellow",
    "orphan": "dark_orange",
    "DUPLICATE": "bold red",
    "zombie": "magenta",
    "down": "dim",
    "down (stale lock)": "dim",
}

_COL_KEYS = ("col.agent", "col.status", "col.wd_dae", "col.lock", "col.tun", "col.last_seen")
COLUMNS = ("agent", "status", "wd/dae", "lock", "tun", "last_seen")  # english default (tests/back-compat)


def columns(lang: str = "en") -> tuple[str, ...]:
    return tuple(t(k, lang) for k in _COL_KEYS)


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
    """One table row, all PLAIN strings (status colored separately by the app via status_markup)."""
    return (
        r.agent,
        r.status,
        f"{r.watchdog_procs}/{r.daemon_procs}",
        "✓" if r.lock else "·",
        "✓" if r.in_tunnel else "·",
        format_age(r.last_seen_age_s),
    )


def statusbar_text(snap: FleetSnapshot, lang: str = "en") -> str:
    serve = f"pid {snap.serve_pid}:{snap.serve_port}" if snap.serve_pid else f"[bold red]{t('sb.down', lang)}[/]"
    n_run = sum(1 for r in snap.rows if r.status == "running")
    n_stale = sum(1 for r in snap.rows if r.status == "stale-heartbeat")
    warn = ""
    dups = [r.agent for r in snap.rows if r.status == "DUPLICATE"]
    if dups:
        warn += f"  [bold red]DUPLICATE: {', '.join(dups)}[/]"
    if snap.zombies:
        warn += f"  [magenta]zombie: {', '.join(snap.zombies)}[/]"
    return (f"serve {serve} · {snap.n_watchdogs} {t('sb.watchdogs', lang)} · {snap.n_locks} {t('sb.locks', lang)} · "
            f"[green]{n_run} {t('sb.running', lang)}[/] · [yellow]{n_stale} {t('sb.stale', lang)}[/]{warn}")


def detail_lines(r: AgentRow | None, lang: str = "en") -> list[str]:
    if r is None:
        return [t("d.none_sel", lang)]
    return [
        f"{t('d.agent', lang)}:      {r.agent}",
        f"{t('d.status', lang)}:     {status_markup(r.status)}",
        f"{t('d.crew_file', lang)}:  {r.crew_file or t('sec.no_readme', lang)}",
        f"{t('d.mode', lang)}:       {r.mode or '—'}",
        f"{t('d.watchdog', lang)}:   {r.watchdog_procs}    {t('d.daemon', lang)}: {r.daemon_procs}",
        f"{t('d.lock', lang)}:       {'yes' if r.lock else 'no'}    {t('d.tunnel', lang)}: {'yes' if r.in_tunnel else 'no'}",
        f"{t('d.last_seen', lang)}:  {r.last_seen or '—'}  ({format_age(r.last_seen_age_s)} {t('d.ago', lang)})",
    ]


def overview_lines(r: AgentRow | None, readme: str | None, lang: str = "en") -> list[str]:
    """The Overview tab: basic status info + the agent's README (or a placeholder)."""
    lines = detail_lines(r, lang)
    if r is None:
        return lines
    lines += ["", f"── {t('sec.readme', lang)} ──", ""]
    lines += readme.splitlines() if readme else [t("sec.no_readme", lang)]
    return lines


def meta_lines(profile: str, model_labels: list[str], n_offers: int, n_wf: int, lang: str = "en") -> list[str]:
    """The Config tab: llm profile + ordered model chain + offer/workflow-compat counts."""
    chain = "\n             ".join(model_labels) if model_labels else "—"
    return [
        "",
        f"── {t('sec.config', lang)} ──",
        f"{t('cfg.profile', lang)}: {profile}",
        f"{t('cfg.chain', lang)}: {chain}",
        f"{t('cfg.offers', lang)}:      {n_offers}  ([green]{n_wf}[/] {t('cfg.wf_compat', lang)})",
    ]


def versions_line(vr: dict, lang: str = "en") -> str:
    """One-line version summary with an update flag per component."""
    if not vr:
        return t("ver.loading", lang)

    def _fmt(part: dict) -> str:
        inst = part.get("installed") or "?"
        if part.get("update"):
            return f"{inst} [yellow](→ {part.get('latest')})[/]"
        return inst

    return f"aimeat-crewai {_fmt(vr.get('pypi', {}))}  ·  aimeat-cli {_fmt(vr.get('cli', {}))}"

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
    """One table row, all PLAIN strings (status colored separately by the app via status_markup). A
    host-threaded agent has no per-crew process, so its wd/dae cell shows 'host' instead of '0/0'."""
    return (
        r.agent,
        r.status,
        "host" if r.hosted else f"{r.watchdog_procs}/{r.daemon_procs}",
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
    n_hosted = sum(1 for r in snap.rows if r.hosted)
    host = f"  [cyan]host pid {snap.host_pid} ({n_hosted} {t('sb.threaded', lang)})[/]" if snap.host_pid else ""
    return (
        f"serve {serve} · {snap.n_watchdogs} {t('sb.watchdogs', lang)} · {snap.n_locks} {t('sb.locks', lang)} · "
        f"[green]{n_run} {t('sb.running', lang)}[/] · [yellow]{n_stale} {t('sb.stale', lang)}[/]{warn}{host}"
    )


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


def meta_lines(
    profile: str,
    model_labels: list[str],
    n_offers: int,
    n_wf: int,
    lang: str = "en",
    *,
    override: dict | None = None,
    offers: list | None = None,
    contracts: list | None = None,
    tags: list | None = None,
    capabilities: dict | None = None,
    workflows: list | None = None,
) -> list[str]:
    """The Config tab: llm profile + ordered model chain + offer/workflow-compat counts, and (when
    supplied) the agent's pinned override, offer titles, contract schemas, capabilities and the
    workflows it has a step in. The extra sections are keyword-only so the basic 5-arg call stays."""
    chain = "\n             ".join(model_labels) if model_labels else "—"
    lines = [
        "",
        f"── {t('sec.config', lang)} ──",
        f"{t('cfg.profile', lang)}: {profile}",
        f"{t('cfg.chain', lang)}: {chain}",
    ]
    if override:
        if override.get("kind") == "model":
            pin = override.get("label", "model")
        elif override.get("kind") == "profile":
            pin = f"{t('cfg.profile', lang)} → {override.get('profile')}"
        else:
            pin = str(override)
        lines.append(f"{t('cfg.override', lang)}: [yellow]{pin}[/]  ({t('cfg.override_hint', lang)})")
    lines.append(f"{t('cfg.offers', lang)}:      {n_offers}  ([green]{n_wf}[/] {t('cfg.wf_compat', lang)})")

    if offers:
        for oid, title in offers:
            lines.append(f"  · [b]{oid}[/] — {title}")
    if tags:
        lines += ["", f"── {t('sec.identity', lang)} ──", f"{t('cfg.tags', lang)}: {', '.join(tags)}"]
        for dim in ("technical", "domain", "languages"):
            vals = (capabilities or {}).get(dim) or []
            disp = [v.get("name") if isinstance(v, dict) else str(v) for v in vals]
            if disp:
                lines.append(f"{t('cfg.cap_' + dim, lang)}: {', '.join(disp)}")
    elif capabilities:
        lines += ["", f"── {t('sec.identity', lang)} ──"]
        for dim in ("technical", "domain", "languages"):
            vals = capabilities.get(dim) or []
            disp = [v.get("name") if isinstance(v, dict) else str(v) for v in vals]
            if disp:
                lines.append(f"{t('cfg.cap_' + dim, lang)}: {', '.join(disp)}")
    if contracts:
        lines += ["", f"── {t('sec.contracts', lang)} ──"]
        for c in contracts:
            lines.append(f"  [b]{c['id']}[/]")
            for sp in c.get("spaces") or []:
                fields = ", ".join(sp.get("fields") or []) or "—"
                lines.append(f"    {sp['space']} ({sp['mode']}): {fields}")
    if workflows:
        lines += ["", f"── {t('sec.workflows', lang)} ──"]
        for wid, steps in workflows:
            lines.append(f"  [b]{wid}[/]: {', '.join(steps)}")
    return lines


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

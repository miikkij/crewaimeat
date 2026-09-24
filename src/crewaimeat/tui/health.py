"""Health findings derived from the monitor's existing reads. No extra probes or actions."""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text

from crewaimeat.tui import i18n
from crewaimeat.tui.fleet_state import FleetSnapshot


@dataclass(frozen=True)
class Finding:
    code: str
    subjects: tuple[str, ...] = ()
    detail: str = ""
    warning: bool = False


def findings(snapshot: FleetSnapshot | None, versions: dict | None, errors: dict[str, str]) -> list[Finding]:
    result = [Finding("read", (source,), error) for source, error in sorted(errors.items())]
    if snapshot is not None:
        if not snapshot.serve_pid:
            result.append(Finding("serve"))
        elif snapshot.n_connectors == 0:
            result.append(Finding("serve_process"))
        if snapshot.n_connectors > 1:
            result.append(Finding("connectors", detail=str(snapshot.n_connectors)))
        if not snapshot.rows:
            result.append(Finding("roster"))
        for status, code in (
            ("DUPLICATE", "duplicate"),
            ("zombie", "zombie"),
            ("stale-heartbeat", "stale"),
            ("orphan", "orphan"),
            ("attached (no runtime)", "runtime"),
            ("down (stale lock)", "lock"),
            ("down", "down"),
        ):
            agents = tuple(row.agent for row in snapshot.rows if row.status == status)
            if agents:
                result.append(Finding(code, agents))
        # A valid parked spawn agent is healthy. Running N workers is healthy too.
    if versions is not None:
        for key, package in (("cli", "aimeat CLI"), ("pypi", "aimeat-crewai")):
            info = versions.get(key) or {}
            if not info.get("installed"):
                result.append(Finding("installed", (package,)))
            elif not info.get("latest"):
                result.append(Finding("registry", (package,), warning=True))
            elif info.get("update"):
                result.append(Finding("update", (package,), f"{info['installed']} -> {info['latest']}", warning=True))
    return result


def state(items: list[Finding], pending: set[str]) -> str:
    if any(not item.warning for item in items):
        return "alert"
    if items:
        return "warning"
    return "checking" if pending else "ok"


def report_text(items: list[Finding], pending: set[str], lang: str) -> Text:
    """Use literal Text for agent names/errors so an error cannot inject terminal markup."""

    def t(key: str) -> str:
        return i18n.t(f"health.{key}", lang)

    text = Text()
    text.append(t("scope") + "\n\n", style="#a6b8c8")
    if pending:
        sources = ", ".join(t(f"source.{source}") for source in sorted(pending))
        text.append(t("pending").format(sources=sources) + "\n\n", style="#eebc78")
    if not items:
        text.append(t("checking") if pending else t("clear"), style="bold #50dfd4")
    for index, item in enumerate(items, 1):
        color = "#eebc78" if item.warning else "#ff6666"
        text.append(f"{index:02d}  {t(item.code + '.title')}\n", style=f"bold {color}")
        if item.subjects:
            subjects = [t(f"source.{s}") for s in item.subjects] if item.code == "read" else item.subjects
            text.append("    " + ", ".join(subjects) + "\n", style="bold")
        if item.detail:
            text.append("    " + item.detail + "\n")
        text.append(t(item.code + ".help") + "\n\n")
    return text

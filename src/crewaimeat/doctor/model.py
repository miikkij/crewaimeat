"""Findings, the report, and the baseline ratchet.

A Finding is one violated rule about one subject. Rules have STABLE ids (`registry.serve.ghost`) so a
baseline can name them and a dashboard can count them over time; the human-readable message may change
without invalidating the ratchet.

The ratchet is what makes a strict checker adoptable on a live codebase. Turning every existing
violation into a hard failure means either a two-week freeze or (much more likely) someone disables the
checker. Instead: `doctor --accept-baseline` records today's violations, they stop failing the build,
and NOTHING NEW may be added. The baseline only ever shrinks — a recorded finding that no longer fires
is reported as `baseline.stale` and must be removed, so the file cannot quietly become a permanent
amnesty for problems that were already fixed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

ERROR = "error"
WARN = "warn"

BASELINE_FILE = "doctor-baseline.json"


@dataclass(frozen=True)
class Finding:
    """One rule, one subject. `key` is what the baseline matches on."""

    rule: str  # stable dotted id, e.g. "registry.identity.missing"
    severity: str  # ERROR | WARN
    subject: str  # agent name, or "path/to/file.py:120"
    message: str  # what is wrong, in one line
    fix: str = ""  # what to do about it

    @property
    def key(self) -> str:
        return f"{self.rule}::{self.subject}"


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # non-findings worth printing (counts, skips)
    lenses_run: list[str] = field(default_factory=list)
    lenses_skipped: dict[str, str] = field(default_factory=dict)  # lens -> why

    def add(self, *findings: Finding) -> None:
        self.findings.extend(findings)

    def note(self, text: str) -> None:
        self.notes.append(text)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == WARN]

    def by_rule(self) -> dict[str, list[Finding]]:
        out: dict[str, list[Finding]] = {}
        for f in self.findings:
            out.setdefault(f.rule, []).append(f)
        return out


def load_baseline(root: Path) -> set[str]:
    """Accepted finding keys. A missing file means an empty baseline (everything is enforced)."""
    p = root / BASELINE_FILE
    if not p.exists():
        return set()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(
            f"{BASELINE_FILE} is unreadable ({exc}). Fix or delete it — never ignore it silently."
        ) from exc
    return {str(k) for k in data.get("accepted", [])}


def write_baseline(root: Path, findings: list[Finding], *, note: str = "") -> Path:
    """Record today's findings as accepted. Sorted for a reviewable diff."""
    p = root / BASELINE_FILE
    payload = {
        "_comment": (
            "Findings accepted as pre-existing by `crewaimeat doctor --accept-baseline`. This file may "
            "only SHRINK: a new violation of the same rule still fails, and an entry that no longer "
            "fires is reported as baseline.stale so it gets removed. Do not hand-add entries."
        ),
        "note": note,
        "accepted": sorted({f.key for f in findings}),
    }
    p.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return p


def apply_baseline(report: Report, accepted: set[str]) -> tuple[Report, list[str]]:
    """Split a report against the baseline.

    Returns (enforced_report, stale_keys) where `enforced_report` drops accepted findings and
    `stale_keys` are baseline entries that no longer fire — those are reported so the file shrinks.
    """
    live_keys = {f.key for f in report.findings}
    enforced = Report(
        findings=[f for f in report.findings if f.key not in accepted],
        notes=list(report.notes),
        lenses_run=list(report.lenses_run),
        lenses_skipped=dict(report.lenses_skipped),
    )
    stale = sorted(accepted - live_keys)
    return enforced, stale

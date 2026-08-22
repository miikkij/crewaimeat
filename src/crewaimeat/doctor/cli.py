"""`crewaimeat doctor` — the reconciliation this repo used to do by hand, as one command.

    crewaimeat doctor                 # lenses 1+2 (offline, ~1s), warnings shown, exit 0 unless errors
    crewaimeat doctor --live          # + ask the node (needs the fleet attached)
    crewaimeat doctor --strict        # warnings fail too — what CI runs
    crewaimeat doctor --accept-baseline   # record today's findings as pre-existing (the ratchet)
    crewaimeat doctor --json          # machine-readable, for a dashboard or an alerting hook

Exit codes: 0 clean · 1 findings that fail · 2 the run itself could not complete.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import conformance, inventory, liveness, registries
from .model import ERROR, Report, apply_baseline, load_baseline, write_baseline

_WARN, _ERR = "!! ", "XX "


def _colour(text: str, code: str, enabled: bool) -> str:
    return f"\033[{code}m{text}\033[0m" if enabled else text


def run(root: Path, *, live: bool = False) -> Report:
    report = Report()
    inv = inventory.gather(root)
    report.lenses_run.append("registries")
    registries.check(inv, report)
    report.lenses_run.append("conformance")
    conformance.check(root, report)
    if live:
        liveness.check(inv, report)
    else:
        report.lenses_skipped["live"] = "not requested (pass --live)"
    return report


def _render(report: Report, stale: list[str], *, strict: bool, colour: bool) -> str:
    out: list[str] = []
    for note in report.notes:
        out.append(f"    {note}")
    if report.notes:
        out.append("")
    grouped = report.by_rule()
    for rule in sorted(grouped, key=lambda r: (grouped[r][0].severity != ERROR, r)):
        findings = grouped[rule]
        sev = findings[0].severity
        mark = _colour(_ERR if sev == ERROR else _WARN, "31" if sev == ERROR else "33", colour)
        out.append(f"{mark}{rule}  ({len(findings)})")
        out.append(f"      {findings[0].message}")
        if findings[0].fix:
            out.append(f"      fix: {findings[0].fix}")
        shown = findings[:12]
        for f in shown:
            out.append(f"        · {f.subject}")
        if len(findings) > len(shown):
            out.append(f"        · … and {len(findings) - len(shown)} more")
        out.append("")
    if stale:
        out.append(f"{_colour(_WARN, '33', colour)}baseline.stale  ({len(stale)})")
        out.append("      recorded in doctor-baseline.json but no longer firing — the baseline must shrink")
        out.append("      fix: re-run with --accept-baseline to drop them")
        for k in stale[:12]:
            out.append(f"        · {k}")
        out.append("")
    for lens, why in sorted(report.lenses_skipped.items()):
        out.append(f"    lens '{lens}' SKIPPED — {why}")
    n_err, n_warn = len(report.errors), len(report.warnings)
    verdict = "FAIL" if (n_err or (strict and n_warn)) else "PASS"
    tone = "31" if verdict == "FAIL" else "32"
    out.append("")
    out.append(_colour(f"{verdict}  {n_err} error(s), {n_warn} warning(s)", tone, colour))
    if verdict == "PASS" and not n_err and not n_warn:
        out.append("    every registry agrees and every route is sanctioned.")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="crewaimeat doctor", description=__doc__.splitlines()[0])
    ap.add_argument("--live", action="store_true", help="also reconcile against the node (needs the fleet attached)")
    ap.add_argument("--strict", action="store_true", help="warnings fail the run too (CI)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--accept-baseline", action="store_true", help="record today's findings as pre-existing")
    ap.add_argument("--no-baseline", action="store_true", help="ignore doctor-baseline.json and report everything")
    ap.add_argument("--root", default=".", help="repo root (default: cwd)")
    args = ap.parse_args(argv)

    root = Path(args.root).resolve()
    if not (root / "crews").is_dir():
        print(f"doctor: {root} does not look like a crewaimeat repo (no crews/)", file=sys.stderr)
        return 2

    raw = run(root, live=args.live)

    if args.accept_baseline:
        path = write_baseline(root, raw.findings, note="recorded by `crewaimeat doctor --accept-baseline`")
        print(f"recorded {len(raw.findings)} finding(s) as accepted in {path.name}.")
        print("They no longer fail the build; a NEW violation of the same rule still does.")
        return 0

    accepted = set() if args.no_baseline else load_baseline(root)
    report, stale = apply_baseline(raw, accepted)

    if args.json:
        print(
            json.dumps(
                {
                    "verdict": "fail" if (report.errors or (args.strict and report.warnings)) else "pass",
                    "errors": len(report.errors),
                    "warnings": len(report.warnings),
                    "baselined": len(accepted),
                    "stale_baseline": stale,
                    "lenses_run": report.lenses_run,
                    "lenses_skipped": report.lenses_skipped,
                    "findings": [f.__dict__ | {"key": f.key} for f in report.findings],
                    "notes": report.notes,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        colour = sys.stdout.isatty()
        if accepted:
            print(f"    baseline: {len(accepted)} pre-existing finding(s) accepted (doctor-baseline.json)")
        print(_render(report, stale, strict=args.strict, colour=colour))

    failed = bool(report.errors) or (args.strict and bool(report.warnings)) or (args.strict and bool(stale))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

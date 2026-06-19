"""Version check for the TUI — what's installed vs the latest available, for the two AIMEAT pieces:
the `aimeat-crewai` PyPI package (this Python lib) and the `aimeat` npm CLI (the `connect serve`
binary). Network/subprocess calls — the app runs this in a thread worker, caches the result, and
refreshes only on startup + the manual key (never in the tight loop).

Every probe is best-effort: any failure yields None ('unknown'), never an exception — a missing npm
or no internet must not break the TUI.
"""

from __future__ import annotations

import re
import subprocess


def installed_pypi(pkg: str = "aimeat-crewai") -> str | None:
    try:
        from importlib.metadata import version

        return version(pkg)
    except Exception:  # noqa: BLE001
        return None


def latest_pypi(pkg: str = "aimeat-crewai") -> str | None:
    try:
        import requests

        r = requests.get(f"https://pypi.org/pypi/{pkg}/json", timeout=8)
        return r.json()["info"]["version"] if r.status_code == 200 else None
    except Exception:  # noqa: BLE001
        return None


def cli_version() -> str | None:
    """Installed `aimeat` CLI version via `aimeat --version` (shell on Windows so .cmd resolves)."""
    try:
        out = subprocess.run("aimeat --version", shell=True, capture_output=True, text=True, timeout=15).stdout
        m = re.search(r"\d+\.\d+\.\d+", out)
        return m.group(0) if m else None
    except Exception:  # noqa: BLE001
        return None


def latest_npm(pkg: str = "aimeat") -> str | None:
    try:
        import requests

        r = requests.get(f"https://registry.npmjs.org/{pkg}/latest", timeout=8)
        return r.json().get("version") if r.status_code == 200 else None
    except Exception:  # noqa: BLE001
        return None


def is_update(installed: str | None, latest: str | None) -> bool:
    """True when a strictly-newer version is available. Uses packaging if present, else string !=."""
    if not installed or not latest:
        return False
    try:
        from packaging.version import parse

        return parse(latest) > parse(installed)
    except Exception:  # noqa: BLE001
        return installed != latest


def version_report() -> dict:
    pi, pl = installed_pypi(), latest_pypi()
    ci, cl = cli_version(), latest_npm()
    return {
        "pypi": {"installed": pi, "latest": pl, "update": is_update(pi, pl)},
        "cli": {"installed": ci, "latest": cl, "update": is_update(ci, cl)},
    }

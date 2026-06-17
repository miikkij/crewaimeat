"""Single source of truth for the AIMEAT connector home (the directory holding serve.json, tokens/,
agents/, config.yaml, and the serve daemon).

Resolution (mirrors the connector's own precedence):
  1. aimeat-crewai >= 0.6.0 ships `aimeat_crewai.paths.aimeat_home()` — prefer it so we never drift
     from the package (AIMEAT_HOME env wins, else <cwd>/.aimeat).
  2. Fallback for 0.5.0 (no paths module): AIMEAT_HOME env wins, else ~/.aimeat (the 0.5.0 default).

The fleet PINS AIMEAT_HOME in every entrypoint (start_fleet / serve_watchdog / watchdog → inherited by
crew-forge → reconcile_fleet → every detached crew), so the env is set and BOTH branches agree on the
pinned path regardless of the process's cwd. The fallback's base only matters for a bare standalone run
with no env set. Use this everywhere instead of duplicating `os.environ.get("AIMEAT_HOME") or ~/.aimeat`.
"""

from __future__ import annotations

import os
from pathlib import Path


def aimeat_home() -> Path:
    """The resolved AIMEAT connector home directory."""
    try:
        from aimeat_crewai.paths import aimeat_home as _pkg_home  # 0.6.0+
        return Path(_pkg_home())
    except Exception:  # noqa: BLE001 — 0.5.0 has no paths module; fall back to the env/legacy default
        env = os.environ.get("AIMEAT_HOME")
        return Path(env) if env else (Path.home() / ".aimeat")


def serve_json_path() -> Path:
    """Path of the serve daemon's discovery file (`<AIMEAT_HOME>/serve.json`)."""
    return aimeat_home() / "serve.json"

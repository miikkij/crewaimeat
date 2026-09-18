"""Where agency 2.0 keeps things.

DATA DIR is the working directory every child process runs in, because two things the scaffold already
does are relative to it: the runtime reads `<cwd>/.env` (the OpenRouter key), and an agent's FIRST
definition is staged at `<cwd>/crew_defs/<name>.json` for the agent to publish itself
(`json_agent.seed_from_staged`, which resolves against `forge._project_root()` = cwd). The Tauri shell
starts the cockpit in the runtime folder, so that folder is the data dir there; a dev run can point it
anywhere with AIMEAT_AGENCY_DATA.

The connector home stays `crewaimeat._home.aimeat_home()` (AIMEAT_HOME wins), never re-derived here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

ENV_KEY = "OPENROUTER_API_KEY"


def data_dir() -> Path:
    d = Path(os.environ.get("AIMEAT_AGENCY_DATA") or os.getcwd()).resolve()
    d.mkdir(parents=True, exist_ok=True)
    return d


def state_dir() -> Path:
    """agency 2.0's own files (instances, agents, pids, logs) — beside the data, never in the connector home."""
    d = data_dir() / ".agency2"
    d.mkdir(parents=True, exist_ok=True)
    return d


def logs_dir() -> Path:
    d = state_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def aimeat_home() -> Path:
    from crewaimeat._home import aimeat_home as _home

    return Path(_home())


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def write_json(path: Path, value: Any) -> None:
    """Atomic write: a crash mid-write must never leave a half file that the next start cannot read."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ── the OpenRouter key: <data>/.env, the file the runtime already loads ─────────────────────────────


def _env_file() -> Path:
    return data_dir() / ".env"


def get_env_key(var: str = ENV_KEY) -> str:
    """The saved value (process env first — the shell or a dev may set it), or ''."""
    if os.environ.get(var, "").strip():
        return os.environ[var].strip()
    f = _env_file()
    if f.is_file():
        for ln in f.read_text(encoding="utf-8").splitlines():
            if ln.startswith(f"{var}="):
                return ln.split("=", 1)[1].strip()
    return ""


def set_env_key(value: str, var: str = ENV_KEY) -> None:
    f = _env_file()
    lines = f.read_text(encoding="utf-8").splitlines() if f.is_file() else []
    lines = [ln for ln in lines if not ln.startswith(f"{var}=")]
    if value:
        lines.append(f"{var}={value}")
    f.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    if value:
        os.environ[var] = value
    else:
        os.environ.pop(var, None)


def load_env_into_process() -> None:
    """The cockpit authors definitions with the model in-process, so it needs the key the children read."""
    key = get_env_key()
    if key:
        os.environ[ENV_KEY] = key

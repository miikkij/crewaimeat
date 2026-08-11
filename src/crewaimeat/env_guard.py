"""Load `.env` for the fleet, and SHOUT when the ambient environment shadows it.

TWO REAL BUGS, one module, both found the hard way on 2026-08-09/10.

1. NOTHING IN THE FLEET PATH EVER LOADED `.env`. `llm.py` reads its keys with a plain
   `os.getenv(api_key_env)`, `fleet_host` never called `load_dotenv()`, and `uv run` does not read
   `.env` unless `UV_ENV_FILE` says so. The fleet therefore ran on whatever the LAUNCHING SHELL
   happened to export — which worked only because a key had been exported into that shell once, long
   ago, and inherited ever since. Clean the shell and the fleet silently has no key at all.

2. WHEN BOTH EXIST, THE ENVIRONMENT WINS AND SAYS NOTHING. `load_dotenv()` does not override an
   existing variable. So a STALE `OPENROUTER_API_KEY` in the environment beat the correct one in
   `.env`, every LLM call 401'd, and the only visible symptom was an evening edition that failed
   with two red steps. Cost: two days, twice.

The environment still wins — that is the standard contract and scripts and CI depend on it. What
changes is that it can no longer win QUIETLY: a differing value is reported on its own line, with
fingerprints of both, naming which one the fleet will actually use. Fingerprints only, never the
secret: the fleet log is pasted into chats and issues.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_done = False

# Names whose VALUE is a secret and must never be printed in full. Everything else (a model id, a
# base URL, a flag) is printed as-is, because seeing the actual value is the whole point of the report.
_SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASS", "CREDENTIAL", "API")


def _is_secret(name: str) -> bool:
    return any(h in name.upper() for h in _SECRET_HINTS)


def fingerprint(name: str, value: str | None) -> str:
    """A value rendered safe to log: secrets become len + head + tail, which is enough to tell two
    keys apart (the exact question this module exists to answer) and useless to anyone who steals it."""
    if value is None:
        return "(unset)"
    if not _is_secret(name):
        return value if len(value) <= 60 else value[:57] + "..."
    if len(value) <= 12:
        return f"len={len(value)} (too short to fingerprint)"
    return f"len={len(value)} {value[:10]}...{value[-4:]}"


def _parse_env_file(path: Path) -> dict[str, str]:
    """Minimal `.env` reader — enough for `NAME=value`, `export NAME=value`, quotes and comments.

    Deliberately not python-dotenv's parser: this runs BEFORE we hand over to python-dotenv and its
    job is only to see what the file DECLARES, so it can be compared against what is already set."""
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        name, sep, value = line.partition("=")
        if not sep:
            continue
        name = name.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if name:
            out[name] = value
    return out


def load_env(env_path: str | Path | None = None, *, quiet: bool = False) -> list[str]:
    """Load `.env` into the process, reporting every variable the environment shadows.

    Returns the names that were shadowed with a DIFFERENT value (empty when all is well). Idempotent —
    safe to call from both the fleet host and an individual crew entrypoint.
    """
    global _done
    if _done:
        return []
    _done = True

    path = Path(env_path) if env_path else Path.cwd() / ".env"
    if not path.is_file():
        if not quiet:
            print(f"[env] no .env at {path} — the fleet runs on the ambient environment only", file=sys.stderr)
        return []

    declared = _parse_env_file(path)
    shadowed = [n for n, v in declared.items() if n in os.environ and os.environ[n] != v]
    missing_before = [n for n in declared if n not in os.environ]

    # The environment keeps precedence (standard dotenv contract, and CI/scripts rely on it) — the
    # change is that it can no longer win in silence.
    for name in shadowed:
        env_v, file_v = os.environ[name], declared[name]
        print(
            f"[env] !! {name} DIFFERS between the environment and {path.name} — "
            f"the ENVIRONMENT WINS and {path.name} is ignored for it.\n"
            f"[env]    environment : {fingerprint(name, env_v)}   <-- the fleet will use THIS\n"
            f"[env]    {path.name:<11} : {fingerprint(name, file_v)}\n"
            f"[env]    If that is the wrong one, clear it in the shell you start the fleet from "
            f"(`Remove-Item Env:{name}`) and start again. A stale value here 401s every call and the "
            f"only symptom is red workflow steps.",
            file=sys.stderr,
        )

    try:
        from dotenv import load_dotenv
    except ImportError:  # python-dotenv is a hard dep of the fleet; say so rather than half-work
        print(
            f"[env] python-dotenv missing — {path.name} NOT loaded, keys must come from the environment",
            file=sys.stderr,
        )
        return shadowed

    load_dotenv(path)  # no override: environment keeps precedence, exactly as reported above
    if not quiet:
        loaded = [n for n in missing_before if n in os.environ]
        secrets = [n for n in loaded if _is_secret(n)]
        print(
            f"[env] loaded {path.name}: {len(declared)} declared, {len(loaded)} applied"
            + (f", {len(shadowed)} shadowed by the environment (see above)" if shadowed else "")
            + (f" — incl. {', '.join(sorted(secrets))}" if secrets else ""),
            file=sys.stderr,
        )
    return shadowed

"""The engine: the Node runtime and the AIMEAT connector that agency 2.0 ships INSIDE the installer.

0.8.x relied on a Node the person installed from nodejs.org and a globally npm-installed `aimeat`. On a
dev box both exist, so the appliance "worked" everywhere it was tested and nowhere a person without a dev
setup tried it. 2.0 never looks at the machine's Node: the shell passes the bundled folders in

    AIMEAT_AGENCY_NODE_DIR       <resources>/node          (portable node.exe)
    AIMEAT_AGENCY_CONNECTOR_DIR  <resources>/connector     (npm install --prefix … aimeat@<ver>)

and every child process gets that Node FIRST on PATH plus `AIMEAT_CLI` pointing at the bundled shim,
which is the seam `node_engine.aimeat_cli()` / `serve_guard` already honour. Proven on a clean PATH
2026-09-18 (spec §7 V0).

Outside the shell (a dev run with neither variable) the machine's own tools are used, and `status()`
says so — `bundled: False` is shown in the health view, never hidden.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from crewaimeat.agency2 import paths

NODE_DIR_ENV = "AIMEAT_AGENCY_NODE_DIR"
CONNECTOR_DIR_ENV = "AIMEAT_AGENCY_CONNECTOR_DIR"


def _dir(var: str) -> Path | None:
    v = os.environ.get(var, "").strip()
    return Path(v) if v else None


def node_exe() -> str | None:
    d = _dir(NODE_DIR_ENV)
    if d is not None:
        p = d / ("node.exe" if os.name == "nt" else "bin/node")
        return str(p) if p.is_file() else None
    return shutil.which("node")


def connector_js() -> Path | None:
    d = _dir(CONNECTOR_DIR_ENV)
    if d is None:
        return None
    p = d / "node_modules" / "aimeat" / "dist" / "bin" / "aimeat.js"
    return p if p.is_file() else None


def connector_shim() -> str | None:
    """The `.bin` shim — what `AIMEAT_CLI` must name, because serve_guard spawns it as a command."""
    d = _dir(CONNECTOR_DIR_ENV)
    if d is None:
        return None
    p = d / "node_modules" / ".bin" / ("aimeat.cmd" if os.name == "nt" else "aimeat")
    return str(p) if p.is_file() else None


def bundled() -> bool:
    return _dir(NODE_DIR_ENV) is not None or _dir(CONNECTOR_DIR_ENV) is not None


def connector_version() -> str | None:
    d = _dir(CONNECTOR_DIR_ENV)
    if d is None:
        return None
    try:
        return json.loads((d / "node_modules" / "aimeat" / "package.json").read_text(encoding="utf-8"))["version"]
    except (OSError, ValueError, KeyError):
        return None


def connector_argv(*args: str) -> list[str]:
    """argv to run the connector with `args`. Bundled: node.exe + aimeat.js (no shell, no PATH lookup)."""
    js, node = connector_js(), node_exe()
    if bundled():
        if not (js and node):
            raise RuntimeError(
                "the bundled engine is incomplete: "
                f"node={node or 'missing'} connector={js or 'missing'} "
                f"({NODE_DIR_ENV}={os.environ.get(NODE_DIR_ENV)!r}, {CONNECTOR_DIR_ENV}={os.environ.get(CONNECTOR_DIR_ENV)!r})"
            )
        return [node, str(js), *args]
    from crewaimeat.node_engine import aimeat_cli

    cli = aimeat_cli()
    if not cli:
        raise RuntimeError("no AIMEAT connector found (not bundled, and no `aimeat` on this machine)")
    return (["cmd", "/c", cli] if os.name == "nt" and cli.lower().endswith(".cmd") else [cli]) + list(args)


def apply_to_process() -> None:
    """Make THIS process (and so everything it spawns, incl. serve_guard's daemon) use the bundled engine."""
    d = _dir(NODE_DIR_ENV)
    if d is not None:
        parts = os.environ.get("PATH", "").split(os.pathsep)
        if str(d) not in parts:
            os.environ["PATH"] = os.pathsep.join([str(d), *parts])
    shim = connector_shim()
    if shim:
        os.environ["AIMEAT_CLI"] = shim
    os.environ.setdefault("AIMEAT_HOME", str(paths.aimeat_home()))


# 2.0 thinks through OpenRouter only (owner, 2026-09-18). `llm.get_llm` tries NVIDIA_KEY and xAI BEFORE
# OpenRouter, so either one present would silently route an agent elsewhere.
FOREIGN_PROVIDER_VARS = ("NVIDIA_KEY", "USE_XAI", "XAI_API_KEY")


def openrouter_only() -> list[str]:
    """This process's environment = what it was started with + the app's own `.env`. Nothing else.

    Returns the names removed. Call it FIRST in a process, before anything imports crewai.

    WHY. litellm (`LITELLM_MODE=DEV`, its default) and `crewai/llm.py` call a bare `load_dotenv()` at
    import, which walks up from the LIBRARY's own folder. With the venv inside a checkout that finds the
    checkout's `.env` and fills in whatever it declares. Measured 2026-09-18 in a dev run: the cockpit
    routed to `NVIDIA NIM (z-ai/glm-5.2)` from crewfive/.env (410 Gone), and after that switch was
    removed, to that file's `OPENROUTER_MODEL` (a rate-limited free model). In the installed app the
    walk finds the app's own `.env`, but the app must not depend on where its venv happens to sit.
    So the imports run here, and every variable they ADDED is taken back out. The other providers'
    switches go too: 2.0 thinks through OpenRouter only (owner, 2026-09-18).
    """
    from crewaimeat.agency2 import LAUNCH_ENV, paths

    os.environ.setdefault("LITELLM_MODE", "PRODUCTION")
    removed: list[str] = []
    try:
        import crewai.llm as _crewai_llm  # for its module-level load_dotenv(), now rather than later
        from dotenv import dotenv_values, find_dotenv  # noqa: F401

        stray = _stray_dotenv(Path(_crewai_llm.__file__).parent)
        if stray is not None:
            ours_file = paths.data_dir() / ".env"
            ours = set(dotenv_values(ours_file)) if ours_file.is_file() else set()
            for k in dotenv_values(stray):
                if k not in LAUNCH_ENV and k not in ours and os.environ.pop(k, None) is not None:
                    removed.append(k)
    except Exception:  # noqa: BLE001 — no crewai in this process is fine: nothing will load it then
        pass
    removed += [v for v in FOREIGN_PROVIDER_VARS if os.environ.pop(v, None) is not None]
    _silence_bare_load_dotenv()
    paths.load_env_into_process()
    if removed:
        print(f"[agency2] ignored {', '.join(sorted(set(removed)))} (not from this app's .env)", flush=True)
    return removed


def _silence_bare_load_dotenv() -> None:
    """From here on, a `load_dotenv()` with NO path does nothing in this process.

    Cleaning once is not enough: crewai has a THIRD bare call in `crewai/project/crew_base.py`, which
    loads lazily when a Crew is built — after the cleanup (measured 2026-09-18: a trial routed to
    NVIDIA again). A call that names its file (env_guard's `.env` in the working directory = the app's
    own) keeps working.
    """
    try:
        import dotenv
        import dotenv.main
    except ImportError:
        return
    original = getattr(dotenv.main.load_dotenv, "__wrapped__", dotenv.main.load_dotenv)

    def load_dotenv(dotenv_path=None, stream=None, *args, **kwargs):  # noqa: ANN001 — dotenv's own signature
        if dotenv_path is None and stream is None:
            return False
        return original(dotenv_path, stream, *args, **kwargs)

    load_dotenv.__wrapped__ = original  # type: ignore[attr-defined]
    dotenv.load_dotenv = load_dotenv
    dotenv.main.load_dotenv = load_dotenv


def _stray_dotenv(start: Path) -> Path | None:
    """The `.env` a bare `load_dotenv()` in a library at `start` finds (python-dotenv walks UP from the
    caller's folder) — when it is not this app's own. None when there is none, or it is ours."""
    from crewaimeat.agency2 import paths

    ours = (paths.data_dir() / ".env").resolve()
    for d in [start, *start.parents]:
        f = d / ".env"
        if f.is_file():
            return None if f.resolve() == ours else f
    return None


def child_env() -> dict[str, str]:
    apply_to_process()
    env = {k: v for k, v in os.environ.items() if k not in FOREIGN_PROVIDER_VARS}
    env["LITELLM_MODE"] = env.get("LITELLM_MODE", "PRODUCTION")
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def python_exe() -> str:
    return sys.executable


def status() -> dict:
    node = node_exe()
    js = connector_js() if bundled() else None
    ok = bool(node) and (bool(js) if bundled() else True)
    return {
        "bundled": bundled(),
        "node": node,
        "connector": str(js) if js else None,
        "connector_version": connector_version(),
        "ok": ok,
    }

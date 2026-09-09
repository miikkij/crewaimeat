"""Agency reset service: stop first and report partial cleanup explicitly."""

from __future__ import annotations

import os
from pathlib import Path

from crewaimeat import brains


def reset_agency(stop_ollama) -> dict:
    """Wipe ALL agency state (account, brains, agents, memory, tokens) for a true fresh start — what the
    uninstaller's 'delete application data' doesn't reach. Stops the fleet first so nothing is locked."""
    import shutil

    from crewaimeat._home import aimeat_home
    from crewaimeat.tui import actions

    errors = []
    try:
        actions.stop_fleet(strict=True)
    except Exception as exc:
        raise RuntimeError(f"Cannot reset while fleet shutdown failed: {exc}") from exc
    removed = []
    # Brains live in a SQLite DB that may be LOCKED (so file-unlink can fail silently) — clear the rows
    # through the data layer instead, plus its model overrides. This is the bit a plain file delete missed.
    try:
        from crewaimeat import llm

        for b in brains.list_brains():
            name = b["agent_name"]
            if brains.delete_brain(name):
                removed.append("brain:" + name)
            llm.clear_override(name)
    except Exception as exc:
        errors.append(str(exc))
    stop_ollama()  # if WE started ollama, stop it too — reset means a truly cold start
    home = Path(aimeat_home())
    for name in (
        "brains.db",
        "sessions.db",
        "chat.db",
        "agency_apps.db",
        "local_memory.db",
        "events.db",
        "agency_account.json",
        "llm_overrides.json",
        "serve.json",
        "agency_ollama.pid",  # else a later shutdown could act on a stale (reused) pid
    ):
        for suffix in ("", "-wal", "-shm"):
            p = home / (name + suffix)
            try:
                if p.exists():
                    p.unlink()
                    removed.append(p.name)
            except OSError as exc:
                errors.append(str(exc))
    tok = home / "tokens"
    if tok.is_dir():
        shutil.rmtree(tok, onerror=lambda fn, path, exc: errors.append(f"{path}: {exc[1]}"))
        removed.append("tokens/")
    for f in Path("crews").glob("*_crew.py"):  # generated brain stubs
        try:
            f.unlink()
            removed.append(f.name)
        except OSError as exc:
            errors.append(str(exc))
    # The reset confirm promises ALL settings go — that includes the saved OpenRouter key (.env)
    # and this process's copy of it, so the wizard's model step truly starts over.
    try:
        from crewaimeat.forge import _project_root

        envf = _project_root() / ".env"
        if envf.is_file():
            lines = [
                ln for ln in envf.read_text(encoding="utf-8").splitlines() if not ln.startswith("OPENROUTER_API_KEY=")
            ]
            envf.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
            removed.append(".env:OPENROUTER_API_KEY")
    except OSError as exc:
        errors.append(str(exc))
    os.environ.pop("OPENROUTER_API_KEY", None)
    # Crew/register logs are agent data too (prompts, outputs, device codes) — a fresh start drops them.
    logs = Path("logs")
    if logs.is_dir():
        shutil.rmtree(
            logs, onerror=lambda fn, path, exc: errors.append(f"{path}: {exc[1]}")
        )  # best-effort: a file held open just survives
        removed.append("logs/")
    os.environ.pop("AIMEAT_OWNER", None)  # so the wizard restarts at step 1
    return {"ok": not errors, "removed": removed, "errors": errors}

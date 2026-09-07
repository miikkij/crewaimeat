"""node_engine — locate the Node.js toolchain + the AIMEAT connector CLI the fleet depends on.

Device-auth shells out to `npx aimeat@<ver>` (forge.register_agent) and the shared serve daemon is the
globally npm-installed `aimeat` CLI (aimeat_crewai.ensure_serve). Every dev/test machine has had both on
PATH, which hid a fatal appliance gap: a fresh machine has neither, and nothing checked. This module is
the single place that answers "is the engine there, and HOW do we invoke it?" for the cockpit's setup
wizard and for the actual spawns.

Windows PATH nuance (same class as the cockpit's _ollama_bin): a JUST-installed Node (MSI) updates the
machine PATH, but processes already running — the cockpit — don't see it until relaunch. So every lookup
falls back to the default install locations (Program Files\nodejs, %APPDATA%\npm for `npm -g` shims).
"""

from __future__ import annotations

import os
import shutil


def _win_candidates(*paths: str) -> str | None:
    for p in paths:
        if p and os.path.isfile(p):
            return p
    return None


def node_bin() -> str | None:
    """The node executable, or None when Node.js is not installed."""
    p = shutil.which("node")
    if p:
        return p
    if os.name == "nt":
        pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        return _win_candidates(os.path.join(pf, "nodejs", "node.exe"))
    return None


def npx_bin() -> str | None:
    """The npx command (bare name when on PATH, else the default install dir), or None."""
    if shutil.which("npx"):
        return "npx"  # on PATH — spawn sites wrap bare names via `cmd /c` themselves (Windows .cmd shim)
    if os.name == "nt":
        pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        return _win_candidates(os.path.join(pf, "nodejs", "npx.cmd"))
    return None


def npm_bin() -> str | None:
    """The npm command (bare name when on PATH, else the default install dir), or None."""
    if shutil.which("npm"):
        return "npm"
    if os.name == "nt":
        pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        return _win_candidates(os.path.join(pf, "nodejs", "npm.cmd"))
    return None


def aimeat_cli() -> str | None:
    """The connector CLI to run: `AIMEAT_CLI` when set, else the globally npm-installed `aimeat`
    (bare name when on PATH, else the npm -g shim dir %APPDATA%\\npm). None when nothing is found.

    `AIMEAT_CLI` EXISTS BECAUSE THE PUBLISHED CONNECTOR CAN LAG THE NODE. Measured 2026-09-03 on the
    developer's own machine: agent v2 asks the daemon to generate an Ed25519 key per agent, and that
    code (`connect/agent-key.js`, `connect/enrolment.js`) was in a local build only — neither the
    installed 3.10.0 nor the PUBLISHED 3.11.0 contained either file, though the local build calls
    itself 3.11.0 too. With no override there was no way to point a fleet at a connector that could
    do the thing the node was offering, and the version number could not tell you which you had.
    Setting this is the owner's decision and it is deliberately not inferred: pointing a live fleet
    at an unreleased binary is a choice somebody makes on purpose.
    """
    override = os.environ.get("AIMEAT_CLI", "").strip()
    if override:
        return override
    if shutil.which("aimeat"):
        return "aimeat"
    if os.name == "nt":
        appdata = os.environ.get("APPDATA", "")
        return _win_candidates(os.path.join(appdata, "npm", "aimeat.cmd"))
    return None


def serve_command() -> str | list[str]:
    """What to pass ensure_serve() as `aimeat_command`. Bare "aimeat" when the CLI resolves normally
    (ensure_serve's own Windows .cmd shim handling applies); a full-path `cmd /c` argv when the CLI is
    installed but not yet on this process's PATH (fresh install, no relaunch). Falls back to "aimeat"
    when nothing is found — the spawn then fails with the connector's own clear error.

    An `AIMEAT_CLI` pointing at a `.js` entry point is run with `node`: that is the shape a connector
    build has before it is packaged (`<repo>/aimeat/dist/bin/aimeat.js`), and `cmd /c` on a .js path
    hands it to the shell's file association instead of to node."""
    cli = aimeat_cli()
    if cli is None or cli == "aimeat":
        return "aimeat"
    if str(cli).lower().endswith(".js"):
        return [node_bin() or "node", cli]
    if os.name == "nt":  # CreateProcess can't exec a .cmd shim directly; wrap it
        return ["cmd", "/c", cli]
    return cli


def engine_status() -> dict:
    """The wizard's read model: {node, npx, connector_cli, ready}. `ready` = agents can device-auth
    (npx) AND the serve daemon can spawn (aimeat CLI)."""
    node = node_bin() is not None
    npx = npx_bin() is not None
    cli = aimeat_cli() is not None
    return {"node": node, "npx": npx, "connector_cli": cli, "ready": npx and cli}

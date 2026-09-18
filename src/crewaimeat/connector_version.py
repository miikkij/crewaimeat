"""`crewaimeat connector` — keep the npm `aimeat` connector at the newest release.

THE OWNER'S RULE (2026-09-18): the fleet always runs the newest `aimeat` from npm. Three versions can
drift apart, and each has drifted:

  npm latest   what aimeat publishes — 3.15.0, 3.16.0 and 3.17.0 landed on three consecutive days
  installed    the machine's global `aimeat`. The serve daemon runs from it, so this is the version
               every crew actually talks to the node through, and no lockfile here pins it
  pin          forge.AIMEAT_CONNECTOR — what registration (`npx`) and the agency installer fetch

On 2026-09-18 they read 3.17.0 / 3.15.0 / 3.10.0: the pin was seven releases behind while its own
comment claimed doctor compared it against npm. Nothing did. A rule that nothing checks is a wish.

So this module is the check, and it runs in two places that between them leave no gap:
  * `start_fleet` runs `--install`: a stopped fleet is upgraded to npm latest before the serve daemon
    starts, so the daemon that comes up IS the newest one. A LIVE daemon running from the global
    install — this checkout's or any other home's, since they share it — is never upgraded underneath
    — its node process lazily loads modules from the very directory npm would rewrite, which is the
    half-edited-shared-infrastructure failure of 2026-08-28 in another form. It says what to do.
  * the pre-commit hook runs `--hook`: a commit fails while the PIN is behind npm latest, naming the
    one command that fixes it (`--bump-pin`). The pin is repo state, so the repo's gate owns it; the
    installed version is machine state, so the fleet start owns that.

An unreachable registry is NOT CHECKED, said out loud — never a clean bill, and never a blocked commit
on a train with no network.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

# Exit codes — start_fleet and the hook read these.
OK, BEHIND, NOT_CHECKED, BELOW_FLOOR = 0, 1, 3, 4

_PIN_LINE = re.compile(r'^(AIMEAT_CONNECTOR\s*=\s*")aimeat@(\d+\.\d+\.\d+)("[^\r\n]*)', re.MULTILINE)
_SEMVER = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def parse(v: str | None) -> tuple[int, int, int] | None:
    m = _SEMVER.search(v or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def older(a: str | None, b: str | None) -> bool:
    """True when `a` is PROVABLY older than `b`. Unknown on either side is never "older"."""
    pa, pb = parse(a), parse(b)
    return bool(pa and pb and pa < pb)


def npm_latest() -> tuple[str | None, str]:
    """(version, reason). The version is None when the registry could not be read, and the reason says
    why — a reason is what separates "not checked" from "fine"."""
    try:
        import requests

        r = requests.get("https://registry.npmjs.org/aimeat/latest", timeout=10)
    except Exception as exc:  # noqa: BLE001 — every transport failure is the same answer: not checked
        return None, f"npm registry unreachable ({type(exc).__name__})"
    if r.status_code != 200:
        return None, f"npm registry answered HTTP {r.status_code}"
    try:
        v = r.json().get("version")
    except ValueError:
        return None, "npm registry answered something that is not JSON"
    return (v, "") if parse(v) else (None, f"npm registry named no version ({v!r})")


def installed_version(timeout: int = 30) -> tuple[str | None, str]:
    """(version, reason) of the connector this machine's fleet runs: `AIMEAT_CLI` when set, else the
    global `aimeat`."""
    from crewaimeat.node_engine import aimeat_cli

    cli = aimeat_cli()
    if not cli:
        return None, "no `aimeat` CLI found (npm i -g aimeat)"
    argv = ["node", cli, "--version"] if cli.endswith(".js") else [cli, "--version"]
    if os.name == "nt" and not cli.endswith(".js"):
        argv = ["cmd", "/c", *argv]
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:  # noqa: BLE001
        return None, f"`{cli} --version` failed ({type(exc).__name__})"
    m = _SEMVER.search(out.stdout or "") or _SEMVER.search(out.stderr or "")
    return (m.group(0), "") if m else (None, f"`{cli} --version` printed no version")


def pin_version() -> tuple[str, str]:
    """(pin, floor) as bare versions, read from forge — the one source of truth."""
    from crewaimeat.forge import AIMEAT_CONNECTOR, AIMEAT_CONNECTOR_FLOOR

    return AIMEAT_CONNECTOR.split("@", 1)[-1], AIMEAT_CONNECTOR_FLOOR


def _forge_path() -> Path:
    return Path(__file__).resolve().parent / "forge.py"


def bump_pin(version: str, path: Path | None = None) -> bool:
    """Rewrite the pin line in forge.py to `aimeat@<version>`. Returns False when it is already there.
    Raises when the line cannot be found: a bump that silently did nothing would look like a success."""
    if not parse(version):
        raise ValueError(f"not a version: {version!r}")
    path = path or _forge_path()
    # newline="" both ways: forge.py keeps whatever line endings it has (Windows would otherwise turn
    # every LF into CRLF on write, and the one-line bump would become a whole-file diff).
    with open(path, encoding="utf-8", newline="") as fh:
        text = fh.read()
    m = _PIN_LINE.search(text)
    if not m:
        raise RuntimeError(f'no `AIMEAT_CONNECTOR = "aimeat@x.y.z"` line in {path}')
    if m.group(2) == version:
        return False
    line = (
        f'{m.group(1)}aimeat@{version}"  # bumped {date.today().isoformat()} to npm latest by '
        f"`crewaimeat connector --bump-pin`."
    )
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text[: m.start()] + line + text[m.end() :])
    return True


def _global_root() -> str | None:
    """`npm root -g` — the one directory every home's global `aimeat` loads from."""
    from crewaimeat.node_engine import npm_bin

    npm = npm_bin()
    if not npm:
        return None
    argv = ["cmd", "/c", npm, "root", "-g"] if os.name == "nt" else [npm, "root", "-g"]
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception:  # noqa: BLE001 — None makes the caller refuse, which is the safe answer
        return None
    return out.splitlines()[-1].strip() if out else None


def _process_cmdlines() -> list[tuple[int, str]]:
    """(pid, command line) of every process. Raises when the table cannot be read."""
    if os.name == "nt":
        ps = (
            "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine } | "
            'ForEach-Object { "$($_.ProcessId)`t$($_.CommandLine)" }'
        )
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=60, check=True
        ).stdout
    else:
        out = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True, timeout=30, check=True).stdout
        out = "\n".join("\t".join(line.strip().split(None, 1)) for line in out.splitlines() if line.strip())
    rows = []
    for line in out.splitlines():
        pid, _, cmd = line.partition("\t")
        if pid.strip().isdigit():
            rows.append((int(pid), cmd))
    return rows


def _norm_path(s: str, *, windows: bool | None = None) -> str:
    """Comparable path text. On Windows: one backslash style, case-folded, and repeated separators
    collapsed — npm's own shim runs `"%dp0%\\node_modules\\aimeat\\..."` with a %dp0% that already ends in
    a backslash, so a live daemon's command line reads `nodejs\\\\node_modules`."""
    if windows if windows is not None else os.name == "nt":
        return re.sub(r"\\+", r"\\", s.replace("/", "\\")).lower()
    return re.sub(r"/+", "/", s)


def serve_pids_on_global_install(root: str, procs: list[tuple[int, str]], *, windows: bool | None = None) -> list[int]:
    """The `aimeat connect serve` processes that run FROM the global install at `root`.

    The global install is one directory shared by every home on the machine — this checkout's fleet, a
    sibling checkout, the agency desktop — so any of their daemons would load modules from the files npm
    rewrites. A daemon run from a DIFFERENT build (another session's scratchpad connector, an AIMEAT_CLI
    build) does not load from here and does not block. Matching on the command line alone was wrong in
    both directions: measured 2026-09-18, `connect.*serve` matched Google Drive's crash handler and the
    shell running the probe, and would have kept the upgrade blocked forever."""
    rootn = _norm_path(root.rstrip("\\/"), windows=windows)
    hits = []
    for pid, cmd in procs:
        c = _norm_path(cmd, windows=windows)
        if rootn in c and "aimeat" in c[c.find(rootn) :] and re.search(r"\bconnect\s+serve\b", c):
            hits.append(pid)
    return hits


def live_serve_pids() -> list[int]:
    """Serve daemons that load from the global install. Raises when that cannot be established — an
    upgrade decided on a guess could rewrite a live daemon's files."""
    root = _global_root()
    if not root:
        raise RuntimeError("could not read `npm root -g`")
    procs = _process_cmdlines()
    if not procs:  # a machine with no processes is a probe that did not work, not a quiet machine
        raise RuntimeError("the process table came back empty")
    return serve_pids_on_global_install(root, procs)


def install(version: str) -> tuple[bool, str]:
    """`npm install -g aimeat@<version>`, then read the version back. Refused while any serve daemon on
    this machine is alive, and when AIMEAT_CLI points somewhere deliberate."""
    if os.environ.get("AIMEAT_CLI", "").strip():
        return False, "AIMEAT_CLI is set — the owner chose that binary on purpose; it is not upgraded"
    try:
        pids = live_serve_pids()
    except Exception as exc:  # noqa: BLE001 — cannot tell whether a daemon is live: refuse, and say why
        return False, f"could not check for running serve daemons ({exc}); not upgrading on a guess"
    if pids:
        return False, (
            f"serve daemon(s) running from the global install (pid {', '.join(map(str, pids))}); npm would "
            "rewrite the files they load from. Stop them (scripts/terminate_fleet here; another checkout's "
            "or the desktop app's own stop for theirs), then start the fleet again to upgrade"
        )
    from crewaimeat.node_engine import npm_bin

    npm = npm_bin()
    if not npm:
        return False, "npm not found — install Node.js"
    argv = [npm, "install", "-g", f"aimeat@{version}"]
    if os.name == "nt":
        argv = ["cmd", "/c", *argv]
    r = subprocess.run(argv, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
        return False, f"npm install failed (exit {r.returncode}): {' | '.join(tail)}"
    # The FIRST run of a freshly installed CLI is slow — measured 2026-09-18: >30 s right after install
    # (a cold start behind the virus scanner), 2.4 s on the next call. Read-back gets the long budget.
    got, why = installed_version(timeout=180)
    if got != version:
        return False, f"npm reported success but `aimeat --version` reads {got or why}"
    return True, f"installed aimeat@{version}"


def report(*, do_install: bool = False, do_bump: bool = False, hook: bool = False) -> int:
    latest, why_latest = npm_latest()
    pin, floor = pin_version()

    if hook:  # repo state only; the installed version is this machine's business, not the commit's
        if not latest:
            print(f"[connector] pin aimeat@{pin} NOT CHECKED — {why_latest}", file=sys.stderr)
            return OK
        if older(pin, latest):
            print(
                f"[connector] the repo pins aimeat@{pin} but npm latest is {latest}. The fleet runs the "
                "newest connector — bump the pin in this commit:\n"
                "    uv run crewaimeat connector --bump-pin",
                file=sys.stderr,
            )
            return BEHIND
        return OK

    inst, why_inst = installed_version()
    print(f"  npm latest   {latest or 'NOT CHECKED — ' + why_latest}")
    print(f"  installed    {inst or 'UNKNOWN — ' + why_inst}")
    print(f"  repo pin     {pin}    (floor {floor})")

    code = OK
    if not latest:
        code = NOT_CHECKED
    target = latest

    if target and older(inst, target):
        if do_install:
            ok, msg = install(target)
            print(f"  -> {msg}")
            if ok:
                inst = target
            else:
                code = BEHIND
        else:
            print("  -> installed is behind: uv run crewaimeat connector --install  (fleet stopped)")
            code = BEHIND

    if target and older(pin, target):
        if do_bump:
            bump_pin(target)
            print(f"  -> pin bumped to aimeat@{target} in src/crewaimeat/forge.py — commit it")
            pin = target
        else:
            print("  -> repo pin is behind: uv run crewaimeat connector --bump-pin")
            code = BEHIND

    if older(inst, floor):
        print(f"  !! installed {inst} is BELOW the floor {floor}: a task can be created and never wake an agent")
        return BELOW_FLOOR
    return code


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="crewaimeat connector",
        description="Report npm latest vs the installed aimeat CLI vs the repo pin; keep all three at latest.",
    )
    ap.add_argument("--install", action="store_true", help="npm install -g the latest (refused while the fleet runs)")
    ap.add_argument("--bump-pin", action="store_true", help="rewrite forge.AIMEAT_CONNECTOR to npm latest")
    ap.add_argument("--hook", action="store_true", help="pre-commit mode: fail only when the PIN is behind npm")
    a = ap.parse_args(argv)
    return report(do_install=a.install, do_bump=a.bump_pin, hook=a.hook)


if __name__ == "__main__":
    raise SystemExit(main())

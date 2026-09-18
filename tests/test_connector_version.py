"""`crewaimeat connector`: the fleet runs the newest aimeat from npm (owner's rule, 2026-09-18).

Offline: the registry, the installed CLI, npm and the serve-daemon probe are all stubbed.
"""

from __future__ import annotations

import pytest

from crewaimeat import connector_version as cv


@pytest.fixture
def world(monkeypatch):
    """A controllable world: npm latest, installed CLI, repo pin/floor, live serve daemons."""
    state = {"latest": ("3.17.0", ""), "installed": ("3.15.0", ""), "pin": ("3.10.0", "3.13.4"), "pids": []}
    monkeypatch.setattr(cv, "npm_latest", lambda: state["latest"])
    monkeypatch.setattr(cv, "installed_version", lambda **_: state["installed"])
    monkeypatch.setattr(cv, "pin_version", lambda: state["pin"])
    monkeypatch.setattr(cv, "live_serve_pids", lambda: state["pids"])
    monkeypatch.delenv("AIMEAT_CLI", raising=False)
    return state


def test_older_is_only_true_on_proof():
    assert cv.older("3.15.0", "3.17.0")
    assert cv.older("3.9.9", "3.10.0")  # numeric, not string, comparison
    assert not cv.older("3.17.0", "3.17.0")
    assert not cv.older(None, "3.17.0")  # unknown is never "older"
    assert not cv.older("3.17.0", None)


def test_hook_fails_while_the_pin_is_behind_npm(world, capsys):
    assert cv.report(hook=True) == cv.BEHIND
    assert "crewaimeat connector --bump-pin" in capsys.readouterr().err


def test_hook_passes_at_latest(world):
    world["pin"] = ("3.17.0", "3.13.4")
    assert cv.report(hook=True) == cv.OK


def test_hook_ignores_the_machine_installed_version(world):
    """The installed CLI is machine state; a commit gate must not fail on it."""
    world["pin"] = ("3.17.0", "3.13.4")
    world["installed"] = ("3.0.0", "")
    assert cv.report(hook=True) == cv.OK


def test_hook_never_blocks_without_a_registry_but_says_so(world, capsys):
    world["latest"] = (None, "npm registry unreachable (ConnectionError)")
    assert cv.report(hook=True) == cv.OK
    assert "NOT CHECKED" in capsys.readouterr().err


def test_report_flags_both_installed_and_pin_behind(world, capsys):
    assert cv.report() == cv.BEHIND
    out = capsys.readouterr().out
    assert "--install" in out and "--bump-pin" in out


def test_report_is_not_a_clean_bill_when_npm_is_unreachable(world, capsys):
    world["latest"] = (None, "npm registry answered HTTP 503")
    world["pin"] = ("3.17.0", "3.13.4")
    world["installed"] = ("3.17.0", "")
    assert cv.report() == cv.NOT_CHECKED
    assert "NOT CHECKED" in capsys.readouterr().out


def test_below_floor_wins_over_everything(world):
    world["installed"] = ("3.13.1", "")
    world["pids"] = [1234]  # the upgrade is refused, so the floor verdict must stand
    assert cv.report(do_install=True) == cv.BELOW_FLOOR


def test_install_is_refused_under_a_live_serve_daemon(world):
    world["pids"] = [4321]
    ok, msg = cv.install("3.17.0")
    assert not ok
    assert "4321" in msg and "terminate_fleet" in msg


def test_install_leaves_a_deliberate_aimeat_cli_alone(world, monkeypatch):
    monkeypatch.setenv("AIMEAT_CLI", "C:/build/aimeat.js")
    ok, msg = cv.install("3.17.0")
    assert not ok and "AIMEAT_CLI" in msg


def test_install_reads_the_version_back(world, monkeypatch):
    """npm saying success is not proof: the version is read back, and a mismatch is a failure."""

    class Done:
        returncode, stdout, stderr = 0, "", ""

    monkeypatch.setattr("crewaimeat.node_engine.npm_bin", lambda: "npm")
    monkeypatch.setattr(cv.subprocess, "run", lambda *a, **k: Done())
    ok, msg = cv.install("3.17.0")  # installed_version still reads 3.15.0
    assert not ok and "3.15.0" in msg
    world["installed"] = ("3.17.0", "")
    assert cv.install("3.17.0") == (True, "installed aimeat@3.17.0")


WIN_ROOT = r"C:\Program Files\nodejs\node_modules"


def test_a_daemon_on_the_global_install_blocks_the_upgrade_as_npm_launches_it():
    """npm's shim passes `"%dp0%\\node_modules\\..."` with a trailing-backslash %dp0% — a doubled separator."""
    procs = [(101, r'"node"  "C:\Program Files\nodejs\\node_modules\aimeat\dist\bin\aimeat.js" connect serve --http')]
    assert cv.serve_pids_on_global_install(WIN_ROOT, procs, windows=True) == [101]


def test_what_is_not_a_global_serve_daemon_does_not_block():
    """Measured 2026-09-18: all three of these matched the old `connect.*serve` probe."""
    procs = [
        # Google Drive's crash handler: 'connect' and 'serve' both appear somewhere in its annotations
        (44216, r'"C:\Program Files\Google\crashpad_handler.exe" --annotation=max_connection_idle_time --url=server'),
        # another session's own connector build, loading from ITS directory, not the global one
        (
            33776,
            r'"C:\Temp\scratchpad\node.exe" "C:\Temp\scratchpad\connector\node_modules\aimeat\dist\bin\aimeat.js" '
            r"connect serve --http",
        ),
        # the global install doing something other than serving
        (7, r'"node" "C:\Program Files\nodejs\node_modules\aimeat\dist\bin\aimeat.js" connect call memory_read'),
        # the shell that runs the probe, quoting the pattern
        (8, r"bash -c \"Get-CimInstance ... -match 'connect.*serve'\""),
    ]
    assert cv.serve_pids_on_global_install(WIN_ROOT, procs, windows=True) == []


def test_posix_paths_match_too():
    procs = [(5, "node /usr/lib/node_modules/aimeat/dist/bin/aimeat.js connect serve --http")]
    assert cv.serve_pids_on_global_install("/usr/lib/node_modules", procs, windows=False) == [5]


def test_install_refuses_when_it_cannot_tell(world, monkeypatch):
    def boom():
        raise RuntimeError("could not read `npm root -g`")

    monkeypatch.setattr(cv, "live_serve_pids", boom)
    ok, msg = cv.install("3.17.0")
    assert not ok and "not upgrading on a guess" in msg


PIN_FILE = """x = 1
AIMEAT_CONNECTOR = "aimeat@3.10.0"  # bumped 2026-08-30 (npm latest)
#   history comment that stays
AIMEAT_CONNECTOR_FLOOR = "3.13.4"
"""


def test_bump_pin_rewrites_only_the_pin_line(tmp_path):
    f = tmp_path / "forge.py"
    f.write_text(PIN_FILE, encoding="utf-8")
    assert cv.bump_pin("3.17.0", f) is True
    text = f.read_text(encoding="utf-8")
    assert 'AIMEAT_CONNECTOR = "aimeat@3.17.0"' in text
    assert "history comment that stays" in text
    assert 'AIMEAT_CONNECTOR_FLOOR = "3.13.4"' in text
    assert cv.bump_pin("3.17.0", f) is False  # idempotent


def test_bump_pin_keeps_crlf_line_endings(tmp_path):
    """forge.py is CRLF on this repo; a one-line bump must not become a whole-file diff."""
    f = tmp_path / "forge.py"
    f.write_bytes(PIN_FILE.replace("\n", "\r\n").encode("utf-8"))
    cv.bump_pin("3.17.0", f)
    raw = f.read_bytes()
    assert raw.count(b"\r\n") == 4 and b"\n" not in raw.replace(b"\r\n", b"")
    assert b'"aimeat@3.17.0"' in raw


def test_bump_pin_refuses_to_succeed_at_nothing(tmp_path):
    f = tmp_path / "forge.py"
    f.write_text("nothing here\n", encoding="utf-8")
    with pytest.raises(RuntimeError):
        cv.bump_pin("3.17.0", f)
    with pytest.raises(ValueError):
        cv.bump_pin("latest", f)


def test_the_real_pin_line_is_bumpable():
    """If forge's pin line ever changes shape, --bump-pin must fail here, not on the owner's machine."""
    assert cv._PIN_LINE.search(cv._forge_path().read_text(encoding="utf-8"))


def test_the_real_pin_is_at_or_above_the_floor():
    pin, floor = cv.pin_version()
    assert not cv.older(pin, floor)

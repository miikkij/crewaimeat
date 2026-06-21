"""fleet_state — the pure, testable data layer behind the fleet TUI (and a cross-platform successor
to scripts/view_fleet.ps1).

Separation of concerns:
  - DERIVATION (top): pure functions over raw inputs (process command lines, lock names, serve.json,
    the node's agents_list). Unit-tested with fakes — no OS, no network.
  - COLLECTORS (bottom): the impure edges that read the process table / lock files / serve.json and
    make the ONE read-only `aimeat_agents_list` call. `build_snapshot` wires them together; the UI
    renders the FleetSnapshot and never gathers state itself.

Safety (docs/internal/tui-plan.md): collectors NEVER auto-start the serve daemon — they read
serve.json and enumerate processes only. The single node call is read-only and made on a modest
cadence by the UI, never in a tight loop ([[background-loops-spawn-daemons]]).
"""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

# A crew process' command line references crews/<name>_crew.py; its supervisor also matches
# watchdog.(ps1|sh). The serve daemon matches 'connect serve'. Same patterns as view_fleet.ps1.
_CREW_RE = re.compile(r"crews[\\/]([A-Za-z0-9_]+_crew\.py)")
_WATCHDOG_RE = re.compile(r"watchdog\.(ps1|sh)")
_CONNECT_RE = re.compile(r"connect\s+serve")

_LOCKS_DIR = Path("logs/.locks")
_HOST_STATUS_FILE = Path("logs/.host_status.json")  # heartbeat written by fleet_host (threaded model)
_HOST_STALE_S = 15  # the host rewrites it every ~2s; older than this means the host is gone

# Node last_seen older than this WHILE the local daemon is up = the daemon isn't heartbeating to the
# node (the "orange" stale-heartbeat case: image-maker / ledger-reader / research-crew / doc-fact-reader).
STALE_AFTER_S = 600


@dataclass
class AgentRow:
    agent: str
    crew_file: str | None
    watchdog_procs: int
    daemon_procs: int
    lock: bool
    in_tunnel: bool
    last_seen: str | None
    last_seen_age_s: float | None
    mode: str | None
    status: str
    hosted: bool = False  # running as a THREAD inside the fleet host (one process), not its own daemon


@dataclass
class FleetSnapshot:
    serve_pid: int | None
    serve_port: int | None
    n_watchdogs: int
    n_connectors: int
    n_locks: int
    rows: list[AgentRow]
    zombies: list[str]
    host_pid: int | None = None  # the fleet host process, when agents run threaded in one process


# ── pure derivation ──────────────────────────────────────────────────────────
def age_seconds(last_seen: str | None, now: datetime.datetime) -> float | None:
    """Seconds between `now` (tz-aware UTC) and an ISO `last_seen` ('…Z' or offset). None if absent
    or unparseable — a missing timestamp must never read as 'fresh'."""
    if not last_seen:
        return None
    try:
        dt = datetime.datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return (now - dt).total_seconds()


def tally_processes(cmdlines: list[str], known_fnames: set[str]) -> tuple[dict, list[str]]:
    """Count watchdog/daemon processes per crew filename and find zombies (a running crew with no
    file on disk). Returns ({fname: {'watchdog': n, 'daemon': n}}, [zombie_fname, ...]). Pure."""
    per: dict[str, dict[str, int]] = {}
    seen: set[str] = set()
    for cl in cmdlines:
        m = _CREW_RE.search(cl or "")
        if not m:
            continue
        fname = m.group(1)
        seen.add(fname)
        d = per.setdefault(fname, {"watchdog": 0, "daemon": 0})
        if _WATCHDOG_RE.search(cl):
            d["watchdog"] += 1
        else:
            d["daemon"] += 1
    zombies = sorted(f for f in seen if f not in known_fnames)
    return per, zombies


def derive_status(
    *,
    watchdog: int,
    daemon: int,
    lock: bool,
    in_tunnel: bool,
    age_s: float | None,
    stale_after_s: float = STALE_AFTER_S,
) -> str:
    """The single source of truth for an agent's status. Precedence matters: a duplicated watchdog is
    the loudest problem; a locally-running daemon the node hasn't heard from recently is
    'stale-heartbeat' (the silent-failure case), not 'running'."""
    if watchdog > 1:
        return "DUPLICATE"
    if daemon >= 1 and watchdog == 0:
        return "orphan"
    if daemon >= 1:  # watchdog >= 1 by elimination
        if age_s is not None and age_s > stale_after_s:
            return "stale-heartbeat"
        return "running"
    if lock:
        return "down (stale lock)"
    return "down"


def build_rows(
    *,
    roster: dict,
    tally: dict,
    locks: set,
    tunnel: set,
    node_index: dict,
    now: datetime.datetime,
    stale_after_s: float = STALE_AFTER_S,
    host_agents: set | None = None,
) -> list[AgentRow]:
    """Assemble one AgentRow per local crew (roster = {agent: crew_fname}) plus a row for every
    zombie (a running crew filename absent from the roster). Pure — all I/O already resolved. An agent
    in `host_agents` runs as a THREAD in the fleet host (no per-crew process), so it reads as running."""
    host_agents = host_agents or set()
    rows: list[AgentRow] = []
    for agent, fname in sorted(roster.items()):
        counts = tally.get(fname, {"watchdog": 0, "daemon": 0})
        node = node_index.get(agent) or {}
        age = age_seconds(node.get("last_seen"), now)
        hosted = agent in host_agents
        status = (
            "running"
            if hosted
            else derive_status(
                watchdog=counts["watchdog"],
                daemon=counts["daemon"],
                lock=agent in locks,
                in_tunnel=agent in tunnel,
                age_s=age,
                stale_after_s=stale_after_s,
            )
        )
        rows.append(
            AgentRow(
                agent=agent,
                crew_file=fname,
                watchdog_procs=counts["watchdog"],
                daemon_procs=counts["daemon"],
                lock=agent in locks,
                in_tunnel=agent in tunnel,
                last_seen=node.get("last_seen"),
                last_seen_age_s=age,
                mode=node.get("mode"),
                status=status,
                hosted=hosted,
            )
        )
    known = set(roster.values())
    for fname, counts in sorted(tally.items()):
        if fname in known:
            continue
        rows.append(
            AgentRow(
                agent=fname[: -len("_crew.py")] if fname.endswith("_crew.py") else fname,
                crew_file=None,
                watchdog_procs=counts["watchdog"],
                daemon_procs=counts["daemon"],
                lock=False,
                in_tunnel=False,
                last_seen=None,
                last_seen_age_s=None,
                mode=None,
                status="zombie",
            )
        )
    return rows


def serve_tunnel_agents(serve_doc: dict) -> set[str]:
    """Agent ids attached to the shared serve tunnel, from serve.json (agents[] or principals[])."""
    out: set[str] = set()
    for a in (serve_doc or {}).get("agents") or []:
        if a.get("agent"):
            out.add(a["agent"])
    for p in (serve_doc or {}).get("principals") or []:
        if p.get("type") == "agent" and p.get("id"):
            out.add(p["id"])
    return out


# ── impure collectors (the OS/network edges; defaults overridable for tests) ──
def _scope_to_repo(cmdlines: list[str]) -> list[str]:
    """Keep only command lines belonging to THIS checkout — those carrying this repo's root path (the
    crew/watchdog procs reference it via the venv python / absolute watchdog path). So a sibling clone
    running the SAME crew filenames (a separate dev fleet) is not counted into this fleet's view. The
    root carries a trailing separator so 'crewfive' can't match 'crewfive-dev'. Falls back to all lines
    if nothing matches (e.g. an unusual launch) so the view is never blanked by an over-strict filter."""
    root = str(Path.cwd()).rstrip("\\/")
    needles = (root + "\\", root + "/")
    scoped = [cl for cl in cmdlines if any(n.lower() in cl.lower() for n in needles)]
    return scoped or cmdlines


def collect_cmdlines() -> list[str]:
    """Command lines of fleet-relevant processes (crew daemons, watchdogs, the serve daemon),
    scoped to THIS checkout. Cross-platform: Win32_Process on Windows, `ps` elsewhere. Read-only."""
    if os.name == "nt":
        ps = "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine } | ForEach-Object { $_.CommandLine }"
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=25
            ).stdout
            return _scope_to_repo([ln for ln in out.splitlines() if ln.strip()])
        except Exception:  # noqa: BLE001
            return []
    try:
        out = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True, timeout=20).stdout
        return _scope_to_repo([ln for ln in out.splitlines() if ln.strip()])
    except Exception:  # noqa: BLE001
        return []


def collect_roster() -> dict[str, str]:
    """{agent_name: crew_filename} for every crews/*_crew.py on disk (skips _-prefixed helpers)."""
    from crewaimeat.forge import _agent_name_of, _crew_files

    roster: dict[str, str] = {}
    for p in _crew_files():
        agent = _agent_name_of(p)
        if agent:
            roster[agent] = Path(p).name
    return roster


def collect_locks() -> set[str]:
    try:
        return {p.stem for p in _LOCKS_DIR.glob("*.lock")}
    except OSError:
        return set()


def collect_host_status() -> tuple[int | None, set[str]]:
    """(host_pid, {agents running as threads in the fleet host}) from the host's heartbeat file. Empty
    when no host is running — the file is absent or stale (the host rewrites it every ~2s). Lets the TUI
    show host-threaded agents as 'running' even though they have no per-crew process to scan for."""
    try:
        if time.time() - _HOST_STATUS_FILE.stat().st_mtime > _HOST_STALE_S:
            return (None, set())
        data = json.loads(_HOST_STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return (None, set())
    agents = {a for a, s in (data.get("agents") or {}).items() if s in ("running", "starting")}
    return (data.get("pid"), agents)


def collect_serve() -> dict:
    from crewaimeat._home import serve_json_path

    try:
        return json.loads(serve_json_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def collect_node_index(caller_agent: str = "news-fetcher") -> dict[str, dict]:
    """{agent_name: {last_seen, mode}} from one read-only aimeat_agents_list call. Empty on any
    failure — the TUI must render local state even when the node is unreachable."""
    from crewaimeat.aimeat_crew import _aimeat_call

    r = _aimeat_call(caller_agent, "aimeat_agents_list", {}) or {}
    agents = r.get("agents") or (r.get("data") or {}).get("agents") or []
    return {a.get("name"): {"last_seen": a.get("last_seen"), "mode": a.get("mode")} for a in agents if a.get("name")}


def build_snapshot(
    *, caller_agent: str = "news-fetcher", now: datetime.datetime | None = None, node_index: dict | None = None
) -> FleetSnapshot:
    """Gather the full fleet state. Pass `node_index` (even `{}`) to use cached node data and SKIP the
    network call — the TUI does this so its fast local tier never hits the network; only the slow tier
    refreshes the node index. `node_index=None` fetches it here (standalone use). `now` is injectable."""
    now = now or datetime.datetime.now(datetime.timezone.utc)
    cmdlines = collect_cmdlines()
    roster = collect_roster()
    tally, _zombies = tally_processes(cmdlines, set(roster.values()))
    locks = collect_locks()
    serve = collect_serve()
    tunnel = serve_tunnel_agents(serve)
    host_pid, host_agents = collect_host_status()
    if node_index is None:
        node_index = collect_node_index(caller_agent)
    rows = build_rows(
        roster=roster,
        tally=tally,
        locks=locks,
        tunnel=tunnel,
        node_index=node_index,
        now=now,
        host_agents=host_agents,
    )
    return FleetSnapshot(
        serve_pid=serve.get("pid"),
        serve_port=serve.get("port"),
        n_watchdogs=sum(c["watchdog"] for c in tally.values()),
        n_connectors=sum(1 for cl in cmdlines if _CONNECT_RE.search(cl)),
        n_locks=len(locks),
        rows=rows,
        zombies=[r.agent for r in rows if r.status == "zombie"],
        host_pid=host_pid,
    )

"""What this machine keeps for one agent, found by the agent's NAME — and removed when the agent is gone.

Two parties file things per agent on a connector home. The CONNECTOR keeps the credential: a key or
a token, the per-agent config, the line in serve.json. The RUNTIME keeps its own: a start lock, the
README cache, the onboarding marker, the explore counter, and for a spawned agent a pid file, an
audit record per run and a log per run.

Since aimeat-protocol 3f644989c the connector removes ITS files by itself when the node deletes an
agent (the `auth_revoked` frame, reason `deleted`). The runtime's stayed, and the one command meant
to remove them found the agent's identity only in the connector files that had just been deleted.
Measured on this machine 2026-10-12 with the test agent `viive-testi`: seven runtime files on disk,
and `crewaimeat retire viive-testi --apply --forget` answered "serve.json: was not registered" and
"local files: nothing left on this machine".

So the identity is no longer read from the agent's own registration. A home says which owners and
nodes it serves through its OTHER agents (`name#owner@node` in serve.json), and a runtime file is
named `agent_file_stem(identity)`. The files of `<name>` are therefore the ones named for `<name>`
under each owner and node this home serves: exact names, built the way the code that writes them
builds them. Nothing is matched by prefix — `news` never takes `news-fetcher` with it — and whatever
merely STARTS with the name is reported, never deleted.

No crewai, no package import: the idle spawner uses this.
"""

from __future__ import annotations

import glob
import shutil
from pathlib import Path

from crewaimeat import spawn_state
from crewaimeat.agent_manifest import agent_file_stem, agent_local_name


def identities_of(name: str, serve_doc: dict | None, *, known: tuple[str, ...] = ()) -> list[str]:
    """Every identity `name` can have on this home: the name itself, any identity already known for
    it (`known`, and its own line in serve.json while that exists), and `name#owner@node` for each
    owner and node the home serves through another agent."""
    name = agent_local_name(name)
    idents = [name, *[i for i in known if i and agent_local_name(i) == name]]
    suffixes: list[str] = []
    for row in (serve_doc or {}).get("agents") or []:
        gaii = str((row or {}).get("gaii") or "")
        if "#" not in gaii:
            continue
        if (row or {}).get("agent") == name:
            idents.append(gaii)
        suffixes.append(gaii.partition("#")[2])
    idents += [f"{name}#{suffix}" for suffix in suffixes if suffix]
    return list(dict.fromkeys(idents))


def runtime_files(root: Path, idents: list[str]) -> list[Path]:
    """The runtime's own files for these identities, existing ones only. A folder is one entry."""
    home = spawn_state.aimeat_home()
    found: list[Path] = []
    for ident in idents:
        stem = agent_file_stem(ident)
        cache = root / "logs" / ".readme_cache"
        # also by the raw identity: until 2026-10-11 the README cache and the onboarding marker wrote
        # `#` and `@` into the file name, and such files are still on disks
        for spelled in dict.fromkeys((stem, ident)):
            found += [cache / f"{spelled}.hash", cache / f"{spelled}.md"]
            found.append(home / "logs" / ".locks" / f"onboarding_{spelled}.attempt")
        found.append(root / "logs" / ".locks" / f"{stem}.lock")
        found.append(root / "logs" / ".explore" / f"{stem}.seq")
        audit = spawn_state.audit_dir(ident)
        if audit.is_dir():  # one audit file per run, and each run has one log named after it
            found += [spawn_state.log_file(ident, run.stem) for run in sorted(audit.glob("*.json"))]
        if "#" in ident:  # a log whose audit record is gone: `<stem>-<stem>-<run>.log`, both halves exact
            logs = spawn_state.spawn_dir() / "logs"
            found += sorted(logs.glob(f"{glob.escape(stem)}-{glob.escape(stem)}-*.log"))
        found += [audit, spawn_state.pid_file(ident), spawn_state.subscribed_file(ident)]
    seen: set[Path] = set()
    return [p for p in found if p.exists() and not (p in seen or seen.add(p))]


def credential_files(name: str) -> list[Path]:
    """The connector's files for `name`, where they still exist: a key or a token of any owner, and
    the per-agent config folder. `retire --forget` deletes these too; the spawner never does."""
    home = spawn_state.aimeat_home()
    escaped = glob.escape(agent_local_name(name))
    found: list[Path] = []
    for folder, suffix in (("tokens", ".token"), ("keys", ".key")):
        found += sorted((home / folder).glob(f"{escaped}@*{suffix}"))
    found += sorted(p for p in (home / "agents").glob(f"*/{escaped}") if p.is_dir())
    return found


def look_alikes(root: Path, name: str, taken: list[Path]) -> list[Path]:
    """Runtime files that START with the name and belong to no identity this home can name.

    They are never deleted: `<name>_...` may be another agent's. They are what stops a report from
    saying "nothing left" over files a person would recognise at a glance."""
    home, name = spawn_state.aimeat_home(), agent_local_name(name)
    spawn = spawn_state.spawn_dir()
    places = [
        (root / "logs" / ".readme_cache", ""),
        (root / "logs" / ".locks", ""),
        (root / "logs" / ".explore", ""),
        (home / "logs" / ".locks", "onboarding_"),
        (spawn / "audit", ""),
        (spawn / "logs", ""),
        (spawn / "running", ""),
    ]
    known = set(taken)
    inside = [t for t in taken if t.is_dir()]
    out: list[Path] = []
    for folder, prefix in places:
        if not folder.is_dir():
            continue
        for path in sorted(folder.iterdir()):
            rest = path.name[len(prefix) :] if path.name.startswith(prefix) else None
            if rest is None or not (rest.startswith(f"{name}_") or rest.startswith(f"{name}#")):
                continue
            if path not in known and not any(d in path.parents for d in inside):
                out.append(path)
    return out


def _running_worker(pid_file: Path) -> int | None:
    """The pid a pid file names, when that process is alive. None for a worker that has exited."""
    pid = (spawn_state.read_json(pid_file) or {}).get("pid")
    return pid if isinstance(pid, int) and spawn_state.pid_alive(pid) else None


def remove(root: Path, paths: list[Path]) -> tuple[list[str], list[str], list[str]]:
    """Delete `paths`. Returns `(removed, left_running, failed)`, each as text a person can read.

    A PID FILE OF A WORKER THAT IS STILL RUNNING STAYS: it is how the manager finds that process, to
    time it out or to reap it. A pid file whose process has exited is only a leftover and goes with
    the rest."""
    removed: list[str] = []
    running: list[str] = []
    failed: list[str] = []
    for path in paths:
        try:
            shown = path.relative_to(root).as_posix()
        except ValueError:
            shown = path.as_posix()
        if path.suffix == ".pid" and path.parent == spawn_state.running_dir():
            pid = _running_worker(path)
            if pid is not None:
                running.append(f"{shown} (worker pid {pid} is still running)")
                continue
        try:
            shutil.rmtree(path) if path.is_dir() else path.unlink()
            removed.append(shown)
        except OSError as exc:
            failed.append(f"{shown} ({exc.strerror or exc})")
    return removed, running, failed

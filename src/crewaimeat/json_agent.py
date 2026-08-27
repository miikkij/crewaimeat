"""The generic JSON-agent runtime — the crew lives on the node, not on this disk.

A Python crew IS its file: change what the agent does and you edit `crews/<name>_crew.py` and restart
the fleet. For an agent defined as data that is the wrong way round, so this turns it over:

    the definition lives at `crews.registry.<agent_name>` on the node, and the fleet FOLLOWS it.

There is no per-agent definition on disk — only a five-line loader that names the agent and calls
`run_json_agent`. Everything the agent is comes from the node.

HOT RELOAD IS FREE, and that is worth saying because the machinery it replaces is not. `run_crew`
calls `build_domain` ONCE PER TASK ("build crew for task <id>"), so a build that re-reads the
definition means the next task simply uses the new one. No restart, and no push handler needed for
the RELOAD itself. The cost is one memory read per task.

WHAT THE NODE CANNOT SEE, and why this module writes a second key. The Crew tab's "published" state
shows *when the runtime last loaded the definition* — the only honest answer to "is my change live
yet". Nothing else can answer it: the node knows what it stored, not what a fleet somewhere picked
up. So after every load this reports to `crews.runtime.<agent_name>`:

    { loadedAt, revision, ok, errors: [], runtime: "crewaimeat <version>" }

An idle agent would otherwise never report a new revision — it only rebuilds when a task arrives —
so the tab would show yesterday's number and look wrong when it was merely quiet. The node's
`crew.def_updated` wake (spec doc-mtc3ztsbxn9n) exists for exactly that: not to trigger the load,
but to make the load happen NOW so the report is true.

TWO WAYS A PUBLISH CAN BE BAD, and neither may take a working agent down:

  * the read fails (a tunnel blip) — keep running on the last good definition, say so loudly
  * the stored definition is INVALID — same: keep the last good one, and report every error, both to
    stderr and to `crews.runtime.<agent>` so the tab shows WHY it is still on the old revision

An agent that goes dark because somebody saved a typo is worse than an agent running yesterday's
definition while the typo is fixed. What is NOT survivable is having no definition at all on the
first task: then the agent has nothing to be, and it fails with that as the reason.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any

from crewaimeat.crew_def import CrewDocError, build_domain_from_json, validate_crew_doc
from crewaimeat.crew_registry import registry_key
from crewaimeat.memory_tools import read_owner_key

# Where the runtime says what it actually loaded. One key, overwritten — this is a live status, not a
# history, and the history of DEFINITIONS lives in the node's own `.version.N` keys.
RUNTIME_PREFIX = "crews.runtime."


def runtime_key(agent_name: str) -> str:
    return f"{RUNTIME_PREFIX}{agent_name}"


def _errors_of(exc: CrewDocError) -> list[str]:
    return list(getattr(exc, "errors", None) or [str(exc)])


def load_def(agent_name: str) -> tuple[dict, Any]:
    """`(doc, revision)` from the node. Raises `CrewDocError` when there is none, it is not JSON, or
    it does not validate — the caller decides whether it has a last-good to fall back on.

    The stored value is an ENVELOPE (`{version, publishedAt, agent_name, doc, revision, publishedBy}`),
    and a bare document is accepted too so a def written by hand or by `crew_registry` still loads.
    """
    key = registry_key(agent_name)
    value = read_owner_key(agent_name, key)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise CrewDocError([f"{key} is not JSON: {exc}"]) from exc
    if not isinstance(value, dict):
        raise CrewDocError(
            [
                f"{key} holds no crew definition (got {type(value).__name__}). This agent is "
                "registered but not yet defined — publish a definition to that key."
            ]
        )
    doc = value["doc"] if isinstance(value.get("doc"), dict) else value
    revision = value.get("revision") if doc is not value else None
    errors = validate_crew_doc(doc)
    if errors:
        raise CrewDocError(errors)
    return doc, revision


def report_runtime(agent_name: str, *, revision: Any, ok: bool, errors: list[str] | None = None) -> None:
    """Tell the node what this runtime is actually running. Best-effort and never fatal: a status
    write that fails must not take down the agent whose status it describes."""
    from crewaimeat.aimeat_crew import _aimeat_call

    # The installed version, not a constant that would drift from it. The tab reads this to tell one
    # fleet's runtime from another's when a definition loads on one and not the other.
    try:
        from importlib.metadata import version as _pkg_version

        ver = _pkg_version("crewaimeat")
    except Exception:  # noqa: BLE001 — running from a checkout without metadata is not a failure
        ver = "unknown"
    payload = {
        "loadedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "revision": revision,
        "ok": bool(ok),
        "errors": list(errors or []),
        "runtime": f"crewaimeat {ver}",
    }
    try:
        _aimeat_call(
            agent_name,
            "aimeat_memory_write",
            {"key": runtime_key(agent_name), "value": payload, "visibility": "owner", "tags": ["crew-runtime"]},
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[{agent_name}] could not report runtime status: {type(exc).__name__}: {exc}", file=sys.stderr)


class Definition:
    """The agent's current definition, re-read per task, with the last good one kept as the floor."""

    def __init__(self, agent_name: str, doc: dict, revision: Any = None) -> None:
        self.agent_name = agent_name
        self.doc = doc
        self.revision = revision

    def refresh(self) -> dict:
        """The definition to build from — fresh when the node answers with a valid one, last-good
        otherwise. Reports every outcome, because "still on revision 3" is only useful with a why."""
        try:
            doc, revision = load_def(self.agent_name)
        except CrewDocError as exc:
            errs = _errors_of(exc)
            for e in errs:
                print(f"[{self.agent_name}] definition REJECTED: {e}", file=sys.stderr)
            print(
                f"[{self.agent_name}] staying on the last definition that validated "
                f"(revision {self.revision}) — fix {registry_key(self.agent_name)} and the next task "
                "picks it up.",
                file=sys.stderr,
            )
            report_runtime(self.agent_name, revision=self.revision, ok=False, errors=errs)
            return self.doc
        except Exception as exc:  # noqa: BLE001 — transport, not content: same floor, different reason
            print(
                f"[{self.agent_name}] could not read {registry_key(self.agent_name)} "
                f"({type(exc).__name__}: {exc}); staying on revision {self.revision}.",
                file=sys.stderr,
            )
            return self.doc
        if doc != self.doc:
            print(
                f"[{self.agent_name}] definition CHANGED on the node "
                f"(revision {self.revision} -> {revision}) — this task uses the new one.",
                file=sys.stderr,
            )
            self.doc, self.revision = doc, revision
            report_runtime(self.agent_name, revision=revision, ok=True)
        return self.doc

    def build(self, ctx: Any) -> tuple[list, list]:
        return build_domain_from_json(self.refresh(), ctx)


def run_json_agent(agent_name: str, **overrides: Any) -> None:
    """Run the agent whose crew is defined on the node. The five-line loader in `crews/` calls this.

    The definition is loaded once here — the identity half (tags, capabilities, offers, README) is
    pushed to the node at start and cannot follow a mid-flight change — and then re-read per task, so
    the WORK half is always current.
    """
    from crewaimeat.aimeat_crew import run_crew
    from crewaimeat.crew_def import crewspec_from_json

    try:
        doc, revision = load_def(agent_name)
    except CrewDocError as exc:
        errs = _errors_of(exc)
        print(f"[{agent_name}] CANNOT START — {registry_key(agent_name)}:", file=sys.stderr)
        for e in errs:
            print(f"  - {e}", file=sys.stderr)
        print(
            f"[{agent_name}] An agent with no definition has nothing to be. Publish one "
            "(`crewaimeat try` validates it first) and start the fleet again.",
            file=sys.stderr,
        )
        report_runtime(agent_name, revision=None, ok=False, errors=errs)
        raise

    live = Definition(agent_name, doc, revision)
    spec = crewspec_from_json(doc, **overrides)
    spec.build_domain = live.build  # the work half follows the node; the identity half was set above
    print(
        f"[{agent_name}] JSON crew from {registry_key(agent_name)} (revision {revision}): "
        f"{len(doc.get('agents') or [])} agent(s), {len(doc.get('tasks') or [])} task(s) — "
        "re-read per task, so a publish takes effect on the next one.",
        file=sys.stderr,
    )
    report_runtime(agent_name, revision=revision, ok=True)
    run_crew(spec)


# The whole of an agent that lives on the node. Everything else — who it is, what it does, which
# tools it holds — comes from `crews.registry.<agent>`; this file exists because fleet discovery
# scans `crews/*_crew.py` and `crewaimeat doctor` reads AGENT_NAME and build_domain statically.
_LOADER_TEMPLATE = '''"""{agent_name} — a JSON agent. Its crew lives ON THE NODE, not in this repo.

There is no definition here and there is not meant to be one: this file only names the agent. The
crew is at `crews.registry.{agent_name}` and you edit it in AIMEAT under
profile > agents > {agent_name} > Crew. Publish, and the NEXT TASK uses it — no restart, because the
definition is re-read every time a task is built.

`crewaimeat try <def.json> --prompt "…"` runs a definition locally before you publish it.

Run standalone: uv run python crews/{fname}
"""

from __future__ import annotations

from crewaimeat.json_agent import Definition, load_def, run_json_agent

AGENT_NAME = "{agent_name}"

_live: Definition | None = None


def build_domain(ctx):
    """Interpret the definition the node currently holds for this agent.

    `run()` is the real path; this exists so the fleet validator and any direct caller behave exactly
    as they do for a Python crew. The `Definition` is kept so repeated builds reuse the last good one
    instead of going dark when a read fails.
    """
    global _live
    if _live is None:
        doc, revision = load_def(AGENT_NAME)
        _live = Definition(AGENT_NAME, doc, revision)
    return _live.build(ctx)


def run() -> None:
    run_json_agent(AGENT_NAME)


if __name__ == "__main__":
    run()
'''


def loader_source(agent_name: str) -> str:
    """The `crews/<name>_crew.py` for a node-backed agent — five lines of naming, no definition."""
    base = agent_name.strip().replace("-", "_")
    return _LOADER_TEMPLATE.format(agent_name=agent_name.strip(), fname=f"{base}_crew.py")


def write_loader(agent_name: str, crews_dir: str | None = None) -> str:
    """Write the loader and return its path. Refuses to overwrite: a crew file that already exists
    may be a hand-written Python crew, and replacing one with a stub loses the whole agent."""
    from pathlib import Path

    name = agent_name.strip()
    if not name:
        raise ValueError("agent_name is required")
    base = name.replace("-", "_")
    root = Path(crews_dir) if crews_dir else Path(__file__).resolve().parent.parent.parent / "crews"
    path = root / f"{base}_crew.py"
    if path.exists():
        raise FileExistsError(f"{path} already exists — remove it first if you mean to replace it")
    root.mkdir(parents=True, exist_ok=True)
    path.write_text(loader_source(name), encoding="utf-8", newline="\n")
    return str(path)

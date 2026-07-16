"""deploy-app-agent: instantiate an app-embedded crew-def on the OWNER'S OWN fleet.

Slice 1 of "Agent-Bundled Apps" (living spec: Internal workspace ws-mq5vvdgsjwp, plan doc-76ab674).
An AIMEAT app carries a DECLARATIVE crew-def in its manifest (``cortex.agents: [crewDef, ...]``,
the ``crewaimeat.crew_def`` JSON schema). When the owner deploys that app's agent, their node
creates a task on their OWN crew-forge with scope (list form, the AIMEAT convention):

    { kind: "deploy-app-agent", app_id, agent_name, organism_id?, owner? }

crew-forge recognizes it by ``scope.kind`` (NEVER by title — mirror of ``contract_adopt``) and runs
the deterministic pipeline here: resolve the app -> read ``cortex.agents`` -> select the entry ->
VALIDATE (crew_def schema + the ``forge_catalog`` vetted tool set + locally-present skills) ->
OWNER GUARD -> install via the existing declarative path (``crew_registry.install_crew_def``:
materialize + register + launch) -> write ``agents.<deployed>.deploy`` so the app can observe
liveness. ``kind: "undeploy-app-agent"`` reverses it (stop + remove materialized files + flip the
deploy key).

Trust boundary (non-negotiable):
- The crew-def is DATA; nothing app-supplied is ever exec'd/eval'd as code.
- Tool ids are allow-listed against ``forge_catalog`` (the vetted set), and env-preflighted.
- Single-tenant: the deploy is refused unless this fleet's ``AIMEAT_OWNER`` is set and matches the
  task's ``owner`` scope (when present) and the app's owner (when readable). Foreign owner = hard
  reject, logged loudly.
- Every rejection raises :class:`DeployError` at the boundary — no guessing fallback. The crew tool
  wrapper (``build_deploy_domain``) is the ONE dispatcher that renders it into the task report.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import sys
from zoneinfo import ZoneInfo

from crewaimeat import forge_catalog
from crewaimeat.aimeat_crew import BuildContext, _aimeat_call
from crewaimeat.contract_adopt import task_scope
from crewaimeat.crew_def import validate_crew_doc

DEPLOY_KIND = "deploy-app-agent"
UNDEPLOY_KIND = "undeploy-app-agent"

_SLUG_RE = re.compile(r"[^a-z0-9-]+")


class DeployError(ValueError):
    """A deploy/undeploy request was rejected at the boundary. Carries every problem at once."""

    def __init__(self, errors: list[str] | str):
        self.errors = [errors] if isinstance(errors, str) else list(errors)
        super().__init__("deploy-app-agent rejected:\n  - " + "\n  - ".join(self.errors))


# --------------------------------------------------------------------------- #
# Recognition — by scope.kind, never by title (the contract_adopt pattern).
# --------------------------------------------------------------------------- #
def is_deploy_app_agent(task: dict) -> bool:
    return task_scope(task).get("kind") == DEPLOY_KIND


def is_undeploy_app_agent(task: dict) -> bool:
    return task_scope(task).get("kind") == UNDEPLOY_KIND


# --------------------------------------------------------------------------- #
# Naming — the deployed agent is namespaced to the app so fleet names never collide.
# The rule is part of the shared contract (spec §3.4): the app/node can derive the same name.
# --------------------------------------------------------------------------- #
def _slug(s: str) -> str:
    """Coerce to the fleet agent-name charset [a-z0-9-] (collapsed, trimmed)."""
    return _SLUG_RE.sub("-", (s or "").strip().lower()).strip("-")


def deployed_agent_name(app_id: str, agent_name: str) -> str:
    """``<agent_name>-<slug(app_id)>`` — owner scoping is implicit (single-tenant registration)."""
    return f"{_slug(agent_name)}-{_slug(app_id)}"


def deploy_key(deployed: str) -> str:
    """The memory key the app observes for liveness: ``agents.<deployed>.deploy``."""
    return f"agents.{deployed}.deploy"


def _now_iso() -> str:
    try:
        return datetime.datetime.now(ZoneInfo("Europe/Helsinki")).isoformat()
    except Exception:  # noqa: BLE001 — tzdata missing: UTC is a fine timestamp
        return datetime.datetime.now(datetime.timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Validation — the crew_def schema PLUS the app-supplied hardening this surface needs.
# --------------------------------------------------------------------------- #
def validate_app_crew_def(doc: object) -> list[str]:
    """Every problem with an APP-SUPPLIED crew def (empty list == deployable).

    Layered on :func:`crewaimeat.crew_def.validate_crew_doc` (schema, DAG, ``{{ctx.prompt}}``,
    ``llm_profile`` against the active providers file):
    - every tool id must be in ``forge_catalog``'s VETTED set (``crew_def.TOOL_REGISTRY`` is wider;
      an app may only attach what the forge catalog vets) AND pass its env preflight — a tool that
      would only fail at run time is rejected at deploy time;
    - every ``skills`` entry must be a bare name already present under the fleet's local skills dir
      (the owner sanctioned it by putting it there); path-like names are rejected outright.
    """
    if not isinstance(doc, dict):
        return ["crew def must be a JSON object"]
    errors = validate_crew_doc(doc)

    for a in doc.get("agents") or []:
        if not isinstance(a, dict):
            continue
        for tid in a.get("tools") or []:
            cap = forge_catalog.get(tid) if isinstance(tid, str) else None
            if cap is None:
                errors.append(
                    f"tool {tid!r} is not in the vetted forge catalog "
                    f"(allowed: {sorted(c.id for c in forge_catalog.CATALOG)})"
                )
                continue
            ok, reason = forge_catalog.preflight(cap)
            if not ok:
                errors.append(f"tool {tid!r} is not usable on this machine ({reason})")

    skills = doc.get("skills")
    if isinstance(skills, list):
        from crewaimeat.skills import skills_root

        root = skills_root()
        for sk in skills:
            if not isinstance(sk, str) or not sk.strip():
                continue  # validate_crew_doc already flags the type error
            if any(sep in sk for sep in ("/", "\\")) or ".." in sk:
                errors.append(f"skill {sk!r}: path-like skill names are not allowed in an app crew-def")
            elif not (root / sk / "SKILL.md").is_file():
                errors.append(f"skill {sk!r} is not installed on this fleet (no {root / sk / 'SKILL.md'})")
    return errors


# --------------------------------------------------------------------------- #
# Owner guard — single-tenant, defense in depth. The node enforces owner==installer too; the
# fleet refuses on its own evidence regardless.
# --------------------------------------------------------------------------- #
def _fleet_owner() -> str:
    owner = os.getenv("AIMEAT_OWNER", "").strip()
    if not owner:
        raise DeployError(
            "AIMEAT_OWNER is not set on this fleet — the owner guard cannot verify the deploy "
            "target, so the deploy is refused. Set AIMEAT_OWNER in .env and retry."
        )
    return owner


def _guard_owner(claimed: object, source: str) -> str:
    """Refuse unless ``claimed`` (when present) equals this fleet's AIMEAT_OWNER. Returns the owner."""
    owner = _fleet_owner()
    if claimed is not None and str(claimed).strip() and str(claimed).strip() != owner:
        msg = (
            f"FOREIGN OWNER: {source} says owner {str(claimed).strip()!r} but this fleet is "
            f"{owner!r} — deploy-app-agent is single-tenant (own fleet only); hard reject."
        )
        print(f"[app_deploy] {msg}", file=sys.stderr)
        raise DeployError(msg)
    return owner


# --------------------------------------------------------------------------- #
# App resolution — read cortex.agents out of the app record, tolerating the envelope shapes
# aimeat_app_get is known to use (spec open question 2 tracks the canonical path).
# --------------------------------------------------------------------------- #
def _maybe_json(v: object) -> object:
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def extract_cortex_agents(app: object) -> list[dict]:
    """The ``cortex.agents`` list from an app record. Checks the record itself, an ``app``/``value``
    envelope, and a ``manifest`` field (each possibly JSON-encoded). Raises :class:`DeployError`
    when the app embeds no agents — never returns a guessed/partial list."""
    candidates: list[dict] = []
    node = _maybe_json(app)
    if isinstance(node, dict):
        candidates.append(node)
        for k in ("app", "value", "data"):
            inner = _maybe_json(node.get(k))
            if isinstance(inner, dict):
                candidates.append(inner)
    for c in list(candidates):
        manifest = _maybe_json(c.get("manifest"))
        if isinstance(manifest, dict):
            candidates.append(manifest)
    for c in candidates:
        cortex = _maybe_json(c.get("cortex"))
        if isinstance(cortex, dict):
            agents = cortex.get("agents")
            if isinstance(agents, list) and agents:
                return [a for a in agents if isinstance(a, dict)]
    raise DeployError(
        "the app embeds no agents: no non-empty cortex.agents list found on the app record "
        "(checked the record, its app/value/data envelopes, and their manifest fields)"
    )


def select_crew_def(entries: list[dict], agent_name: str) -> dict:
    """The cortex.agents entry whose ``agent_name`` matches. Missing = loud reject naming what IS there."""
    for e in entries:
        if e.get("agent_name") == agent_name:
            return e
    have = sorted(str(e.get("agent_name")) for e in entries)
    raise DeployError(f"no cortex.agents entry with agent_name {agent_name!r} (the app embeds: {have})")


# --------------------------------------------------------------------------- #
# Deploy / undeploy — deterministic, no LLM. `agent` is the fleet identity whose token does the
# AIMEAT I/O (crew-forge).
# --------------------------------------------------------------------------- #
def _scope_field(scope: dict, name: str) -> str:
    v = scope.get(name)
    if not isinstance(v, str) or not v.strip():
        raise DeployError(f"task scope is missing required field {name!r} (the deploy handshake carries it)")
    return v.strip()


def _write_deploy_key(agent: str, deployed: str, app_id: str, agent_name: str, status: str) -> str:
    key = deploy_key(deployed)
    value = {
        "app_id": app_id,
        "agent_name": agent_name,
        "deployed_agent_name": deployed,
        "status": status,
        "ts": _now_iso(),
    }
    r = _aimeat_call(agent, "aimeat_memory_write", {"key": key, "value": value, "visibility": "owner"})
    if r is None:
        # the agent may be live while the key write hit a transient — say so loudly, never mask it
        print(f"[app_deploy] FAILED to write {key} (status={status}) — liveness key is stale", file=sys.stderr)
        return f"WARNING: could not write {key} — the app's liveness view is stale."
    return f"wrote {key} = {status}"


def deploy_app_agent(agent: str, task: dict) -> str:
    """Deterministically deploy an app-embedded crew-def onto THIS fleet. Idempotent: a live
    deployed agent is a no-op (key refreshed). Raises :class:`DeployError` on any rejection."""
    scope = task_scope(task)
    if scope.get("kind") != DEPLOY_KIND:
        raise DeployError(f"not a {DEPLOY_KIND} task (scope.kind={scope.get('kind')!r})")
    app_id = _scope_field(scope, "app_id")
    agent_name = _scope_field(scope, "agent_name")
    _guard_owner(scope.get("owner"), "the task scope")

    app = _aimeat_call(agent, "aimeat_app_get", {"app_id": app_id})
    if app is None:
        raise DeployError(f"aimeat_app_get returned nothing for app {app_id!r} — cannot deploy what I cannot read")
    if isinstance(app, dict):  # defense in depth: the app record's own owner must be this fleet's
        rec = app.get("app") if isinstance(app.get("app"), dict) else app
        for f in ("owner", "owner_name", "ownerName"):
            if rec.get(f):
                _guard_owner(rec.get(f), f"the app record ({f})")
                break

    doc = select_crew_def(extract_cortex_agents(app), agent_name)
    errors = validate_app_crew_def(doc)
    if errors:
        raise DeployError([f"crew-def '{agent_name}' in app '{app_id}' failed validation:", *errors])

    deployed = deployed_agent_name(app_id, agent_name)
    from crewaimeat.forge import is_crew_running

    if is_crew_running(deployed):
        keyline = _write_deploy_key(agent, deployed, app_id, agent_name, "live")
        return f"'{deployed}' is already live — deploy is a no-op (idempotent). {keyline}"

    install_doc = dict(doc)
    install_doc["agent_name"] = deployed  # namespace to the app; owner scoping is implicit
    from crewaimeat.crew_registry import install_crew_def

    report = install_crew_def(install_doc, agent=agent, register=True)
    if report.startswith("INSTALL FAILED"):
        raise DeployError(f"install of '{deployed}' failed: {report}")
    keyline = _write_deploy_key(agent, deployed, app_id, agent_name, "live")
    return f"deployed '{agent_name}' from app '{app_id}' as fleet agent '{deployed}'.\n{report}\n{keyline}"


def undeploy_app_agent(agent: str, app_id: str, agent_name: str, *, task: dict | None = None) -> str:
    """Reverse a deploy: stop the daemon, remove the materialized def + loader, flip the deploy key
    to ``undeployed``. The registered node identity stays (no deregister API today — spec open
    question 1); a later deploy task re-installs over it."""
    if task is not None:
        _guard_owner(task_scope(task).get("owner"), "the task scope")
    else:
        _fleet_owner()
    if not app_id.strip() or not agent_name.strip():
        raise DeployError("undeploy needs both app_id and agent_name")
    deployed = deployed_agent_name(app_id, agent_name)

    from crewaimeat import forge
    from crewaimeat.forge_json import _doc_base

    stopped = forge.stop_crew(deployed)
    root = forge._project_root()
    removed: list[str] = []
    for path in (root / "crews" / forge._fname(deployed), root / "crew_defs" / f"{_doc_base(deployed)}.json"):
        if path.is_file():
            path.unlink()
            removed.append(forge._rel(path))
    keyline = _write_deploy_key(agent, deployed, app_id, agent_name, "undeployed")
    removed_line = f"removed {', '.join(removed)}" if removed else "no materialized files found (already removed)"
    return f"undeployed '{deployed}': {stopped} {removed_line}. {keyline}"


# --------------------------------------------------------------------------- #
# build_domain branch — one agent, one tool, one call (exactly like build_adopt_domain). The
# pipeline is deterministic _aimeat_call work; the LLM only relays the tool's report.
# --------------------------------------------------------------------------- #
def build_deploy_domain(ctx: BuildContext, agent_name: str):
    """crew-forge's build_domain branch for deploy-/undeploy-app-agent tasks."""
    from crewai import Agent, Task
    from crewai.tools import tool

    task_dict = ctx.task or {}
    scope = task_scope(task_dict)
    kind = scope.get("kind")

    if kind == UNDEPLOY_KIND:

        @tool("undeploy_app_agent")
        def _run() -> str:
            """Undeploy the app-embedded agent this task names: stop its daemon, remove its
            materialized files, and flip its deploy key. Deterministic and idempotent."""
            try:
                return undeploy_app_agent(
                    agent_name, str(scope.get("app_id") or ""), str(scope.get("agent_name") or ""), task=task_dict
                )
            except DeployError as exc:
                print(f"[app_deploy] {exc}", file=sys.stderr)
                return f"REJECTED: {exc}"

        verb = "Undeploy"
    else:

        @tool("deploy_app_agent")
        def _run() -> str:
            """Deploy the app-embedded agent this task names onto this fleet: read the app's
            cortex.agents, validate the crew-def, register + launch it, and write its deploy key.
            Deterministic and idempotent — a live agent is a no-op."""
            try:
                return deploy_app_agent(agent_name, task_dict)
            except DeployError as exc:
                print(f"[app_deploy] {exc}", file=sys.stderr)
                return f"REJECTED: {exc}"

        verb = "Deploy"

    deployer = Agent(
        role="App Agent Deployer",
        goal=f"{verb} the app-embedded agent this task names, exactly once, and report the result.",
        backstory=(
            f"You handle {kind or DEPLOY_KIND} tasks: you call your one tool EXACTLY ONCE and report "
            "its result verbatim. The tool is deterministic and idempotent; a REJECTED result is the "
            "final answer, not something to retry."
        ),
        llm=ctx.llm,
        tools=[_run],
    )
    deploy_task = Task(
        description=(
            f"{verb} agent '{scope.get('agent_name')}' of app '{scope.get('app_id')}' on this fleet "
            f"(request: {ctx.prompt}). Call your tool EXACTLY ONCE and report its result verbatim."
        ),
        agent=deployer,
        expected_output="The tool's result line(s), verbatim (deployed/undeployed/no-op/REJECTED).",
    )
    return ([deployer], [deploy_task])

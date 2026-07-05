"""AIMEAT skills-registry consumer — fetch an agent's LINKED skills at crew build.

The node hosts a dedicated skills registry (spec doc-sdie0se): node scope (system library)
+ user scope (owner GHII). The owner links skills to an agent (profile UI or
``aimeat_skill_link``); the agent's refs live at memory key ``agents.{name}.skills``. This
module is the consumer side: at each crew build, fetch the agent's resolved skills
(``GET /v1/agents/{name}/skills`` — a DIRECT authed node call, the storage.py pattern),
materialize each as ``<tmpdir>/<name>/<files>``, validate through the same fail-loud
``load_skills`` path as repo-local skills, and merge with the crew's local skills.

DECISION (recorded in the spec): a dedicated fetch+materialize path, NOT crewai's
experimental ``"@org/name"`` resolver — that resolver is hard-coupled to the CrewAI+ Plus
API, refuses to download when non-interactive (our daemons always are), keeps a global
``~/.crewai/skills`` cache (violates fetch-fresh + per-repo fleet isolation), and falls
back silently between tiers (violates fail-loud).

Failure semantics (the boundary between "environment" and "configuration"):
- Registry unreachable / endpoint missing (404) / no token → LOUD stderr error, return []
  — the crew still runs with its repo-local skills (an owner's node hiccup must not kill
  every task), but the gap is visible in the log, never silent.
- ``unresolved`` refs reported by the node → LOUD stderr error per ref, continue (contract).
- A skill that FETCHES but fails validation when materialized → raises ``SkillLoadError``
  (the node validates at publish, so this means contract drift or corruption — fail loud).

Fetch is fresh per crew build; there is deliberately NO daemon-lifetime cache (an owner
link/unlink takes effect on the very next task). Materialized dirs live under the process
temp dir for the process lifetime (crewai may lazily read resource files after activation).
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any

import requests

from crewaimeat.skills import SkillLoadError, load_skills

_TIMEOUT = 30
_ALLOWED_TOP = ("scripts", "references", "assets")  # + SKILL.md — the contract layout


def _log(agent: str, msg: str) -> None:
    print(f"[{agent}] skills-registry: {msg}", file=sys.stderr, flush=True)


def _safe_rel_path(path: str) -> Path:
    """Validate a registry-supplied file path against the skill layout. Fail loud on escape
    attempts (absolute, drive letter, '..', backslash) and on paths outside the contract
    layout (SKILL.md or scripts|references|assets/...). Defense-in-depth: the node hardened
    its extraction side; we still never write outside the skill dir on OUR side."""
    p = str(path or "")
    if not p or "\\" in p or p.startswith("/") or ":" in p:
        raise SkillLoadError(f"registry skill file path rejected: {path!r}")
    rel = Path(p)
    if any(part in ("..", "") for part in rel.parts):
        raise SkillLoadError(f"registry skill file path rejected: {path!r}")
    if p != "SKILL.md" and rel.parts[0] not in _ALLOWED_TOP:
        raise SkillLoadError(f"registry skill file path outside the skill layout: {path!r}")
    return rel


def materialize_skill(entry: dict, base_dir: Path) -> Path:
    """Write one fetched skill entry as ``base_dir/<name>/<files>`` and return the skill dir.
    ``entry`` is one item of the fetch response's ``skills`` list: ``name`` + ``fileContents``
    ({path -> content}). Fails loud on a missing SKILL.md or a path outside the layout."""
    name = str(entry.get("name") or "")
    files: dict = entry.get("fileContents") or {}
    if "SKILL.md" not in files:
        raise SkillLoadError(
            f"registry skill '{name or entry.get('ref')}': response has no SKILL.md body "
            "(was the fetch made with manifest_only?)"
        )
    skill_dir = base_dir / name if name else None
    if skill_dir is None:
        raise SkillLoadError(f"registry skill entry has no name: {entry.get('ref')!r}")
    for rel_str, content in files.items():
        rel = _safe_rel_path(rel_str)
        target = skill_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(content), encoding="utf-8")
    return skill_dir


def fetch_agent_skills(agent_name: str, owner: str | None = None) -> list:
    """Fetch + materialize + validate the agent's registry-linked skills. Returns a list of
    activated crewai ``Skill`` objects ([] when none / registry unavailable — see the module
    docstring for the loud-vs-raise boundary)."""
    from crewaimeat.generator_tool import _discover_owner, _token

    owner = owner or _discover_owner(agent_name)
    tok, url = _token(agent_name, owner)
    if not tok or not url:
        _log(agent_name, "no token/url — registry skills skipped (is the agent registered + approved?)")
        return []
    try:
        r = requests.get(
            f"{url.rstrip('/')}/v1/agents/{agent_name}/skills",
            headers={"Authorization": f"Bearer {tok}"},
            timeout=_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 — environment failure: loud, crew keeps local skills
        _log(agent_name, f"fetch FAILED ({exc!r}) — running WITHOUT registry skills this task")
        return []
    if r.status_code == 404:
        _log(agent_name, "node has no skills registry (404) — registry skills skipped")
        return []
    if r.status_code != 200:
        _log(agent_name, f"fetch HTTP {r.status_code} — running WITHOUT registry skills this task")
        return []
    payload = r.json() or {}
    data = payload.get("data") or payload

    for u in data.get("unresolved") or []:
        _log(
            agent_name,
            f"UNRESOLVED skill ref {u.get('ref')!r}: {u.get('error')} — linked but NOT loaded; "
            "fix the link (aimeat_skill_link / profile UI)",
        )

    entries = data.get("skills") or []
    if not entries:
        return []
    base = Path(tempfile.mkdtemp(prefix=f"aimeat-skills-{agent_name}-"))
    dirs = [materialize_skill(e, base) for e in entries]
    skills = load_skills(dirs)  # the SAME fail-loud validation path as repo-local skills
    _log(agent_name, f"loaded {len(skills)} registry skill(s): " + ", ".join(s.name for s in skills))
    return skills


def merge_skills(local: list | None, registry: list, agent_name: str = "skills") -> list | None:
    """Union by skill name; the EARLIER source WINS a collision (callers pass sources in
    precedence order — repo-local > owner-linked > workspace-auto; the checkout is the
    developer's explicit override of whatever the registry serves under the same name).
    Returns None when the union is empty — crewai REJECTS ``Agent(skills=[])``."""
    merged = list(local or [])
    have = {s.name for s in merged}
    for s in registry:
        if s.name in have:
            _log(agent_name, f"'{s.name}': a higher-precedence skill shadows this copy (earlier source wins)")
            continue
        have.add(s.name)
        merged.append(s)
    return merged or None


# --------------------------------------------------------------------------- #
# Workspace auto-attach (platform Phase 2c) — a crew opts in to ALSO carry the
# skills published in the workspaces it operates in (CrewSpec.workspace_skills).
# --------------------------------------------------------------------------- #
def workspace_targets(flag: bool | list, record_spaces: Any = None) -> list[tuple[str, str]]:
    """Resolve CrewSpec.workspace_skills into unique (organism_id, ws) pairs.

    ``False`` (default) → no auto-attach. ``True`` → derive from the crew's RESOLVED
    ``record_spaces`` (the workspaces it demonstrably operates in). An explicit list of
    ``{"organism_id", "ws"}`` dicts targets workspaces directly (for a crew that works in a
    workspace without record listening). Order-preserving, deduplicated."""
    if not flag:
        return []
    source = flag if isinstance(flag, list) else (record_spaces or [])
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in source:
        org = (item.get("organism_id") or "") if isinstance(item, dict) else ""
        ws = (item.get("ws") or "") if isinstance(item, dict) else ""
        if org and ws and (org, ws) not in seen:
            seen.add((org, ws))
            pairs.append((org, ws))
    return pairs


def fetch_workspace_skills(agent_name: str, owner: str | None, organism_id: str, ws: str) -> list:
    """Fetch + materialize + validate ONE workspace's skills (2c). The listing returns
    MANIFESTS only (bodies never ride a manifest), so each skill is then resolved by name
    (``GET /v1/skills/{name}?scope=workspace&…``) for its ``fileContents``. Same failure
    boundary as :func:`fetch_agent_skills`: environment failures (node without 2c → 400,
    unreachable, non-200 on a single resolve) are LOUD-and-skip; a skill that RESOLVES but
    fails validation raises ``SkillLoadError``."""
    from crewaimeat.generator_tool import _discover_owner, _token

    owner = owner or _discover_owner(agent_name)
    tok, url = _token(agent_name, owner)
    if not tok or not url:
        _log(agent_name, "no token/url — workspace skills skipped")
        return []
    base_url = url.rstrip("/")
    headers = {"Authorization": f"Bearer {tok}"}
    where = f"workspace {organism_id}/{ws}"
    try:
        r = requests.get(
            f"{base_url}/v1/skills",
            params={"scope": "workspace", "organism": organism_id, "ws": ws},
            headers=headers,
            timeout=_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 — environment failure: loud, crew keeps other skills
        _log(agent_name, f"{where}: listing FAILED ({exc!r}) — workspace skills skipped this task")
        return []
    if r.status_code == 400:
        _log(agent_name, f"{where}: node rejects the workspace scope (400 — 2c not deployed?) — skipped")
        return []
    if r.status_code != 200:
        _log(agent_name, f"{where}: listing HTTP {r.status_code} — workspace skills skipped this task")
        return []
    payload = r.json() or {}
    manifests = (payload.get("data") or payload).get("skills") or []
    if not manifests:
        return []

    base = Path(tempfile.mkdtemp(prefix=f"aimeat-skills-ws-{agent_name}-"))
    dirs: list[Path] = []
    for m in manifests:
        name = str(m.get("name") or "")
        try:
            rr = requests.get(
                f"{base_url}/v1/skills/{name}",
                params={"scope": "workspace", "organism": organism_id, "ws": ws},
                headers=headers,
                timeout=_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001
            _log(agent_name, f"{where}: resolve '{name}' FAILED ({exc!r}) — skill skipped")
            continue
        if rr.status_code != 200:
            _log(agent_name, f"{where}: resolve '{name}' HTTP {rr.status_code} — skill skipped")
            continue
        entry = ((rr.json() or {}).get("data") or {}).get("skill") or {}
        dirs.append(materialize_skill(entry, base))
    if not dirs:
        return []
    skills = load_skills(dirs)  # fail-loud validation, same path as every other skill source
    _log(agent_name, f"{where}: loaded {len(skills)} workspace skill(s): " + ", ".join(s.name for s in skills))
    return skills


def build_ctx_skills(
    agent_name: str,
    owner: str | None,
    local: list | None,
    *,
    registry: bool = True,
    ws_targets: list[tuple[str, str]] = (),
) -> list | None:
    """Compose ctx.skills for ONE crew build, in precedence order:
    repo-LOCAL > owner-LINKED (registry) > WORKSPACE auto-attach. Fetches are fresh per
    call; returns None when nothing is carried (crewai rejects an empty skills list)."""
    merged = merge_skills(local, fetch_agent_skills(agent_name, owner) if registry else [], agent_name)
    if not ws_targets:
        return merged
    ws_skills: list = []
    for org, ws in ws_targets:
        ws_skills.extend(fetch_workspace_skills(agent_name, owner, org, ws))
    return merge_skills(merged, ws_skills, agent_name)

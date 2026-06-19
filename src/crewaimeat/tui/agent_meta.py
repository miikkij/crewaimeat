"""Per-agent config enrichment for the TUI detail pane — ALL local, no network.

Two questions per agent, answered from the repo's own config:
  - which LLM profile + the ORDERED provider→model fallback chain (xai / openrouter / ollama)?
    Source: llm._select_chain + llm_providers.json (the same routing the crews use).
  - does it publish offers, and how many are workflow-compatible?
    Source: offers._CREW_OFFERS + aimeat_crewai.workflow_spec.is_workflow_compatible.

We read the configured chain DIRECTLY (not llm._flatten_endpoints, which drops providers whose API
key env is unset) — the panel should show the full intended priority order regardless of which keys
this machine happens to have.
"""

from __future__ import annotations

import json
import os
import re

# README is a triple-quoted module constant in each crew file; pull it out without importing the
# module. The [[FIGLET:style]["Title"]] banner directive is reduced to its plain title for display.
_README_RE = re.compile(r"README\s*=\s*(?P<q>'''|\"\"\")(?P<body>.*?)(?P=q)", re.DOTALL)
_FIGLET_RE = re.compile(r'\[\[FIGLET:[^\]]*\]\[\s*"?([^"\]]*?)"?\s*\]\]')


def _load_cfg() -> dict:
    from crewaimeat.llm import _providers_file
    p = _providers_file()
    if not p or not os.path.exists(p):
        return {}
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def model_chain(agent: str) -> tuple[str, list[str]]:
    """(profile_label, ['xai:grok-4.3', 'openrouter:gpt-oss-120b:free', ...]) in priority order."""
    from crewaimeat.llm import _select_chain
    cfg = _load_cfg()
    if not cfg:
        return ("(no llm_providers.json)", [])
    providers, profile = _select_chain(cfg, agent)
    labels: list[str] = []
    for prov in providers or []:
        ptype = (prov.get("type") or prov.get("name") or "?")
        for m in prov.get("models") or []:
            mid = m.get("id") if isinstance(m, dict) else m
            if mid:
                labels.append(f"{ptype}:{mid}")
    return (profile, labels)


def read_readme(agent: str) -> str | None:
    """The crew file's README constant (FIGLET banner reduced to plain text), or None if absent."""
    try:
        from crewaimeat.forge import _fname, _project_root
        p = _project_root() / "crews" / _fname(agent)
        text = p.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return None
    m = _README_RE.search(text)
    if not m:
        return None
    body = _FIGLET_RE.sub(r"\1", m.group("body")).strip()
    return body or None


_HOWTO_RE = re.compile(r"How to task me:\**\s*(?P<body>.+?)(?:\n\s*\n|$)", re.DOTALL | re.IGNORECASE)


def how_to_task(agent: str) -> str | None:
    """The crew README's 'How to task me:' line — what to type to drive this agent — or None. This is
    the honest per-agent hint: a task-runner says 'describe the image…', a contract agent says 'scout
    — I run process_moodboards ONCE and fulfil pending requests' (so a free-text brief won't do much)."""
    rm = read_readme(agent)
    if not rm:
        return None
    m = _HOWTO_RE.search(rm)
    if not m:
        return None
    return " ".join(m.group("body").split()) or None


def offer_summary(agent: str) -> tuple[int, int]:
    """(n_offers, n_workflow_compatible) for the agent, from the local offer definitions."""
    try:
        from aimeat_crewai.workflow_spec import is_workflow_compatible

        from crewaimeat.offers import _CREW_OFFERS, crew_offer
    except Exception:  # noqa: BLE001
        return (0, 0)
    metas = _CREW_OFFERS.get(agent) or []
    n_wf = 0
    for meta in metas:
        try:
            if is_workflow_compatible(crew_offer(agent, meta)):
                n_wf += 1
        except Exception:  # noqa: BLE001 — a malformed offer must not break the panel
            pass
    return (len(metas), n_wf)


def offers_detail(agent: str) -> list[tuple[str, str]]:
    """[(offer_id, title), …] for the agent — its crew-task offers PLUS any contract-derived ones."""
    out: list[tuple[str, str]] = []
    try:
        from crewaimeat.offers import _CREW_OFFERS
        for meta in _CREW_OFFERS.get(agent) or []:
            out.append((meta.get("id") or "?", meta.get("title") or meta.get("id") or "?"))
    except Exception:  # noqa: BLE001
        pass
    try:
        from crewaimeat.offers import _OFFER_META, _contracts
        for c in _contracts():
            cid = c.get("id")
            meta = _OFFER_META.get(cid) or {}
            if meta.get("agent") == agent:
                out.append((cid or "?", meta.get("title") or cid or "?"))
    except Exception:  # noqa: BLE001
        pass
    return out


def contracts_for(agent: str) -> list[dict]:
    """[{id, spaces:[{space, mode, fields:[…]}]}] — contracts this agent serves, with each space's
    schema field names (so the panel can show the input/output shape)."""
    out: list[dict] = []
    try:
        from crewaimeat.offers import _OFFER_META, _contracts
    except Exception:  # noqa: BLE001
        return out
    for c in _contracts():
        cid = c.get("id")
        if (_OFFER_META.get(cid) or {}).get("agent") != agent:
            continue
        spaces = []
        for sp in c.get("spaces") or []:
            schema = sp.get("schema") or {}
            fields = list((schema.get("properties") or {}).keys()) if isinstance(schema, dict) else []
            spaces.append({"space": sp.get("space") or sp.get("namespace") or "?",
                           "mode": sp.get("mode") or "?", "fields": fields})
        out.append({"id": cid or "?", "spaces": spaces})
    return out


def identity(agent: str) -> tuple[list[str], dict]:
    """(tags, capabilities) the agent advertises, from the curated fleet registry (or ([], {}))."""
    try:
        from crewaimeat.fleet_identity import identity_for
        ident = identity_for(agent) or {}
        return (list(ident.get("tags") or []), ident.get("capabilities") or {})
    except Exception:  # noqa: BLE001
        return ([], {})


def workflows_for(agent: str) -> list[tuple[str, list[str]]]:
    """[(workflow_id, [step_id, …]), …] — workflows this agent has a step in (local WORKFLOWS)."""
    out: list[tuple[str, list[str]]] = []
    try:
        from crewaimeat.workflow_spec import WORKFLOWS
    except Exception:  # noqa: BLE001
        return out
    for wf in WORKFLOWS.values():
        steps = [s.get("id") or "?" for s in wf.get("steps") or [] if s.get("agent") == agent]
        if steps:
            out.append((wf.get("id") or "?", steps))
    return out


def current_override(agent: str) -> dict | None:
    """The agent's pinned model/profile override, or None (revert = llm_providers.json routing)."""
    try:
        from crewaimeat.llm import agent_override
        return agent_override(agent)
    except Exception:  # noqa: BLE001
        return None


def model_catalogue() -> list[dict]:
    """Every selectable (provider, model) from llm_providers.json — feeds the picker. See llm.available_models."""
    try:
        from crewaimeat.llm import available_models
        return available_models()
    except Exception:  # noqa: BLE001
        return []

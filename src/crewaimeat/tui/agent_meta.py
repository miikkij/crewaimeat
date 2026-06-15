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

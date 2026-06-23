"""Social-briefing — the human-in-the-loop morning-briefing loop (deterministic half).

The loop: a daily SCHEDULE fires a kickoff -> the agent DMs the owner ready-to-paste Grok(X)/Reddit
queries for the topics it tracks -> the owner runs them and pastes the raw results back -> a curation
crew sorts the signals by topic, writes a digest to memory, and replies with an assessment + thread
suggestions. The human brings the social data (no scraping); the agent structures it.

This module holds the DETERMINISTIC parts (config, owner addressing, the kickoff message, the digest
write). The judgement — extracting signals + suggesting threads — is the crew's LLM job (the crew file).

Config lives in session_store under a fixed pseudo-conversation ("_briefing") so it survives restarts
with no read-after-write lag: {topics:[...], conversation_id, schedule_id}. The owner is addressed on the
federated inbox; same-owner delivery is ungated, so the agent reaches its own owner directly.
"""

from __future__ import annotations

import sys

from crewaimeat import dm, orchestrator, session_store
from crewaimeat.aimeat_crew import _aimeat_call

AGENT_NAME = "social-briefing"
_CONFIG_CONV = "_briefing"  # fixed session_store conversation that holds this agent's own config
_CONFIG_KEY = "config"
KICKOFF_MARKER = "SOCIAL_BRIEFING_KICKOFF"  # scheduled task_description carries this so build_domain detects it

DEFAULT_TOPICS = ["AI agents", "multi-agent systems", "agent memory", "CrewAI", "AIMEAT"]


# ── config (durable, local) ──────────────────────────────────────────────────
def get_config(agent: str = AGENT_NAME) -> dict:
    cfg = session_store.session_get(agent, _CONFIG_CONV, _CONFIG_KEY) or {}
    if not cfg.get("topics"):
        cfg["topics"] = list(DEFAULT_TOPICS)
    return cfg


def set_config(agent: str, **changes) -> dict:
    cfg = get_config(agent)
    cfg.update({k: v for k, v in changes.items() if v is not None})
    session_store.session_set(agent, _CONFIG_CONV, _CONFIG_KEY, cfg)
    return cfg


def set_topics(agent: str, topics: list[str]) -> list[str]:
    clean = [t.strip() for t in topics if t and t.strip()][:12]
    set_config(agent, topics=clean or list(DEFAULT_TOPICS))
    return get_config(agent)["topics"]


# ── owner addressing ─────────────────────────────────────────────────────────
def own_gaii(agent: str = AGENT_NAME) -> str | None:
    """This agent's full GAII (name#owner@node) from the live roster."""
    for a in orchestrator.list_node_agents(agent):
        if a.get("name") == agent and a.get("gaii"):
            return a["gaii"]
    return None


def owner_gaii(agent: str = AGENT_NAME) -> str | None:
    """The owner's federated address (owner@node) — derived from this agent's own GAII. Same owner, so
    DMing it is consented (ungated)."""
    g = own_gaii(agent)
    return g.split("#", 1)[1] if g and "#" in g else None


# ── the kickoff message (deterministic — the queries come from the topics) ────
def build_kickoff(topics: list[str], date_str: str) -> str:
    """The DM body: per-topic copy-paste queries for Grok(X) + Reddit, and how to paste back."""
    grok = "\n".join(
        f"{i + 1}. `What are people on X saying this week about {t}? Summarise the top posts with links "
        f"and the overall sentiment.`"
        for i, t in enumerate(topics)
    )
    reddit = "\n".join(f"{i + 1}. `{t} site:reddit.com` (sort by Top, past week)" for i, t in enumerate(topics))
    return (
        f"**☕ Morning briefing — {date_str}**\n\n"
        f"Tracking: {', '.join(topics)}.\n\n"
        "Run these and paste the raw results back here — I'll sort them by topic, flag what matters, and "
        "suggest threads worth joining. Rough copy-paste is fine.\n\n"
        f"**Grok / X**\n{grok}\n\n"
        f"**Reddit**\n{reddit}\n\n"
        "Paste each block's output back in this thread when ready. 👇"
    )


def send_kickoff(agent: str, date_str: str) -> bool:
    """Send the kickoff to the owner — reply in the standing briefing thread if we have one, else open it
    (and remember the conversation id). Returns True if delivered."""
    to = owner_gaii(agent)
    if not to:
        print(f"[{agent}] send_kickoff: could not resolve owner gaii", file=sys.stderr)
        return False
    cfg = get_config(agent)
    body = build_kickoff(cfg["topics"], date_str)
    conv = cfg.get("conversation_id")
    if conv:
        res = dm.dm_reply(agent, to, body, conversation_id=conv)
    else:
        res = dm.dm_send(agent, to, body, subject="Morning briefing")
    conv_id = orchestrator._conv_id(res) if res else None
    if conv_id and conv_id != conv:
        set_config(agent, conversation_id=conv_id)
    return bool(res)


# ── digest write (the structured output is consumable by a marketing organism) ─
def write_digest(agent: str, date_str: str, digest_text: str, topics: list[str]) -> bool:
    """Persist the curated digest to owner memory: one dated digest key + a 'latest' pointer. (Per-topic
    keys / organism-workspace sync are a follow-up — the shell-callable surface has memory, not workspace_*.)"""
    value = {"date": date_str, "topics": topics, "digest": digest_text[:20000]}
    ok = True
    for key in (f"social.briefing.digest.{date_str}", "social.briefing.latest"):
        res = _aimeat_call(agent, "aimeat_memory_write", {"key": key, "value": value, "visibility": "owner"})
        ok = ok and bool(res)
    return ok

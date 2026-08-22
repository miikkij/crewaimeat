"""Auto-generated brain stub — do not edit. The behavior lives in the brain (crewaimeat.brains), edited in the agency cockpit; this stub only launches it.
Agent: news-watcher
"""

from crewaimeat.brains import run_brain

AGENT_NAME = "news-watcher"

# ── This agent's own declaration ─────────────────────────────────────────────────────────────
# The single source for what this agent is: its model routing, how it is discovered, and what it
# promises. These used to live in three central lists (fleet_identity.py / llm_providers.json /
# offers.py) that nothing kept in step, so an agent could — and did — come online missing from
# all of them. crewaimeat.agent_manifest reads these statically; the lists are derived.
LLM_PROFILE = "content-free"


def run() -> None:
    run_brain(AGENT_NAME)


if __name__ == "__main__":
    run()

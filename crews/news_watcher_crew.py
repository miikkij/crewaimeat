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

# ── This agent's own declaration ─────────────────────────────────────────────────────────────
# The single source for what this agent is — crewaimeat.agent_manifest reads it statically.
TAGS = ["news-watch", "brain-stub", "role.task-runner"]
CAPABILITIES = {
    "technical": [{"name": "news-watch", "type": "skill"}],
    "domain": [
        "watches news sources and reports what changed",
        "behaviour lives in its JSON brain (crewaimeat.brains), edited in the agency cockpit",
        "pinned to a local Ollama model by a per-agent override, so it costs nothing to run",
    ],
    "languages": ["fi", "en"],
}


# What this agent advertises it can do. The `ask` states NEGATIVE SCOPE on purpose — what it
# will NOT do is the half a buyer needs and the half an author skips.
OFFERS = [
    {
        "id": "watch-news",
        "title": "Watch news sources and report what changed",
        "ask": "I watch the sources configured in my brain and report what is new since last time. My "
        "behaviour lives in a JSON brain edited in the agency cockpit, so what I watch is "
        "configuration, not code. I do NOT write articles and do not publish to the newspaper.",
        "example": "configured in the cockpit; runs on a local model, so it costs nothing",
        "cost": "cheap",
        "latency": "minutes",
        "repeatability": "accumulative",
        "verification": "ungated",
        "consequences": [],
        "sample": (
            '# Fusion Startups — New Developments (week of June 24, 2026)\n\n**Helion Energy raises $465M at $15.5B valuation** (June 4)\n- Series G led by Thrive Capital; new investors include Ford Motor Company\'s Bill Ford, Alta Park Capital, BoxGroup, Lux Capital. Sam Altman owns ~1/3 of the company.\n- Total funding now $1.5B. Microsoft PPA for 2028 electricity delivery. Orion 50MW plant under construction in Malaga, WA.\n- Operating Polaris (7th-gen, 60-ft fusion device); building smaller "Tiny Merge" testbed for faster iteration.\n- Source: https://www.geekwire.com/2026/helion-hits-15-5b-valuation-with-465m-in-new-cash-to-commercialize-fusion-this-decade/\n\n…'
        ),
    },
]


def run() -> None:
    run_brain(AGENT_NAME)


if __name__ == "__main__":
    run()

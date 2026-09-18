"""aimeat-agency 2.0 — a thin local runtime for people who describe agents instead of coding them.

The agent's definition lives on the AIMEAT node (`crews.registry.<agent>`); this package only does what
needs the person's own machine: run the agents, keep the OpenRouter key, connect to one or more AIMEAT
instances, and show whether everything is healthy. Spec: docs/internal/2026-09-18-agency-2-spesifikaatio.md
(local). The 0.8.x cockpit (`crewaimeat.agency`) stays beside it until 2.0 ships.
"""

import os as _os

# The environment this process was STARTED with, captured before anything imports crewai/aimeat_crewai
# (whose bare `load_dotenv()` fills in a stray .env). `engine.openrouter_only` uses it.
LAUNCH_ENV = frozenset(_os.environ)

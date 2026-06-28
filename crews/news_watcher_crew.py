"""Auto-generated brain stub — do not edit. The behavior lives in the brain (crewaimeat.brains), edited in the agency cockpit; this stub only launches it.
Agent: news-watcher
"""

from crewaimeat.brains import run_brain

AGENT_NAME = "news-watcher"


def run() -> None:
    run_brain(AGENT_NAME)


if __name__ == "__main__":
    run()

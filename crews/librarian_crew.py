"""librarian — the fleet's index, reuse-scout and freshness watch.

Ask it "what do we already have on X?" and it searches every same-owner crew's deliverables
(owner_scope memory), classifies the best matches by shelf-life (permanent / slow / fast), drops
junk, and reports what is reusable — so you can avoid re-running expensive work. See
docs/librarian-design.md. It is also the home of the contribute_to_library scaffold hook and
(later) the aggregator/janitor.

Register first, then approve:
  npx aimeat@latest connect add --agent librarian --mode task-runner --url https://aimeat.io --owner <your-aimeat-account>

Run: uv run python crews/librarian_crew.py
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.librarian import make_librarian_tools

AGENT_NAME = "librarian"

README = """[[FIGLET:slant]["LIBRARIAN"]]

# librarian — what do we already have?

Ask me about a topic and I search every crew's finished deliverables, rank the best matches by how
well they fit and how long they stay reliable (permanent / slow / fast to go stale), drop the noise,
and tell you what is worth reusing — so the fleet doesn't redo expensive work.

## How to task me
Queue a topic or need, e.g.:
- `What do we already have on board-game cafe feasibility?`
- `Do we have market research on the subscription-box idea?`
"""


def build_domain(ctx: BuildContext) -> tuple[list[Agent], list[Task]]:
    librarian = Agent(
        role="Fleet Librarian",
        goal="Find existing same-owner work relevant to the request and report what is reusable, noting shelf-life",
        backstory=(
            "You keep a map of everything the fleet has produced. When asked about a topic, you consult "
            "the index, judge which existing deliverables actually fit the need, and flag any that are "
            "time-sensitive and may need re-verifying. You never invent results — you report what exists."
        ),
        tools=make_librarian_tools(AGENT_NAME),
        llm=ctx.llm,
        verbose=True,
    )
    task = Task(
        description=(
            f"{ctx.today}\n\nThe owner wants to know what the fleet already has for this need:\n{ctx.prompt}\n\n"
            "Call consult_librarian(need=...) with a concise description of the need. Then report the "
            "matches it returns verbatim-ish: for each, the owning crew, topic, shelf-life, the EXACT "
            "memory key (always include it — the owner needs it to fetch the deliverable), a one-line "
            "summary, and whether it looks directly reusable or should be re-verified (old 'fast' items). "
            "If nothing relevant exists, say so plainly so the owner knows to commission the work."
        ),
        expected_output="A short list of reusable existing deliverables (or a clear 'nothing relevant yet').",
        agent=librarian,
    )
    return [librarian], [task]


def run() -> None:
    # Knowledge indexing / classification — run cool for consistency.
    run_crew(CrewSpec(agent_name=AGENT_NAME, build_domain=build_domain, readme_md=README, temperature=0.25))


if __name__ == "__main__":
    run()

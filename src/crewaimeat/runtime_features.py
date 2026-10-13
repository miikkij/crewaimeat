"""What this runtime does with a crew definition beyond its agents and tasks, by name.

ONE LIST, SAID IN TWO PLACES. `crew.menu` answers it for an agent (crew_invoke), and the serve daemon
is started with it in `AIMEAT_RUNTIME_FEATURES` (serve_guard), which the connector sends to the node
for the computer. The node's new-agent page offers a choice only where the runtime behind it honours
the field: an older runtime ignores `record_spaces`, and an agent made with it there would never be
woken, with nothing on any screen to say why. Both read this tuple, so a computer and its agents
cannot say different things.

A name goes in here in the same change that makes the field work, never before: a runtime that does
not send one is read as not having it. No imports: serve_guard must stay as light as it is.
"""

from __future__ import annotations

FEATURES = (
    "record_spaces",  # a definition names workspace spaces whose records become tasks (record_wake)
    "record_backfill",  # a record space may ask for the records already there: {"backfill": true}
    # A definition crew can be a workflow step: its deliverable is published at the task scope's
    # `deliverable_key`, and the scope's `var.*` entries reach the crew as data (crewaimeat.task_scope).
    # The home writes workflow signals into a definition's offer only for a runtime that says this.
    "task_scope",
)

FEATURES_ENV = "AIMEAT_RUNTIME_FEATURES"  # comma-separated, read by the connector at daemon start

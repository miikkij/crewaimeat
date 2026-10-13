"""What this runtime does with a crew definition beyond its agents and tasks, by name.

ONE LIST, SAID IN TWO PLACES. `crew.menu` answers it for an agent (crew_invoke), and the serve daemon
is started with it in `AIMEAT_RUNTIME_FEATURES` (serve_guard), which the connector sends to the node
for the computer. The node's new-agent page offers a choice only where the runtime behind it honours
the field: an older runtime ignores `record_spaces`, and an agent made with it there would never be
woken, with nothing on any screen to say why. Both read this tuple, so a computer and its agents
cannot say different things.

A name goes in here in the same change that makes the field work, never before: a runtime that does
not send one is read as not having it. Nothing heavy is imported: serve_guard must stay as light as
it is.

SOME OF IT DEPENDS ON THE COMPUTER. The browser needs a Chromium, and the `playwright` package
that comes with the install does not bring one: somebody runs `uv run playwright install chromium`
on the computer. So `features()` is asked, not the tuple read: a computer without a browser must not
say it has one, or the page would offer an agent that cannot do its work.
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

# The `browser` tool opens only the hostnames the definition names in `browser_domains`
# (crewaimeat.fenced_browser). Said only by a computer that has a browser to run it with.
BROWSER_FEATURE = "browser_domains"

FEATURES_ENV = "AIMEAT_RUNTIME_FEATURES"  # comma-separated, read by the connector at daemon start


def browser_installed() -> bool:
    """True when this computer has the `playwright` package and a Chromium it installed.

    Read from the disk: no driver is started and nothing of playwright is imported, so asking costs a
    daemon start nothing. It can be wrong one way (a Chromium of another playwright version is
    there): the tool then says in its own answer that the browser could not run, and why.
    """
    import importlib.util
    import os
    import sys
    from pathlib import Path

    spec = importlib.util.find_spec("playwright")
    if spec is None or not spec.origin:
        return False
    chosen = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if chosen == "0":
        home = Path(spec.origin).parent / "driver" / "package" / ".local-browsers"
    elif chosen:
        home = Path(chosen)
    elif sys.platform == "win32":
        home = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "ms-playwright"
    elif sys.platform == "darwin":
        home = Path.home() / "Library" / "Caches" / "ms-playwright"
    else:
        home = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "ms-playwright"
    try:
        return any(entry.name.startswith("chromium") for entry in home.iterdir())
    except OSError:
        return False


def features() -> tuple[str, ...]:
    """What THIS computer's runtime honours: the list above, and the browser where one is installed."""
    return (*FEATURES, BROWSER_FEATURE) if browser_installed() else FEATURES

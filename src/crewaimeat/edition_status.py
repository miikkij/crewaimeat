"""ONE status record per edition, patched field-by-field by the six pipeline steps.

Before this, the only way to ask "where is tonight's paper?" was to list the whole `news.<date>.*`
prefix and infer progress from which keys had appeared. Now there is a record that says it:

    news.<date>.<edition>.status
    { fetch, writeA, writeB, spaceWeather, features, editorial, updatedAt }

Each step writes ONLY its own field, via PATCH /v1/memory/:key (RFC 7386 merge patch): the node
reads, merges, and compare-and-swaps, retrying on a lost swap, so two desks finishing at the same
moment cannot lose each other's field.

THE TRAP THIS MODULE EXISTS TO CLOSE: memory is keyed by the WRITER. Six agents patching "one key"
WITHOUT `owner_scope` produce SIX records, one per agent namespace, and nothing merges them — a
failure with no error message, because every individual write succeeds. So `owner_scope: True` is
not optional here and is never a caller's argument to forget; it is baked into `_patch` below.
It requires the `memory:write-as-owner` scope (or `*`) on each of the six agents — a patch without
the scope is refused 403, which is the loud half of the failure; the silent half is the one above.

Verified against aimeat.io 2026-08-09: three agents patched distinct fields of one owner-scoped key,
the version went 1 -> 2 -> 3, and an owner-scope list returned exactly ONE record under the owner
GHII with all three fields present.
"""

from __future__ import annotations

import datetime
import sys
from contextlib import contextmanager

from crewaimeat.aimeat_crew import _aimeat_rest

# The six steps of the evening workflow, in run order. The field names are the CONTRACT the record
# publishes — a reader (a dashboard, the workflow inspector, the owner) matches on these, so they
# are deliberately step-shaped, not agent-shaped: `writeA` is the Desk A step whichever agent runs it.
STEPS: tuple[str, ...] = ("fetch", "writeA", "writeB", "spaceWeather", "features", "editorial")

QUEUED, RUNNING, DONE, FAILED = "queued", "running", "done", "failed"


def status_key(date: str, edition: str) -> str:
    return f"news.{date}.{edition}.status"


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _patch(agent_name: str, date: str, edition: str, patch: dict) -> bool:
    """Merge-patch the edition's status record. `owner_scope` is fixed True — see the module note.

    Best-effort by design: this record REPORTS on the pipeline, it is not part of the newspaper. A
    status write that fails must not take an edition down with it, so it logs loud and returns False
    rather than raising. The step's own success/failure is still carried by its deliverable and its
    workflow signal — the honest sources — so a missing status field costs visibility, not truth."""
    key = status_key(date, edition)
    data = _aimeat_rest(
        agent_name,
        "PATCH",
        f"/v1/memory/{key}",
        {"patch": {**patch, "updatedAt": _now()}, "owner_scope": True, "visibility": "owner"},
    )
    if data is None:
        print(f"[{agent_name}] status patch {key} {patch} FAILED — the edition continues unaffected", file=sys.stderr)
        return False
    return True


def seed_status(agent_name: str, date: str, edition: str) -> bool:
    """Put every step at "queued" at the START of an edition, so the record answers "what is still
    coming?" and not only "what has already happened".

    Sent as a PATCH, not a plain write, and that matters: the fetch step is retried like any other,
    and a plain write on a re-run would erase the desks' fields with a screenful of "queued". A merge
    patch of the not-yet-run steps is idempotent for the record as a whole, and the fields it does
    overwrite (a step that ran, then the fetch re-ran) are re-stamped by that step's own next patch."""
    return _patch(agent_name, date, edition, dict.fromkeys(STEPS, QUEUED))


def set_status(agent_name: str, date: str, edition: str, step: str, state: str) -> bool:
    """Set ONE step's field. Rejects an unknown step at the boundary rather than quietly minting a
    seventh field that no reader is looking for."""
    if step not in STEPS:
        raise ValueError(f"unknown pipeline step {step!r} — expected one of {', '.join(STEPS)}")
    return _patch(agent_name, date, edition, {step: state})


class StepReport:
    """Handed to the body of `step_status` so a stage that reports failure by RETURN VALUE rather
    than by raising can still say so. The quiz builder returns "quiz=SKIPPED(…)" by design — it
    refuses to fabricate a quiz from articles it cannot read — and a status record that called that
    "done" would be lying in the one place built to be believed."""

    __slots__ = ("state",)

    def __init__(self) -> None:
        self.state = DONE

    def fail(self) -> None:
        self.state = FAILED


@contextmanager
def step_status(agent_name: str, date: str, edition: str, step: str):
    """Wrap a pipeline stage: `running` on entry, `done` on a clean return, `failed` if it raises.

        with step_status(agent_name, date, edition, "fetch") as st:
            ...
            st.fail()   # only for a stage that reports failure by returning, not by raising

    The exception is always re-raised — the status record follows the run, it never absorbs it."""
    set_status(agent_name, date, edition, step, RUNNING)
    report = StepReport()
    try:
        yield report
    except BaseException:
        set_status(agent_name, date, edition, step, FAILED)
        raise
    set_status(agent_name, date, edition, step, report.state)


def read_status(agent_name: str, date: str, edition: str) -> dict:
    """The edition's status record as a plain dict ({} when it does not exist yet)."""
    data = _aimeat_rest(agent_name, "GET", f"/v1/memory/{status_key(date, edition)}?owner_scope=true")
    value = (data or {}).get("value")
    return value if isinstance(value, dict) else {}

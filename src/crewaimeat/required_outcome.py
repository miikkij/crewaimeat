"""What a definition says a run must leave behind, checked in code before the task is closed.

A MODEL CAN SAY IT WROTE. Nothing looked. `varoituskartta-vahti` ended done three times on 2026-11-01
with nothing written to `varoituskartta.c.*`: the model narrated the write, a turn with no tool call is
CrewAI's final answer, and the scaffold published that answer and completed the task. So a definition
can name the outcome, as the top-level

    "required_outcome": {"memory_prefix": "varoituskartta.c.", "namespace": "agent",
                         "none_when": "NOTHING QUEUED"}

and a run that changed no record under the prefix is FAILED, unless its answer is exactly `none_when`,
the run's own "there was nothing to do". The shape is agreed with aimeat-protocol (2026-11-02); the node
stores the field unread (`CrewDefSchema` passes unknown top-level fields through).

WHOSE MEMORY. `namespace` says it and has one value today, `agent`: the agent's own, which is where the
`memory` tool writes. A chat or a sibling writing the same key lands elsewhere and does not count.

NO CLOCK. The node's list has no time filter, and comparing its `updated_at` with this computer's clock
turns a real write into "no record" on a slow clock. A write adds one to the record's version
(aimeat services/memory-write.ts) whether or not the value changed, so the versions are read before the
crew is built and again at the close, and any difference is a write. Tasks running side by side share
the agent's memory, so one's write counts for both: the same per-agent reach as the refusal check.

THE WRITE ITSELF NEED NOT BE THE MODEL'S. Writing records is not judgement. Given `"write_answer":
"public"` (or "owner", the visibility the records get), the last task's answer is the JSON list
[{"key", "value"}, ...] and code writes it: no tool, no model turn to get wrong. In the agent's run of
2026-11-01 the list itself was right and the turn that was to save it answered with the list and made no
call. A key outside `memory_prefix` is not written, whatever the answer says, because the answer is a
model's and the prefix is what the owner approved. Written beats empty: every good record is written,
and then anything that could not be, a refusal of a reserved key included, fails the run by name.

WHEN IT CANNOT BE CHECKED the task completes and says so, as the refusal check does: failing a good run
because the check failed would trade one silent lie for another. A refused read is not that case: the
transport counted it, and the refusal check fails the run with the permission named.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from urllib.parse import quote

# The agent's own memory. Another value is added when something needs it and the check can read it.
NAMESPACES = ("agent",)
VISIBILITIES = ("public", "owner")  # what the `memory` tool offers too
# The scaffold itself writes under these on every run (the deliverable, offers, statistics), so a prefix
# here would be satisfied by a run that did nothing.
_SCAFFOLD_PREFIXES = ("crews.", "agents.")


def validation_errors(value: object) -> list[str]:
    """What is wrong with a definition's `required_outcome`; [] when it is absent or fine."""
    if value is None:
        return []
    if not isinstance(value, dict):
        return ['required_outcome: must be an object {"memory_prefix", "namespace", "none_when", "write_answer"}']
    errors: list[str] = []
    unknown = sorted(set(value) - {"memory_prefix", "namespace", "none_when", "write_answer"})
    if unknown:
        errors.append(f"required_outcome: unknown field(s) {', '.join(unknown)}")
    prefix = value.get("memory_prefix")
    if not isinstance(prefix, str) or not prefix.strip() or any(c.isspace() or c in "{}" for c in prefix):
        errors.append("required_outcome.memory_prefix: must be a key prefix with no spaces or placeholders")
    elif prefix.startswith(_SCAFFOLD_PREFIXES):
        errors.append(
            f"required_outcome.memory_prefix: {prefix!r} is where the scaffold writes on every run, so every "
            "run would pass; name the keys the work itself writes"
        )
    if value.get("namespace") not in NAMESPACES:
        errors.append(f"required_outcome.namespace: must say whose memory, one of {NAMESPACES}")
    none_when = value.get("none_when")
    if none_when is not None and (not isinstance(none_when, str) or not none_when.strip()):
        errors.append("required_outcome.none_when: must be the exact words of a run with nothing to do")
    if value.get("write_answer") is not None and value["write_answer"] not in VISIBILITIES:
        errors.append(f"required_outcome.write_answer: must be the records' visibility, one of {VISIBILITIES}")
    return errors


def record_versions(agent_name: str, prefix: str) -> dict[str, object] | None:
    """{key: version} of the agent's own records under `prefix`; None when the node gave no such answer."""
    from crewaimeat.aimeat_crew import _aimeat_rest

    path = f"/v1/memory?prefix={quote(prefix, safe='')}&include=meta"
    res = _aimeat_rest(agent_name, "GET", path, retries=2, return_error=True)
    if not isinstance(res, dict) or res.get("ok") is False or not isinstance(res.get("items"), list):
        return None
    return {str(i["key"]): i.get("version") for i in res["items"] if isinstance(i, dict) and i.get("key")}


def _answer_of(task: object) -> str:
    output = getattr(task, "output", None)
    return str(getattr(output, "raw", None) or output or "").strip()


def _records_in(answer: str) -> list | None:
    """The answer's JSON list, without the code fence a model may put around it; None when it is not one."""
    text = answer.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        records = json.loads(text)
    except ValueError:
        return None
    return records if isinstance(records, list) and records else None


def write_answer(agent_name: str, task_id: str, required: dict, answer: str) -> None:
    """Write the answer's records under the prefix, in code. Anything that could not be written fails the run."""
    from crewaimeat.aimeat_crew import _aimeat_call
    from crewaimeat.lifecycle import note_outcome

    prefix = required["memory_prefix"]
    records = _records_in(answer)
    if records is None:
        note_outcome(
            task_id,
            f'The run\'s answer was not the JSON list of {{"key", "value"}} objects its definition asks for, so '
            f"nothing was written under {prefix}. What the model answered is the task's deliverable.",
            failed=True,
        )
        return
    written: list[str] = []
    problems: list[str] = []
    for record in records:
        key = record.get("key") if isinstance(record, dict) else None
        if not isinstance(key, str) or "value" not in record:
            problems.append(f'{record!r:.80} is not a {{"key", "value"}} object')
        elif not key.startswith(prefix) or key == prefix or any(c.isspace() for c in key):
            problems.append(f"{key!r} is outside {prefix}")
        else:
            payload = {"key": key, "value": record["value"], "visibility": required["write_answer"]}
            res = _aimeat_call(agent_name, "aimeat_memory_write", payload, return_error=True)
            if res is None:
                problems.append(f"{key}: the write got no answer")
            elif isinstance(res, dict) and res.get("ok") is False:
                err = res.get("error")
                problems.append(f"{key}: the node answered {err.get('code') if isinstance(err, dict) else err}")
            else:
                written.append(key)
    print(f"[{agent_name}] wrote {len(written)} record(s) under {prefix} from the answer", file=sys.stderr)
    if problems:
        note_outcome(
            task_id,
            f"{len(written)} record(s) of the run's answer were written under {prefix} and these were not: "
            + "; ".join(problems),
            failed=True,
        )


def check(agent_name: str, task_id: str, required: dict, before: dict | None, answer: str) -> None:
    """Record this run's outcome for the completion to act on, when it has something to say."""
    from crewaimeat.lifecycle import has_failed_outcome, note_outcome

    if has_failed_outcome(task_id):  # the run has already ended as failed, with its own reason
        return
    prefix = required["memory_prefix"]
    none_when = required.get("none_when")
    if none_when and answer == none_when.strip():
        print(f"[{agent_name}] required outcome: the run had nothing to do ({none_when})", file=sys.stderr)
        return
    if required.get("write_answer"):
        write_answer(agent_name, task_id, required, answer)
        if has_failed_outcome(task_id):
            return
    after = record_versions(agent_name, prefix)
    if before is None or after is None:
        print(f"[{agent_name}] required outcome under {prefix} could not be read; completing", file=sys.stderr)
        note_outcome(task_id, f"Crew finished. Whether it wrote under {prefix} could not be checked.")
        return
    written = sorted(k for k, version in after.items() if before.get(k) != version)
    if written:
        print(f"[{agent_name}] required outcome: {len(written)} record(s) written under {prefix}", file=sys.stderr)
        return
    allowed = f', and its answer was not "{none_when}"' if none_when else ""
    message = (
        f"The run finished without writing any record under {prefix} in this agent's own memory, which its "
        f"definition requires{allowed}. What the model answered is the task's deliverable."
    )
    print(f"[{agent_name}] required outcome MISSING -> {message}", file=sys.stderr)
    note_outcome(task_id, message, failed=True)


def checked_close(
    agent_name: str, task_id: str, required: dict, before: dict | None, last_task: object, close: Callable
) -> Callable:
    """The finalize callback: `close` (lifecycle.complete_callback), with this run's check before it."""

    def _cb(out) -> None:
        try:
            check(agent_name, task_id, required, before, _answer_of(last_task))
        except Exception as exc:  # noqa: BLE001 — the check must never be what leaves a task open
            from crewaimeat.lifecycle import note_outcome

            print(f"[{agent_name}] required outcome check raised ({exc!r}); completing", file=sys.stderr)
            note_outcome(
                task_id, f"Crew finished. Whether it wrote under {required['memory_prefix']} could not be checked."
            )
        close(out)

    return _cb

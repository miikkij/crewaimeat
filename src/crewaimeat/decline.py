"""A run that turned the request down ends as DECLINED, not as done.

Measured 2026-10-02 on a hosted place: "add a contact to the CRM" to a concierge with no CRM tool. It
answered "I have no access to any CRM system", which was true, and the task was marked DONE, which was
not: the person's list said the contact was handled. The scaffold completes every run whose kickoff
returns, and a polite refusal is a kickoff that returns.

Whether a request can be done is the MODEL's judgement, and only the model can make it. What happens
next is code. So the model is given one tool, `decline_request(reason)`, and calling it is the whole of
its part: the reason is recorded against the task here, and `lifecycle.complete_callback` reads it and
FAILS the task with "Declined: <reason>" instead of completing it. The person sees, on the task itself,
that it was not done and why. Nothing parses the reply for "I cannot": a sentence is not a signal.

The node has no "declined" end state yet, so this is `aimeat_task_fail` with a message that starts with
DECLINED_PREFIX -- distinct from a crash, a refusal by the node (lifecycle.refusal_summary) or a failed
verify gate, each of which says its own thing.

A crew opts in by giving its agents the tool: a Python crew with `make_decline_tool(agent, task_id)`,
a crew definition with the tool id `decline`. A crew that does not is unchanged.
"""

from __future__ import annotations

import sys
import threading

DECLINED_PREFIX = "Declined:"

_LOCK = threading.Lock()
# task id -> the reason the model gave. Taken (and so cleared) by the completion of that task.
_DECLINED: dict[str, str] = {}


def note_declined(task_id: str, reason: str) -> None:
    """Record that this task's request was turned down. The first reason given stands."""
    reason = " ".join(str(reason or "").split()) or "no reason was given"
    with _LOCK:
        _DECLINED.setdefault(task_id, reason)


def take_declined(task_id: str) -> str | None:
    """The reason this task was declined, or None; clears it, so a daemon does not keep old tasks."""
    with _LOCK:
        return _DECLINED.pop(task_id, None)


def declined_message(reason: str, deliverable_key: str | None = None) -> str:
    """What the task failure says: that it was not done, why, and where the full reply is."""
    msg = f"{DECLINED_PREFIX} the request was not carried out. {reason.rstrip('.')}."
    if deliverable_key:
        msg += f" The full reply is in memory at {deliverable_key}."
    return msg


def make_decline_tool(agent_name: str, task_id: str | None):
    """The `decline_request` tool for one task's run."""
    from crewai.tools import tool

    @tool("decline_request")
    def decline_request(reason: str) -> str:
        """Call this when you will NOT carry out the request: you lack the tool or the access, it is
        outside what you do, or it should not be done. `reason` is one or two plain sentences for the
        person: what you could not do, why, and who or what can do it instead. The task is then recorded
        as declined rather than done. Still write your reply to the person as your final answer. Do not
        call this when you did the work, or did part of it -- say in your answer what is missing instead."""
        if not task_id:
            # A run with no task (a message) has nothing to mark; the reply itself is the answer.
            return "Noted. This request did not come as a task, so your reply is the whole answer."
        note_declined(task_id, reason)
        print(f"[{agent_name}] task {task_id} DECLINED by the agent: {reason}", file=sys.stderr)
        return "Recorded: this task will be marked as declined, with your reason. Now write your reply to the person."

    return decline_request

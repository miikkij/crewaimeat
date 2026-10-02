"""Deterministic publish and completion transitions, independent of CrewAI construction.

A RUN THE NODE REFUSED IS NOT A RUN THAT WORKED. A crew that meets a 403 SCOPE_DENIED usually carries
on: the model reads the refusal as one more tool result, the kickoff returns, and the scaffold's own
completion callback closes the task as done. Measured 2026-09-29 on a sold seat: exit 0, every write
refused, and the customer's task stayed queued with nothing on screen saying why.

The node keeps every refusal of an agent (GET /v1/agents/{name}/refusals?since=<run start>), and
aimeat-crewai 0.31.0 asks it after each kickoff. That is too late HERE: this module completes the task
INSIDE the kickoff (the finalize task's callback) and, on the deterministic `on_task` path and the
onboarding smoke test, inside the builder before any kickoff at all. By the time the package asked, the
task was already done and its /fail was refused as an invalid state. So `complete_callback` asks first,
and a refused run is FAILED with each call and permission named instead of being completed.

A RUN THAT TURNED THE REQUEST DOWN IS NOT DONE EITHER. When the agent called `decline_request`
(crewaimeat.decline), the task is failed with "Declined: <its reason>" instead of completed.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from crewaimeat.decline import declined_message, take_declined

# The runtime's clock and the node's clock are not the same clock. Asking from a little before the run
# started keeps a node a few seconds behind from hiding the run's own refusals. The SAME margin
# aimeat-crewai 0.31.0 uses (daemon._RUN_CLOCK_MARGIN_S), so both sides ask about one window.
RUN_CLOCK_MARGIN_S = 5.0

# Where the owner gives a missing permission, in the words the task failure and the log both use.
WHERE_THE_OWNER_GIVES_IT = "the owner gives it in Profile > Agents > Manage access rights"
_WHERE_THE_OWNER_GIVES_IT = WHERE_THE_OWNER_GIVES_IT

# The statuses of a task the node is waiting on this agent for. The daemon's own vocabulary
# (aimeat_crewai.daemon polls "active" and "stalled"); a queued task of a non-task-runner is the
# OWNER's to start and is left alone.
_OPEN_STATUSES = frozenset({"active", "stalled", "in_progress"})

_LOCK = threading.Lock()
# The start of the WORKER's run, when this process is one (run_once sets it). Consumed by the first task
# the worker builds: a spawn worker exists because of that task, so the refusals of its start-up -- the
# identity push, where the sold seat's `PATCH /v1/agents/concierge/tags` was refused (an agent's own
# tags take no word since aimeat-protocol bcd4027ed, and are written only when they differ) -- belong to it.
_WORKER_RUN_START: dict[str, str | None] = {"at": None}
# Tasks this process refused, task id -> the sentence that says why. run_once reads it for its exit code.
_REFUSED: dict[str, str] = {}


def run_started_iso(now: float | None = None) -> str:
    """The `since` for a run starting now, in the ISO form the node parses (millisecond, Z)."""
    started = (time.time() if now is None else now) - RUN_CLOCK_MARGIN_S
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(started)) + f".{int((started % 1) * 1000):03d}Z"


def set_worker_run_start(iso: str) -> None:
    """Called by run_once at the top of the worker, before anything touches the node."""
    with _LOCK:
        _WORKER_RUN_START["at"] = iso


def take_run_since() -> str:
    """The window start for the task being built now.

    The first task a spawn worker builds takes the WORKER's start, so the refusals of its own start-up
    count against it; every later task, and every task of a continuous daemon (where start-up happened
    once, maybe days ago), takes its own start. Taken at the top of the builder, which is where
    aimeat-crewai 0.31.0 takes its own: "the run starts before the crew is built".
    """
    with _LOCK:
        worker = _WORKER_RUN_START["at"]
        _WORKER_RUN_START["at"] = None
    return worker or run_started_iso()


def note_refused(task_id: str, reason: str) -> None:
    with _LOCK:
        _REFUSED.setdefault(task_id or "(unknown task)", reason)


def refused_runs() -> dict[str, str]:
    with _LOCK:
        return dict(_REFUSED)


def cannot_start_message(reason: str) -> str:
    """The sentence a task carries when the runtime that was to run it never got going."""
    reason = reason.strip().rstrip(".")
    return f"The agent's runtime could not start, so this task did not run: {reason}. Once that is fixed, the task can run again."


def fail_open_tasks(call: Callable, agent_name: str, reason: str, *, refused: bool = False) -> list[str]:
    """Fail every task the node holds OPEN for this agent, naming `reason`. Returns the ids it failed.

    A RUNTIME THAT CANNOT START MUST NOT LEAVE ITS TASK ACTIVE. Measured 2026-10-02 on a hosted place:
    a chat-proposed agent was refused the read of its own definition, the worker exited 1 on each wake
    (two runs, 11 s each), and the customer's task stayed "active" for good with no word on it. The
    task is the one surface the customer sees, so the reason -- and what the owner does about it --
    goes THERE, not only into a log on a machine they cannot open.

    `refused=True` records each task under lifecycle's refused set, so run_once exits 3 and the spawner
    does not re-run a start the owner has to unblock. An agent refused task:write cannot fail its own
    task either; then the record and the exit code are what is left, and the log says so.
    """
    message = cannot_start_message(reason)
    data = call(agent_name, "aimeat_task_list", {})
    tasks = data.get("tasks") if isinstance(data, dict) else data
    if not isinstance(tasks, list):
        print(
            f"[{agent_name}] could not list its tasks, so none could be failed with the reason; "
            f"an open task stays as it is: {reason}",
            file=sys.stderr,
        )
        if refused:
            note_refused("(start-up)", reason)
        return []
    failed: list[str] = []
    noted = False
    for t in tasks:
        if not isinstance(t, dict) or str(t.get("status") or "") not in _OPEN_STATUSES:
            continue
        tid = str(t.get("id") or "")
        if not tid:
            continue
        if refused:
            note_refused(tid, reason)
            noted = True
        fr = call(agent_name, "aimeat_task_fail", {"task_id": tid, "message": message})
        if fr is None:
            print(
                f"[{agent_name}] runtime cannot start -> task_fail {tid} was NOT accepted "
                "(an agent without task:write cannot fail its own task); the task stays active until "
                f"the owner acts: {reason}",
                file=sys.stderr,
            )
            continue
        failed.append(tid)
        print(f"[{agent_name}] runtime cannot start -> task_fail {tid}: {reason}", file=sys.stderr)
    if refused and not noted:
        note_refused("(start-up)", reason)
    if not failed and not any(isinstance(t, dict) and str(t.get("status") or "") in _OPEN_STATUSES for t in tasks):
        print(f"[{agent_name}] runtime cannot start, and the node holds no open task for it: {reason}", file=sys.stderr)
    return failed


def refusal_summary(refusals: list[dict]) -> str:
    """One sentence for the task's failure and the log: which call, which permission, who fixes it."""
    parts = []
    for r in refusals[:5]:
        needed = [str(s) for s in (r.get("needed") or [])] or ["an unnamed permission"]
        joined = (" or " if r.get("any_of") else " and ").join(needed)
        parts.append(f"{r.get('call') or 'a call'} needs {joined}")
    more = f" (and {len(refusals) - 5} more)" if len(refusals) > 5 else ""
    return (
        "AIMEAT refused calls of this run for a missing permission: "
        + "; ".join(parts)
        + more
        + f". {_WHERE_THE_OWNER_GIVES_IT[0].upper()}{_WHERE_THE_OWNER_GIVES_IT[1:]}, and then the task can run again."
    )


@dataclass
class LifecycleCallbacks:
    call: Callable
    eval_ctx: Callable
    mark_todos_done: Callable
    deliverable_keys: dict[str, str]
    # (agent_name, since_iso) -> the refusals since then, [] for none, None when the node could not be
    # asked. None of this is known to the dataclass: aimeat_crew wires the node route in.
    refusals: Callable | None = None

    def publish_callback(
        self,
        agent_name: str,
        primary_key: str,
        shared_key: str | None = None,
        tag: str | None = None,
        eval_info: dict | None = None,
        task_id: str | None = None,
        clean: Callable[[str], str] | None = None,
        offer_id: str | None = None,
    ):
        """Task callback: write the task output to AIMEAT memory deterministically (no LLM).

        Attached to the last DOMAIN task so the deliverable always lands, even if the liaison's
        LLM-driven memory_write loops or errors (observed on weaker models). Always writes the agent's
        own key; if a shared_key/tag are supplied (a delegated workflow subtask), ALSO writes into the
        shared tag area so the coordinator can collect it with its own scope. When eval_info is given,
        also records the run's eval-context (model/temperature/tokens): the shared `<shared_key>.evalctx`
        is written BEFORE the shared deliverable (so a coordinator that detects the deliverable always
        finds the evalctx beside it), plus an own-introspection copy under statistics.custom.*.

        task_id (the full AIMEAT task id) is added as a `task:<id>` tag on every per-task write so AIMEAT
        can list a task's memory entries by tag (GET /v1/memory?...&tags=task:<id>). The tag is additive —
        key formats are unchanged — and since the callback is deterministic it lands as surely as the
        deliverable itself. When the task was ordered from the Offers surface (scope carries offer_id), an
        `offer:<offer_id>` tag is added too, so the Offerings card can list the last N runs for THAT offer."""
        task_tag = f"task:{task_id}" if task_id else None
        offer_tag = f"offer:{offer_id}" if offer_id else None
        _per_task_tags = [t for t in (task_tag, offer_tag) if t]  # additive; key formats unchanged

        def _cb(task_output) -> None:
            text = getattr(task_output, "raw", None)
            if text is None:
                text = str(task_output)
            if clean:  # deterministic post-processor (e.g. strip an editor's leaked KEPT/CUT notes)
                try:
                    cleaned = clean(text)
                    if cleaned:  # never publish an empty deliverable; fall back to the original
                        text = cleaned
                except Exception as exc:  # noqa: BLE001 — cleaning is best-effort, must not block publish
                    print(f"[{agent_name}] clean_deliverable skipped: {exc}", file=sys.stderr)
            r1 = self.call(
                agent_name,
                "aimeat_memory_write",
                {"key": primary_key, "value": text, "visibility": "owner", "tags": list(_per_task_tags)},
            )
            if r1 is None:
                raise RuntimeError(f"Deliverable publication failed for {primary_key}")
            print(
                f"[{agent_name}] deliverable published -> {primary_key} (tags {_per_task_tags}): {bool(r1)}",
                file=sys.stderr,
            )
            ectx = self.eval_ctx(eval_info)
            if ectx and eval_info and eval_info.get("custom_key"):
                self.call(  # own performance introspection; public so the Quality Custom Metrics tab renders it
                    agent_name,
                    "aimeat_memory_write",
                    {"key": eval_info["custom_key"], "value": ectx, "visibility": "public"},
                )
            if shared_key:
                shared_tags = [
                    t for t in (tag, task_tag, offer_tag) if t
                ]  # delegation + per-task + per-offer (additive)
                if ectx:  # write evalctx FIRST so it is present when the coordinator sees the deliverable
                    self.call(
                        agent_name,
                        "aimeat_memory_write",
                        {"key": f"{shared_key}.evalctx", "value": ectx, "visibility": "owner", "tags": shared_tags},
                    )
                r2 = self.call(
                    agent_name,
                    "aimeat_memory_write",
                    {"key": shared_key, "value": text, "visibility": "owner", "tags": shared_tags},
                )
                if r2 is None:
                    raise RuntimeError(f"Shared deliverable publication failed for {shared_key}")
                print(f"[{agent_name}] deliverable shared -> {shared_key} (tag {tag}): {bool(r2)}", file=sys.stderr)

        return _cb

    def complete_callback(
        self,
        agent_name: str,
        tid: str,
        mem_key: str | None = None,
        require_verify: bool = False,
        owner: str | None = None,
        auto_revert: bool = False,
        since: str | None = None,
    ):
        """Task callback: close the AIMEAT task deterministically (no LLM). Attached to the finalize
        task so the task is completed even if the liaison never calls aimeat_task_complete.

        When `since` (the run's start) is given, the node is asked FIRST what it refused this agent
        since then. A refused run is FAILED (aimeat_task_fail) with each call and permission named, and
        is not completed: the refusal comes before the verify gate, because a write that never landed is
        the more fundamental reason and a verify verdict read on top of it would be about the wrong thing.
        A node without the route answers 404 until its release is deployed, which reads as "no refusals
        known" and completes as before. A node that could not be asked at all completes too, and SAYS so
        on the task: turning a good run into a failed one because the check itself failed would trade one
        silent lie for another.

        When require_verify is True (CrewSpec.require_verify_pass — SYS-1), completion is GATED on the app
        verify gates' deterministic outcome: a build whose verify_render / verify_interaction FAILED, or that
        never ran a gate at all, is FAILED (aimeat_task_fail) instead of shipping 'green'. The verdicts come
        from the gate {ok} recorded by the verify tools (author_tool.get_verify_verdicts), never the agent's
        self-reported text — the whole point is to not trust the self-report. The gate is STATUS-ONLY.

        When auto_revert is True (CrewSpec.auto_revert_on_fail), a gate-fail ALSO restores each app this run
        published to its pre-run last-good version (revert_apps_to_baseline) — an outward-facing live rollback,
        kept a SEPARATE opt-in from the safe status gate.

        A task the agent DECLINED (crewaimeat.decline) is failed with "Declined: <reason>" instead of
        completed: after the refusal check (a node refusal is the more fundamental reason, and is often
        WHY it declined), before the verify gate (there is no build to verify)."""

        def _cb(_task_output) -> None:
            # Taken first, whichever way this run ends, so a long-lived daemon never carries it over.
            declined = take_declined(tid) if tid else None
            unchecked = ""
            if since and self.refusals is not None:
                try:
                    refused = self.refusals(agent_name, since)
                except Exception as exc:  # noqa: BLE001 — the check must never be what breaks finalize
                    print(f"[{agent_name}] refusal check raised ({exc!r}); completing without it", file=sys.stderr)
                    refused = None
                if refused:
                    reason = refusal_summary(refused)
                    # Recorded BEFORE the /fail, and whatever it answers: an agent refused task:write
                    # cannot fail its own task either, and then the exit code is the only thing left
                    # that says the run was refused.
                    note_refused(tid, reason)
                    fr = self.call(agent_name, "aimeat_task_fail", {"task_id": tid, "message": reason})
                    print(
                        f"[{agent_name}] run REFUSED by the node -> task_fail {tid} "
                        f"({'failed' if fr is not None else 'the /fail was not accepted either'}): {reason}",
                        file=sys.stderr,
                    )
                    return
                if refused is None:
                    unchecked = " The node could not be asked whether it refused any of this run's calls."
                    print(f"[{agent_name}] refusal check unavailable for {tid}; completing", file=sys.stderr)
            if declined:
                key = self.deliverable_keys.pop(tid, None) or mem_key
                message = declined_message(declined, key) + unchecked
                fr = self.call(agent_name, "aimeat_task_fail", {"task_id": tid, "message": message})
                if fr is None:
                    # Left active it would be run again, and turned down again, for nothing.
                    raise RuntimeError(f"Declining task {tid} failed")
                print(f"[{agent_name}] task DECLINED -> task_fail {tid}: {message}", file=sys.stderr)
                return
            if require_verify:
                try:
                    from crewaimeat.author_tool import get_verify_verdicts

                    verdicts = get_verify_verdicts(tid)
                except Exception as exc:  # noqa: BLE001 — never break finalize on the lookup
                    print(f"[{agent_name}] verify-gate lookup failed ({exc}); refusing completion", file=sys.stderr)
                    verdicts = {"verify_lookup": {"ok": False}}
                if verdicts is not None:
                    failed = sorted(g for g, v in verdicts.items() if v.get("ok") is False)
                    passed = [g for g, v in verdicts.items() if v.get("ok") is True]
                    reason = None
                    if failed:
                        reason = (
                            f"Not shipping a broken build: verify gate(s) FAILED — {', '.join(failed)}. "
                            "Fix the app and re-queue."
                        )
                    elif not passed:
                        reason = (
                            "Not shipping unverified: no verify gate produced a PASS. A build must prove "
                            "itself with verify_render / verify_interaction before it can complete."
                        )
                    if reason:
                        # The gate itself only fails the task (status-only). Optional, separate opt-in:
                        if auto_revert:
                            # also restore each app this run published to its pre-run last-good version, so the
                            # LIVE app is rolled back, not just left un-'done' (the recorded rollback baseline).
                            restored = []
                            try:
                                from crewaimeat.author_tool import revert_apps_to_baseline

                                restored = [r for r in revert_apps_to_baseline(agent_name, tid, owner) if r.get("ok")]
                            except Exception as exc:  # noqa: BLE001 — revert is best-effort; still fail the task
                                print(f"[{agent_name}] auto-revert skipped ({exc})", file=sys.stderr)
                            if restored:
                                names = ", ".join(f"{r['filename']}->v{r['to_version']}" for r in restored)
                                reason += f" Auto-restored {len(restored)} app(s) to last-good: {names}."
                        fr = self.call(agent_name, "aimeat_task_fail", {"task_id": tid, "message": reason})
                        print(
                            f"[{agent_name}] require_verify_pass GATE -> task_fail {tid}: {reason[:90]} ({bool(fr)})",
                            file=sys.stderr,
                        )
                        return
            # Mark todos done DETERMINISTICALLY here, while the task is still active (aimeat_task_todo
            # rejects a completed task), so a task never lands Done with its todos still pending (0/1).
            self.mark_todos_done(agent_name, tid)
            payload = {"task_id": tid, "message": "Crew finished; deliverable published to memory."}
            # A pipeline that wrote a contract key (the workflow blueprint's key) named it here; that key
            # IS the deliverable, so it wins over the scaffold's derived crews.<agent>.… wrapper key.
            key = self.deliverable_keys.get(tid) or mem_key
            if key:
                # The Offers/Inbox contract: the task record's deliverable key points at the memory key
                # holding the deliverable — without it the Inbox shows the task but no content/sample,
                # and `outcome` comes back with a message and no address to follow.
                #
                # THE FIELD IS snake_case, AND WE HAD IT WRONG. Both doors read `deliverable_key`: the
                # MCP tool declares it (mcp/agent-tasks.ts) and the REST route reads
                # `req.body?.deliverable_key` (routes/agent-tasks/completion.ts). We sent
                # `deliverableKey`, which is simply ignored — no error, the completion succeeds, and the
                # pointer is silently absent. Measured 2026-08-16: task a73ddeb9 completed with a real
                # deliverable in memory and its outcome carried state/message/at but no deliverable_key.
                payload["deliverable_key"] = key
                payload["message"] = f"Crew finished; deliverable published to memory at {key}."
            payload["message"] += unchecked
            res = self.call(agent_name, "aimeat_task_complete", payload)
            if res is None:
                raise RuntimeError(f"Task completion failed for {tid}")
            self.deliverable_keys.pop(tid, None)
            print(
                f"[{agent_name}] task completed deterministically {tid} (deliverable_key={key or '-'}): {bool(res)}",
                file=sys.stderr,
            )

        return _cb

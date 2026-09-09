"""Deterministic publish and completion transitions, independent of CrewAI construction."""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class LifecycleCallbacks:
    call: Callable
    eval_ctx: Callable
    mark_todos_done: Callable
    deliverable_keys: dict[str, str]

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
    ):
        """Task callback: close the AIMEAT task deterministically (no LLM). Attached to the finalize
        task so the task is completed even if the liaison never calls aimeat_task_complete.

        When require_verify is True (CrewSpec.require_verify_pass — SYS-1), completion is GATED on the app
        verify gates' deterministic outcome: a build whose verify_render / verify_interaction FAILED, or that
        never ran a gate at all, is FAILED (aimeat_task_fail) instead of shipping 'green'. The verdicts come
        from the gate {ok} recorded by the verify tools (author_tool.get_verify_verdicts), never the agent's
        self-reported text — the whole point is to not trust the self-report. The gate is STATUS-ONLY.

        When auto_revert is True (CrewSpec.auto_revert_on_fail), a gate-fail ALSO restores each app this run
        published to its pre-run last-good version (revert_apps_to_baseline) — an outward-facing live rollback,
        kept a SEPARATE opt-in from the safe status gate."""

        def _cb(_task_output) -> None:
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
            res = self.call(agent_name, "aimeat_task_complete", payload)
            if res is None:
                raise RuntimeError(f"Task completion failed for {tid}")
            self.deliverable_keys.pop(tid, None)
            print(
                f"[{agent_name}] task completed deterministically {tid} (deliverable_key={key or '-'}): {bool(res)}",
                file=sys.stderr,
            )

        return _cb

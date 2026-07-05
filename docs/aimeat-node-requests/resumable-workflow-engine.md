# AIMEAT node — workflow engine: resume-on-retry (partial-success)

**Status (2026-07-05):**
- **Ask 1 (per-step timeout) — RESOLVED, no node work needed.** The node already accepts, stores, and
  honors a per-step `timeout_min`, defaulting each step to **60 min**. We set `write-a`/`write-b` to
  `240` on `laimeat-sanomat-evening` and confirmed the round-trip. See "Ask 1 findings" below.
- **Ask 2 (resume-on-retry / partial-success) — the actual node request.** Detailed below.

---

## Context (how Agent Workflows run today)
An AIMEAT workflow is a declarative DAG of **steps**, each `{id, agent, offer, after[], retry{max,backoff_min},
timeout_min}`. The node schedules the workflow, dispatches each step as a **task** to its agent (a
task-runner crew), then waits for the step's **success signal** — leaf checks resolved from the agent's
*offer* — to be satisfied within `timeout_min`. Leaf types in use: `count_nonempty(key_glob, min)`,
`nonempty(key)`, `json_field(key, path, min)`, evaluated against owner memory. `after[]` edges gate
downstream steps; `on_step_fail: "inspect"` dispatches an inspector task.

The node **already persists** a full run record at memory key `workflows.run.<wf_id>.<run_id>` containing
`defSnapshot`, each step's state (`green` / `input-red` / `output-red` / `timed-out` / `skipped`), and
**per-leaf expected-vs-observed**.

## The incident that motivates this
Run `laimeat-sanomat-evening / 7a8f5016-7ea8-4dde-9aee-c38c5ebfcb0d`: `write-a`/`write-b` each do ~10
sequential LLM article generations. On a slow model endpoint they took ~2h, **exceeded the 60-min per-step
deadline → `timed-out` → downstream `features` + `editorial` were `skipped`** and two inspector tasks fired.
**But the crew kept working and every leaf (`news.<date>.evening.article.*`) was eventually filled** — the
articles all landed. The engine gave up waiting and skipped downstream *unnecessarily*; the work had
actually completed. (A task-runner crew does not abort when the node stops waiting — two independent clocks.)

## Ask 1 findings (resolved — documented for completeness)
- The step schema accepts `timeout_min` (confirmed in the `aimeat_workflow_save` descriptor and by a
  live round-trip). Absent → the node applies a **60-min** default to every step.
- Interim fix applied: `write-a`/`write-b` set to `timeout_min: 240` so a slow LLM spell no longer trips
  the deadline. This is a stopgap; **Ask 2 is the correct long-term fix** (a big timeout just hangs longer
  when a step is genuinely stuck).

## Ask 2 — Resume-on-retry / partial-success (the real value)
Today, on timeout (or a `retry`) the engine restarts the step and, on failure, skips dependents. Requested
change: **before declaring failure, and on any retry, re-evaluate the step's success signal against current
memory** using the persisted run record, and:
- **Fully satisfied now** (the slow crew finished after the deadline) → mark the step `green`
  retroactively and **un-skip / proceed** with dependents.
- **Partially satisfied** → count partial success and **re-dispatch only for the missing leaves**.
  Crewaimeat stages are **idempotent** (a re-run fills only absent keys, never rewrites), so gap-only
  resumption is safe — no completed work is redone.
- **Skip a dependent only if its own `required_to_function` gate is genuinely unmet** after re-evaluation
  (e.g. `features` needs `count_nonempty(article.*) ≥ 3` — if the writes filled 12, it must run, not skip).

Because our stages are already idempotent, the node only needs to **(a) re-check leaves before failing and
(b) resume rather than restart** — this yields crash-resumable pipelines with **zero crew-side change**.
It's the `@persist`/resume-fork analog for a node-run workflow.

## Distinguishing "slow" from "stuck"
To avoid waiting forever on a genuinely dead step, one of: a final success-signal re-check at the deadline;
a short grace re-check window; or treat a step whose leaf count is **monotonically increasing** as
*in-progress* (extend) vs. no progress for N minutes = stalled.

## Backward compatibility
- `timeout_min` already optional; absence = 60-min default (unchanged).
- Resume-on-retry: safe-by-default given idempotent stages, but make it opt-in per workflow if preferred
  (a `resume: true` flag, or fold into the existing `retry` / `on_step_fail`).

## Nice-to-have (dashboard)
Surface per-step progress in the run record so the UI shows **"in progress — leaves 8/12"** instead of a
hard `timed-out` while the crew is still filling keys.

## Acceptance scenarios
1. `write-a` (slow endpoint) fills 12/12 articles at 2h with `timeout_min: 240` → step `green`,
   `features`/`editorial` **run** (not skipped).
2. `write-b` fills 8/12 then the crew dies → on retry, engine re-dispatches only the 4 missing categories;
   on success → `green`.
3. `write-b` fills 0/12 and no progress for N min → `timed-out` + inspect (today's behavior, correct).

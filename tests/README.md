# Crew test floor (L1 + L2)

The deterministic, LLM-free, no-network test floor for the AIMEAT crews — the P0 layer from
[../docs/aimeat-guides/nextgeneration/10-testing-and-evaluation-plan.md](../docs/aimeat-guides/nextgeneration/10-testing-and-evaluation-plan.md).
It is the gate that catches the highest-blast-radius regressions in milliseconds, for free, on
every PR.

## Run it

```bash
uv run pytest            # the whole floor
uv run pytest -q tests/test_build_domain.py     # just the per-crew contract
uv run crewaimeat doctor --strict               # the reconciliation gate CI also runs
```

`doctor` is not part of pytest and is not meant to be: pytest asserts what the code DOES, doctor
reconciles what the repo DECLARES (six registries agreeing, every node/model call taking a sanctioned
route). Both run in CI and in the pre-commit hook. Its own floor is
[test_doctor.py](test_doctor.py) — a quality gate without tests is an opinion.

No API keys, no AIMEAT connection, no network. A dummy `OPENROUTER_API_KEY` is set in
[conftest.py](conftest.py) only so `LLM(...)` objects construct (they are never called).

## What it covers

| File | Layer | Asserts |
|------|-------|---------|
| [test_scaffold_pure.py](test_scaffold_pure.py) | L1 | The pure scaffold functions every crew inherits: `_memory_key` slugging, publish/verify directive parsing, directive & command rendering, the deterministic `_make_publish_cb` (writes the deliverable in code, asserted via a mocked `_aimeat_call` — never via printed ReAct text), and the task-nature keyword fallback. |
| [test_guardrails.py](test_guardrails.py) | L2 | The reusable, LLM-free task guardrails in [../crews/_guardrails.py](../crews/_guardrails.py) — fence-stripping JSON validation, required-field checks, a 1–10 score check, source-URL presence. Build the guardrail once, test the guardrail, then wire it onto a crew's prose-only task contract. |
| [test_build_domain.py](test_build_domain.py) | L1 | Every crew's `build_domain(ctx)` over a stub context: returns a non-empty `(agents, tasks)`; each task has a real description and an in-crew agent; `context=[...]` chains only this crew's tasks; **`ctx.prompt` reaches a task description** (the [crew-builddomain-must-inject-ctx-prompt](../docs/aimeat-guides/crewairesearch/02-prompting-and-agent-design.md) contract); workers are non-delegating and loop-bounded. Plus two regression tests for the bugs fixed alongside this floor. |

## The two regression tests (lock in the bug fixes)

- `test_news_writer_writer_agents_have_memory_tools` — every news-writer agent told to call
  `write_memory` must actually have that tool. (The three category writers previously had no
  `tools=`, so articles never reached memory.)
- `test_finnish_researcher_has_no_unsubstituted_placeholders` — no task description may contain a
  literal `{ctx.today}`/`{ctx.prompt}`. (The synthesis report header was a non-f-string, so the
  placeholders printed verbatim.)

## Which crews are covered — and `max_iter`

`CREW_MODULES` is **derived from disk** (`crews/*_crew.py`, a leading `_` = parked — the same rule the
fleet host uses), so a new crew is contract-tested the day it lands and a parked one leaves the list by
itself. It was hand-kept until 2026-08-22, by which point it named 21 of 46 live crews and still listed
four that had been parked — 16 permanently failing tests, behind which every real regression hid.

A crew with no `build_domain` is not exempt: it must be a genuine brain stub, which
`test_brain_stubs_are_really_brain_stubs` asserts. Deleting the function moves a crew to a *stricter*
claim, never off the floor.

**`max_iter` is a backstop, not a gap to close.** An earlier version of this floor enforced
`max_iter <= 40` with an xfail ratchet. Field data (2026-06-05, live operator runs) overturned it: the
cap fires only on NON-convergent re-authoring loops, is load-bearing for the builder/fixer/editor crews,
and lowering it merely makes a doomed loop fail faster — it cannot tell thrashing from legitimate build
depth. The floor now pins the real invariant (`allow_delegation=False`) and flags only an absurd value
(a typo). The runaway bound is a wall-clock (`AIMEAT_AGENT_MAX_EXECUTION_TIME`) plus verify-gated
completion.

## `ctx.prompt` injection, and opting out honestly

`test_ctx_prompt_is_injected` requires the user's ask to reach a task description — the failure where an
agent never sees its task and drifts to a guessed target. A crew whose real work is NOT prompt-driven (a
deterministic pipeline woken by a record, a DM loop, a scheduled marker) opts out by declaring
`PROMPT_INDEPENDENT = "<reason>"`. The opt-out is a **written reason, not a boolean**, precisely so it
cannot be used to silence a real regression: absent means the strict rule applies, so a new crew is held
to it by default and has to say out loud why it is different.

## What's next (from the testing plan)

- **Wire the guardrails** (`crews/_guardrails.py`) onto the prose-only task contracts (cortex-fixer
  artifacts, idea-feasibility score, researcher source-URLs) with `guardrail_max_retries`.
- **L3** — `crewai test -n 3 -m openrouter/x-ai/grok-4-fast` baselines per crew (evaluator pinned to
  the fleet model).
- **L4** — the AIMEAT-wired regression: queue a known task → read the published key →
  `verify_render`/`verify_interaction` → assert the selection rollup did not regress. See
  [11-claude-code-eval-prompts.md](../docs/aimeat-guides/nextgeneration/11-claude-code-eval-prompts.md).

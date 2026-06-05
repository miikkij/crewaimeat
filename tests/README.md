# Crew test floor (L1 + L2)

The deterministic, LLM-free, no-network test floor for the AIMEAT crews — the P0 layer from
[../docs/aimeat-guides/nextgeneration/10-testing-and-evaluation-plan.md](../docs/aimeat-guides/nextgeneration/10-testing-and-evaluation-plan.md).
It is the gate that catches the highest-blast-radius regressions in milliseconds, for free, on
every PR.

## Run it

```bash
uv run pytest            # the whole floor
uv run pytest -q tests/test_build_domain.py     # just the per-crew contract
```

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

## The `max_iter` ratchet

`test_workers_bounded_and_non_delegating` enforces `allow_delegation=False` for **all** crews and
`max_iter <= 40`. Six builder/fixer crews currently exceed it (audit roadmap item #2) and are
**strict-xfail**'d in `KNOWN_MAX_ITER_GAPS`. When you cap one to `<=40`, its test flips to *xpass*
and pytest fails until you remove it from that set — a ratchet that only tightens. Do not add a
crew to the set to silence it; cap the crew.

## What's next (from the testing plan)

- **Wire the guardrails** (`crews/_guardrails.py`) onto the prose-only task contracts (cortex-fixer
  artifacts, idea-feasibility score, researcher source-URLs) with `guardrail_max_retries`.
- **L3** — `crewai test -n 3 -m openrouter/x-ai/grok-4-fast` baselines per crew (evaluator pinned to
  the fleet model).
- **L4** — the AIMEAT-wired regression: queue a known task → read the published key →
  `verify_render`/`verify_interaction` → assert the selection rollup did not regress. See
  [11-claude-code-eval-prompts.md](../docs/aimeat-guides/nextgeneration/11-claude-code-eval-prompts.md).

"""Reusable, deterministic, LLM-free task guardrails — the L2 "contract as a test" layer.

A CrewAI *function guardrail* runs on every task output and returns ``(ok, value_or_reason)``.
Because it is pure and runs at runtime, it doubles as a unit-testable contract: build the
guardrail once, test the guardrail (tests/test_guardrails.py), then wire it onto the
prose-only task contracts the fleet audit flagged (see
docs/aimeat-guides/nextgeneration/10-testing-and-evaluation-plan.md §3).

CrewAI passes a ``TaskOutput`` (which carries ``.raw``); these helpers stay duck-typed —
they read ``.raw`` if present, else treat the argument as the raw string — so they are
trivially testable with a tiny stand-in object.

Wire example (in a crew's build_domain)::

    from crews._guardrails import json_with_fields
    fix = Task(description="... Output RAW JSON only — no fences, no preamble.",
               expected_output='{"artifacts":[...]}',
               guardrail=json_with_fields("artifacts"), guardrail_max_retries=2)
"""

from __future__ import annotations

import json
import re

# Strips a leading ```/```json fence and a trailing ``` fence (the exact FAIL-5 crash case).
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.S)


def _raw(output) -> str:
    """The raw text of a CrewAI TaskOutput, or the value itself if a plain string was passed."""
    return getattr(output, "raw", output) or ""


def strip_fences(raw: str) -> str:
    """Remove a surrounding markdown code fence so ``json.loads`` can parse the body."""
    return _FENCE.sub("", raw or "").strip()


def json_with_fields(*required: str):
    """Guardrail: the output must be a raw JSON object that contains every ``required`` key
    (non-empty). Tolerates a fenced/preambled reply by fence-stripping first."""

    def _g(output):
        try:
            data = json.loads(strip_fences(_raw(output)))
        except Exception as e:  # noqa: BLE001 — any parse failure is a guardrail failure
            return (False, f"not valid JSON after fence-strip: {e}")
        if not isinstance(data, dict):
            return (False, "top-level JSON must be an object")
        missing = [k for k in required if k not in data or data[k] in (None, "", [])]
        if missing:
            return (False, f"missing/empty fields: {missing}")
        return (True, output)

    return _g


def feasibility_score_1_to_10(output):
    """Guardrail for a rater: a 1–10 integer score must be present (``N/10`` or
    ``Feasibility Score: N``) and in range."""
    m = re.search(r"(\d+)\s*/\s*10|Feasibility Score:\s*(\d+)", _raw(output))
    if not m:
        return (False, "no 'N/10' feasibility score found")
    score = int(next(g for g in m.groups() if g))
    if not 1 <= score <= 10:
        return (False, f"score {score} out of range 1-10")
    return (True, output)


def has_source_urls(min_count: int = 1):
    """Guardrail for a research deliverable: at least ``min_count`` source URLs must appear."""

    def _g(output):
        n = len(re.findall(r"https?://", _raw(output)))
        if n < min_count:
            return (False, f"need >= {min_count} source URL(s), found {n}")
        return (True, output)

    return _g

"""Make CrewAI surface OpenRouter's per-call cost so the AIMEAT ledger can record real spend.

Root cause (traced through the CrewAI source): CrewAI 1.15 routes OpenRouter through its NATIVE
openai-compatible provider (`crewai.llms.providers.openai.completion.OpenAICompletion`), NOT litellm.
OpenRouter returns the authoritative USD cost inline on `response.usage.cost` (and the OpenAI SDK
preserves it in `usage.model_extra`), but CrewAI's `_extract_openai_token_usage` hand-builds a dict of
ONLY the token counts and DROPS `cost`. So `LLMCallCompletedEvent.usage` never carries cost, and the
event-bus ledger hook records the call unpriced ($0) — even though OpenRouter told us the price.

Fix: wrap that extractor (and the Responses-API one) so the returned usage dict also carries `cost`
(and `cost_details`) read straight off `response.usage`. Then the ledger's `usage.get("cost")` read
(aimeat-crewai 0.16.2) sees the real number. Surgical, idempotent, best-effort — if CrewAI's internals
ever move, the patch quietly no-ops and metering degrades to unpriced, never breaking a crew.

Installed once at fleet-host startup (alongside the log-timestamp wrapper).
"""

from __future__ import annotations

import sys
from typing import Any

_installed = False


def _cost_of(response: Any) -> float | None:
    """OpenRouter's authoritative USD cost off a completion/response object, or None."""
    u = getattr(response, "usage", None)
    if u is None:
        return None
    cost = getattr(u, "cost", None)
    if cost is None:
        extra = getattr(u, "model_extra", None)
        if isinstance(extra, dict):
            cost = extra.get("cost")
    return float(cost) if isinstance(cost, (int, float)) and cost >= 0 else None


def _wrap(cls: type, method_name: str) -> None:
    orig = getattr(cls, method_name, None)
    if orig is None:
        return

    def _patched(self, response: Any, *a: Any, **k: Any) -> Any:
        usage = orig(self, response, *a, **k)
        try:
            if isinstance(usage, dict):
                cost = _cost_of(response)
                if cost is not None:
                    usage.setdefault("cost", cost)  # what the AIMEAT ledger reads
        except Exception:  # noqa: BLE001 — never break the LLM call over metering
            pass
        return usage

    setattr(cls, method_name, _patched)


def install() -> None:
    """Patch CrewAI's OpenAI usage extractors to preserve OpenRouter's `cost`. Idempotent + best-effort."""
    global _installed
    if _installed:
        return
    try:
        from crewai.llms.providers.openai.completion import OpenAICompletion
    except Exception as exc:  # noqa: BLE001 — CrewAI layout changed / provider absent
        print(f"[aimeat] crewai cost patch skipped (import): {exc!r}", file=sys.stderr)
        return
    try:
        _wrap(OpenAICompletion, "_extract_openai_token_usage")  # chat.completions path (default)
        _wrap(OpenAICompletion, "_extract_responses_token_usage")  # Responses-API path
        _installed = True
        print("[aimeat] crewai cost patch active (OpenRouter usage.cost -> ledger)", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"[aimeat] crewai cost patch failed: {exc!r}", file=sys.stderr)

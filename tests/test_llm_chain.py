"""MultiProviderLLM — a provider whose LLM can't be CONSTRUCTED is skipped, not fatal to the chain.

Regression: a broken first endpoint (e.g. xai/grok-4.3 when litellm is broken) used to abort the whole
provider config, silently dropping every crew to the env fallback. Construction must skip + keep the rest.
"""

from __future__ import annotations

import pytest

from crewaimeat import llm as llmmod


def _eps(*specs):
    # spec = (label, model, context)
    return [{"label": lbl, "model": m, "context": ctx} for lbl, m, ctx in specs]


def test_skips_endpoint_that_fails_to_construct(monkeypatch):
    """First endpoint's LLM construction raises -> it is skipped; the chain uses the next one as primary."""
    constructed: list = []

    class FakeLLM:
        def __init__(self, **kw):
            if kw["model"] == "xai/grok-4.3":  # the litellm-routed model that can't init
                raise RuntimeError("Unable to initialize LLM with model 'xai/grok-4.3'")
            constructed.append(kw["model"])
            self.model = kw["model"]

        def call(self, *a, **k):
            return f"reply from {self.model}"

    monkeypatch.setattr(llmmod, "LLM", FakeLLM)
    mp = llmmod.MultiProviderLLM(
        _eps(("xai-grok:grok-4.3", "xai/grok-4.3", 131072), ("openrouter-grok", "x-ai/grok-4.3", 131072)),
        temperature=0.5,
    )
    # the broken xai endpoint was skipped; the working OpenRouter-grok is the effective primary
    assert mp.model == "x-ai/grok-4.3"
    assert mp.get_context_window_size() == 131072
    assert constructed == ["x-ai/grok-4.3"]
    assert mp.call([{"role": "user", "content": "hi"}]) == "reply from x-ai/grok-4.3"


def test_all_failing_raises_so_get_llm_can_fall_back(monkeypatch):
    """If NOTHING in the chain constructs, raise -> get_llm catches it and uses the env config."""

    class AllFail:
        def __init__(self, **kw):
            raise RuntimeError("nope")

    monkeypatch.setattr(llmmod, "LLM", AllFail)
    with pytest.raises(RuntimeError, match="no usable LLM endpoints"):
        llmmod.MultiProviderLLM(_eps(("a", "m1", 1000), ("b", "m2", 1000)), temperature=0.5)


def test_call_falls_through_on_runtime_error(monkeypatch):
    """A working endpoint that ERRORS at call time still falls through to the next (existing behaviour)."""

    class FlakyLLM:
        def __init__(self, **kw):
            self.model = kw["model"]

        def call(self, *a, **k):
            if self.model == "m1":
                raise RuntimeError("rate limited")
            return f"ok:{self.model}"

    monkeypatch.setattr(llmmod, "LLM", FlakyLLM)
    mp = llmmod.MultiProviderLLM(_eps(("a", "m1", 1000), ("b", "m2", 1000)), temperature=0.5)
    assert mp.call([{"role": "user", "content": "x"}]) == "ok:m2"

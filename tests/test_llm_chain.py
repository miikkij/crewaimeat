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


def test_model_override_honored_without_providers_file(monkeypatch):
    """A per-agent MODEL override (e.g. local Ollama) must be used even when there is NO llm_providers.json
    and NO OPENROUTER_API_KEY — the installed appliance case. Previously get_llm ignored the override unless
    a providers file existed, and crashed on the missing cloud key."""
    monkeypatch.setattr(llmmod, "_providers_file", lambda: None)  # no llm_providers.json
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("USE_XAI", raising=False)
    monkeypatch.setattr(
        llmmod,
        "agent_override",
        lambda name: {
            "kind": "model",
            "label": "ollama:gemma4:latest",
            "provider": {
                "type": "ollama",
                "name": "ollama",
                "base_url": "http://localhost:11434",
                "models": [{"id": "gemma4:latest"}],
            },
        },
    )
    captured = {}

    def _fake_mp(eps, temp):
        captured["eps"] = eps
        return "LLM"

    monkeypatch.setattr(llmmod, "MultiProviderLLM", _fake_mp)
    out = llmmod.get_llm(agent_name="news-watcher-500001")
    assert out == "LLM"  # built from the override, did NOT raise on the missing cloud key
    assert captured["eps"], "override provider produced no endpoints"

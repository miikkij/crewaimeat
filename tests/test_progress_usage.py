"""L1 tests for the AIMEAT usage-ledger telemetry mapper (LEDGER / TARGET-016).

Deterministic, no LLM, no network: exercises build_llm_usage_data, the pure mapping from a
CrewAI LLM completion's (model, usage) to the node's `llm_call` telemetry `data` payload.
"""

from crewaimeat.progress import build_llm_usage_data


def test_basic_usage_maps_tokens_and_model():
    data = build_llm_usage_data(
        "claude-sonnet-4-6",
        {"prompt_tokens": 1200, "completion_tokens": 450, "total_tokens": 1650},
    )
    assert data == {"model": "claude-sonnet-4-6", "prompt_tokens": 1200, "completion_tokens": 450}
    # Cost is intentionally omitted — the node prices it.
    assert "cost_usd" not in data
    assert "provider" not in data  # no "/" in the model


def test_litellm_provider_prefix_is_surfaced():
    data = build_llm_usage_data(
        "openrouter/anthropic/claude-opus-4",
        {"prompt_tokens": 10, "completion_tokens": 5},
    )
    assert data["provider"] == "openrouter"
    assert data["model"] == "openrouter/anthropic/claude-opus-4"


def test_input_output_token_aliases():
    data = build_llm_usage_data("gpt-4o", {"input_tokens": 300, "output_tokens": 120})
    assert data["prompt_tokens"] == 300
    assert data["completion_tokens"] == 120


def test_zero_tokens_returns_none():
    assert build_llm_usage_data("gpt-4o", {"prompt_tokens": 0, "completion_tokens": 0}) is None
    assert build_llm_usage_data("gpt-4o", {}) is None


def test_non_dict_usage_returns_none():
    assert build_llm_usage_data("gpt-4o", None) is None
    assert build_llm_usage_data("gpt-4o", "not-a-dict") is None


def test_non_numeric_tokens_returns_none():
    assert build_llm_usage_data("gpt-4o", {"prompt_tokens": "x", "completion_tokens": "y"}) is None


def test_missing_model_defaults_to_unknown():
    data = build_llm_usage_data(None, {"prompt_tokens": 5, "completion_tokens": 1})
    assert data["model"] == "unknown"


def test_partial_tokens_still_metered():
    # A completion with only output tokens (streamed) is still worth metering.
    data = build_llm_usage_data("gpt-4o", {"completion_tokens": 42})
    assert data == {"model": "gpt-4o", "prompt_tokens": 0, "completion_tokens": 42}

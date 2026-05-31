"""LLM factory.

By default OpenRouter is used (one key, many models). Alternatively you can
call xAI (Grok) directly by setting USE_XAI=1. Both go through CrewAI's LLM
class.
"""

from __future__ import annotations

import os

from crewai import LLM


def get_llm(for_tool_use: bool = True, temperature: float | None = None) -> LLM:
    """Build an LLM instance based on environment variables.

    for_tool_use=True (default) adds parallel_tool_calls=False for the tool-calling
    crews. Pass False for a plain completion (e.g. README expansion): OpenAI-compatible
    endpoints reject parallel_tool_calls when no tools are supplied.

    `temperature` overrides the LLM_TEMPERATURE env default — the task-nature gate uses this to run
    factual work cool (~0.15, not 0) and creative work warm (~0.7).
    """
    temperature = temperature if temperature is not None else float(os.getenv("LLM_TEMPERATURE", "0.5"))

    # --- Option: xAI directly ------------------------------------------
    if os.getenv("USE_XAI") not in (None, "", "0", "false", "False"):
        api_key = os.getenv("XAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "USE_XAI is on but XAI_API_KEY is missing. "
                "Set the key in the .env file."
            )
        model = os.getenv("XAI_MODEL", "xai/grok-4-fast")
        # litellm reads XAI_API_KEY automatically; we still pass it explicitly.
        return LLM(model=model, api_key=api_key, temperature=temperature)

    # --- Default: OpenRouter -------------------------------------------
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENROUTER_API_KEY is missing. Copy .env.example -> .env and fill in the key "
            "(or set USE_XAI=1 to use xAI directly)."
        )
    model = os.getenv("OPENROUTER_MODEL", "openrouter/x-ai/grok-4-fast")
    base_url = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    kwargs: dict = dict(model=model, base_url=base_url, api_key=api_key, temperature=temperature)
    if for_tool_use:
        # Disable parallel tool calls within a single turn: AIMEAT writes
        # (e.g. aimeat_task_todo) mutate shared task state, and calls batched in
        # the same turn race on the server (read-modify-write of the whole task)
        # -> writes are lost. False forces the model to one tool call at a time.
        kwargs["additional_params"] = {"parallel_tool_calls": False}
    return LLM(**kwargs)

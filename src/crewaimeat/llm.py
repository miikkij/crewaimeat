"""LLM factory.

Two ways to choose models:

1. **Env (simple).** Default OpenRouter (one key, many models); `USE_XAI=1` for xAI directly. A single
   `OPENROUTER_MODEL` plus an optional `OPENROUTER_FALLBACK_MODELS` chain (OpenRouter's own `models` array,
   max 3, same-provider only).

2. **Provider config (OpenClaw-style, recommended).** Drop an `llm_providers.json` in the working dir (or
   point `LLM_PROVIDERS_FILE` at one) and `get_llm` builds a `MultiProviderLLM` that tries PROVIDERS in
   priority order, and within each provider its MODELS in priority order — falling through on ANY error,
   **across providers** (OpenRouter → another free → local Ollama → xAI). You decide the order; a paid model
   is used only if you list it. Each model carries its own **context window** so CrewAI never over-fills a
   small-context model (local Ollama is ~32k; the free OpenRouter models are 128k–256k, far under owl-alpha's
   1M). See `llm_providers.example.json`.

Both go through CrewAI's LLM. The provider config takes precedence when present.
"""

from __future__ import annotations

import json
import os
import sys

from crewai import LLM
from crewai.llms.base_llm import BaseLLM
from pydantic import PrivateAttr

# provider type -> litellm model-id prefix
_PREFIX = {"openrouter": "openrouter/", "ollama": "ollama/", "xai": "xai/", "openai": "", "generic": ""}
_DEFAULT_BASE = {"openrouter": "https://openrouter.ai/api/v1", "ollama": "http://localhost:11434"}
# fallback context window if a model/provider declares none (conservative)
_FALLBACK_CONTEXT = 32768


def _providers_file() -> str | None:
    """Path to the provider-config JSON, or None. LLM_PROVIDERS_FILE wins; else ./llm_providers.json."""
    p = os.getenv("LLM_PROVIDERS_FILE") or "llm_providers.json"
    return p if os.path.isfile(p) else None


def _flatten_endpoints(cfg: dict, for_tool_use: bool) -> list[dict]:
    """Turn the provider config into a flat, ordered list of endpoints (provider-major, model-minor).

    A provider whose `api_key_env` is set but missing is skipped (logged), not fatal — so a machine with no
    OpenRouter key still runs on its local Ollama provider. Each model may be a plain id string or an object
    `{"id": ..., "context": N}`; a provider-level `"context"` is the default for its string models.
    """
    eps: list[dict] = []
    for prov in cfg.get("providers", []):
        if not prov.get("enabled", True):
            continue
        ptype = (prov.get("type") or "openrouter").lower()
        prefix = _PREFIX.get(ptype, "")
        api_key = None
        keyenv = prov.get("api_key_env")
        if keyenv:
            api_key = os.getenv(keyenv)
            if not api_key:
                print(f"[llm] provider '{prov.get('name', ptype)}' skipped: env {keyenv} not set", file=sys.stderr)
                continue
        base_url = prov.get("base_url") or _DEFAULT_BASE.get(ptype)
        prov_ctx = prov.get("context")
        for m in prov.get("models", []):
            mid = m.get("id") if isinstance(m, dict) else m
            ctx = (m.get("context") if isinstance(m, dict) else None) or prov_ctx or _FALLBACK_CONTEXT
            lm = mid if (not prefix or mid.startswith(prefix)) else prefix + mid
            ap: dict = {}
            if for_tool_use and ptype != "ollama":
                ap["parallel_tool_calls"] = False  # AIMEAT task writes race if batched in one turn
            eps.append({
                "label": f"{prov.get('name', ptype)}:{mid}",
                "model": lm, "base_url": base_url, "api_key": api_key,
                "context": int(ctx), "additional_params": ap,
            })
    return eps


class MultiProviderLLM(BaseLLM):
    """A CrewAI LLM that tries an ordered list of (provider, model) endpoints, falling through on any error.

    Composition, not inheritance of `LLM` (whose factory would re-dispatch a subclass): it holds one real
    `LLM` per endpoint and, on each `call`, walks them in priority order — so a local Ollama model can back
    up an OpenRouter/xAI one (or vice versa). `get_context_window_size` returns the PRIMARY (first)
    endpoint's window, so CrewAI uses the model-in-use's real limit (e.g. grok's 1M, not the 32k Ollama
    floor); a rare fallback to a much smaller model is best-effort (an over-long prompt errors there and the
    chain falls through).
    """

    _llms: list = PrivateAttr(default_factory=list)
    _labels: list = PrivateAttr(default_factory=list)
    _context_window: int = PrivateAttr(default=_FALLBACK_CONTEXT)

    def __init__(self, endpoints: list[dict], temperature: float):
        super().__init__(model=endpoints[0]["model"], temperature=temperature)
        llms, labels = [], []
        for ep in endpoints:
            # NB: context is enforced via get_context_window_size() below (the outer LLM CrewAI queries),
            # not as a constructor kwarg — the concrete completion class would leak it into the API call.
            kw: dict = dict(model=ep["model"], temperature=temperature)
            if ep.get("base_url"):
                kw["base_url"] = ep["base_url"]
            if ep.get("api_key"):
                kw["api_key"] = ep["api_key"]
            if ep.get("additional_params"):
                kw["additional_params"] = ep["additional_params"]
            llms.append(LLM(**kw))
            labels.append(ep["label"])
        self._llms = llms
        self._labels = labels
        self._context_window = endpoints[0]["context"]  # the primary (model used ~always)

    def call(self, *args, **kwargs):
        last: Exception | None = None
        for i, (llm, label) in enumerate(zip(self._llms, self._labels)):
            try:
                return llm.call(*args, **kwargs)
            except Exception as e:  # fall through to the next endpoint
                last = e
                more = i + 1 < len(self._llms)
                print(f"[llm] endpoint '{label}' failed ({type(e).__name__}); "
                      f"{'falling back to next' if more else 'no more endpoints'}", file=sys.stderr)
        assert last is not None
        raise last

    def get_context_window_size(self) -> int:
        return self._context_window

    def supports_function_calling(self) -> bool:
        try:
            return self._llms[0].supports_function_calling()
        except Exception:
            return True

    def supports_stop_words(self) -> bool:
        try:
            return self._llms[0].supports_stop_words()
        except Exception:
            return True


def get_llm(for_tool_use: bool = True, temperature: float | None = None) -> BaseLLM:
    """Build an LLM instance.

    for_tool_use=True (default) adds parallel_tool_calls=False for the tool-calling crews. Pass False for a
    plain completion (e.g. README expansion): OpenAI-compatible endpoints reject parallel_tool_calls when no
    tools are supplied.

    `temperature` overrides the LLM_TEMPERATURE env default — the task-nature gate uses this to run factual
    work cool (~0.15) and creative work warm (~0.7).
    """
    temperature = temperature if temperature is not None else float(os.getenv("LLM_TEMPERATURE", "0.5"))

    # --- Provider config (priority chain across providers + models) — wins when present ---
    pf = _providers_file()
    if pf:
        try:
            cfg = json.loads(open(pf, encoding="utf-8").read())
            eps = _flatten_endpoints(cfg, for_tool_use)
            if eps:
                return MultiProviderLLM(eps, temperature)
            print(f"[llm] {pf}: no usable endpoints; using env config", file=sys.stderr)
        except Exception as e:
            print(f"[llm] failed to load {pf} ({e}); using env config", file=sys.stderr)

    # --- Option: xAI directly ------------------------------------------
    if os.getenv("USE_XAI") not in (None, "", "0", "false", "False"):
        api_key = os.getenv("XAI_API_KEY")
        if not api_key:
            raise RuntimeError("USE_XAI is on but XAI_API_KEY is missing. Set the key in the .env file.")
        model = os.getenv("XAI_MODEL", "xai/grok-4-fast")
        return LLM(model=model, api_key=api_key, temperature=temperature)

    # --- Default: OpenRouter -------------------------------------------
    api_key = os.getenv("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENROUTER_API_KEY is missing. Copy .env.example -> .env and fill in the key "
            "(or set USE_XAI=1 to use xAI directly, or add an llm_providers.json)."
        )
    model = os.getenv("OPENROUTER_MODEL", "openrouter/x-ai/grok-4-fast")
    base_url = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    kwargs: dict = dict(model=model, base_url=base_url, api_key=api_key, temperature=temperature)
    additional: dict = {}
    if for_tool_use:
        additional["parallel_tool_calls"] = False
    fallback = [m.strip() for m in os.getenv("OPENROUTER_FALLBACK_MODELS", "").split(",") if m.strip()]
    if fallback:
        additional["extra_body"] = {"models": fallback[:3]}  # OpenRouter caps the models array at 3
    if additional:
        kwargs["additional_params"] = additional
    return LLM(**kwargs)

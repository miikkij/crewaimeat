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


# ── per-agent model override (runtime, set from the TUI; gitignored under AIMEAT_HOME) ──────────
# A user can pin ONE agent to a specific model (or named profile) without editing the committed
# llm_providers.json. The override lives in <AIMEAT_HOME>/llm_overrides.json so every process that
# shares the home (the crew daemons + the TUI) reads the same file. Schema:
#   {"<agent>": {"kind": "model",   "label": "openrouter:openai/gpt-oss-120b",
#                "provider": {<one-model provider dict for _flatten_endpoints>}},
#    "<agent>": {"kind": "profile", "profile": "coding"}}
# The override is self-contained (the model entry carries its own provider/base_url/api_key_env/
# context) so resolving it needs no cross-reference to llm_providers.json.


def _overrides_file() -> str:
    """Path of the per-agent override store (<AIMEAT_HOME>/llm_overrides.json)."""
    from crewaimeat._home import aimeat_home

    return str(aimeat_home() / "llm_overrides.json")


def load_overrides() -> dict:
    """All per-agent overrides ({} when the file is absent/unreadable — overrides are optional)."""
    p = _overrides_file()
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def agent_override(agent_name: str | None) -> dict | None:
    """The override for one agent, or None."""
    ov = load_overrides().get(agent_name or "")
    return ov if isinstance(ov, dict) else None


def save_override(agent_name: str, spec: dict) -> None:
    """Pin `agent_name` to `spec` ({"kind": "model"|"profile", ...}) and persist. Creates the home dir."""
    p = _overrides_file()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    data = load_overrides()
    data[agent_name] = spec
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)


def clear_override(agent_name: str) -> bool:
    """Remove `agent_name`'s override (revert to llm_providers.json routing). True if one was removed."""
    data = load_overrides()
    if agent_name not in data:
        return False
    del data[agent_name]
    with open(_overrides_file(), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, ensure_ascii=False)
    return True


def available_models(cfg: dict | None = None) -> list[dict]:
    """The catalogue a picker offers: every distinct (provider-type, model-id) across all profiles
    (and a flat `providers` list), de-duplicated, each carrying a self-contained one-model provider
    dict ready to store as a `model` override. Pure data (reads cfg, not the network)."""
    if cfg is None:
        pf = _providers_file()
        cfg = {}
        if pf and os.path.isfile(pf):
            try:
                with open(pf, encoding="utf-8") as fh:
                    cfg = json.load(fh)
            except (OSError, ValueError):
                cfg = {}
    chains: list[list] = []
    profiles = cfg.get("profiles")
    if isinstance(profiles, dict):
        for prof in profiles.values():
            if isinstance(prof, dict):
                chains.append(prof.get("providers") or [])
    if cfg.get("providers"):
        chains.append(cfg.get("providers") or [])
    out: list[dict] = []
    seen: set = set()
    for providers in chains:
        for prov in providers or []:
            ptype = (prov.get("type") or "openrouter").lower()
            name = prov.get("name") or ptype
            base_url = prov.get("base_url") or _DEFAULT_BASE.get(ptype)
            keyenv = prov.get("api_key_env")
            prov_ctx = prov.get("context")
            for m in prov.get("models") or []:
                mid = m.get("id") if isinstance(m, dict) else m
                if not mid:
                    continue
                ctx = (m.get("context") if isinstance(m, dict) else None) or prov_ctx or _FALLBACK_CONTEXT
                key = (ptype, mid)
                if key in seen:
                    continue
                seen.add(key)
                single = {"type": ptype, "name": name, "models": [{"id": mid, "context": int(ctx)}]}
                if base_url:
                    single["base_url"] = base_url
                if keyenv:
                    single["api_key_env"] = keyenv
                out.append(
                    {
                        "label": f"{name}:{mid}",
                        "type": ptype,
                        "name": name,
                        "id": mid,
                        "context": int(ctx),
                        "base_url": base_url,
                        "api_key_env": keyenv,
                        "provider": single,
                    }
                )
    return out


def _select_chain(cfg: dict, agent_name: str | None) -> tuple[list, str]:
    """Pick the provider chain for a crew.

    NEW format — per-crew profiles:
        {"profiles": {"content": {"providers": [...]}, "coding": {"providers": [...]}},
         "default": "content", "crews": {"aimeat-app-builder": "coding", ...}}
    A crew listed in `crews` uses that profile; everything else uses `default`. So content crews route to
    grok and code crews route to a real coder.

    OLD format — one flat chain for all crews: {"providers": [...]} (still supported).

    A per-agent OVERRIDE (set from the TUI, stored under AIMEAT_HOME) wins over both formats — it
    pins one agent to a specific model or a named profile. See load_overrides().

    Returns (providers_list, profile_label).
    """
    ov = agent_override(agent_name)
    if ov:
        if ov.get("kind") == "model" and isinstance(ov.get("provider"), dict):
            return ([ov["provider"]], f"override:{ov.get('label', 'model')}")
        if ov.get("kind") == "profile":
            prof = (cfg.get("profiles") or {}).get(ov.get("profile"))
            if isinstance(prof, dict):
                return ((prof.get("providers") or []), f"override-profile:{ov.get('profile')}")
    profiles = cfg.get("profiles")
    if isinstance(profiles, dict) and profiles:
        name = (cfg.get("crews") or {}).get(agent_name or "") or cfg.get("default") or next(iter(profiles))
        prof = profiles.get(name)
        if not isinstance(prof, dict):  # bad mapping → fall back to default, then first profile
            name = cfg.get("default") or next(iter(profiles))
            prof = profiles.get(name) or next(iter(profiles.values()))
        return ((prof or {}).get("providers") or [], str(name))
    return (cfg.get("providers") or [], "providers")


def _flatten_endpoints(providers: list, for_tool_use: bool) -> list[dict]:
    """Turn a provider list (one profile's chain) into a flat, ordered list of endpoints (provider-major,
    model-minor).

    A provider whose `api_key_env` is set but missing is skipped (logged), not fatal — so a machine with no
    OpenRouter key still runs on its local Ollama provider. Each model may be a plain id string or an object
    `{"id": ..., "context": N}`; a provider-level `"context"` is the default for its string models.
    """
    eps: list[dict] = []
    for prov in providers or []:
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
            eps.append(
                {
                    "label": f"{prov.get('name', ptype)}:{mid}",
                    "model": lm,
                    "base_url": base_url,
                    "api_key": api_key,
                    "context": int(ctx),
                    "additional_params": ap,
                }
            )
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
        llms, labels, models, contexts = [], [], [], []
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
            # SKIP an endpoint that can't even be CONSTRUCTED (e.g. a litellm-routed model like xai/grok-4.3
            # when litellm is broken) and keep the rest of the chain — otherwise one bad provider would abort
            # the whole config and silently drop EVERY crew to the env fallback (this hid a 2-week outage).
            try:
                llm = LLM(**kw)
            except Exception as e:  # noqa: BLE001
                print(
                    f"[llm] endpoint '{ep['label']}' could not initialise ({type(e).__name__}); skipping",
                    file=sys.stderr,
                )
                continue
            llms.append(llm)
            labels.append(ep["label"])
            models.append(ep["model"])
            contexts.append(ep["context"])
        if not llms:  # nothing usable in the whole chain → let get_llm fall back to env config
            raise RuntimeError("no usable LLM endpoints in the provider chain (all failed to initialise)")
        super().__init__(model=models[0], temperature=temperature)
        self._llms = llms
        self._labels = labels
        self._context_window = contexts[0]  # the first ENDPOINT THAT INITIALISED (the effective primary)

    def call(self, *args, **kwargs):
        last: Exception | None = None
        for i, (llm, label) in enumerate(zip(self._llms, self._labels)):
            try:
                return llm.call(*args, **kwargs)
            except Exception as e:  # fall through to the next endpoint
                last = e
                more = i + 1 < len(self._llms)
                print(
                    f"[llm] endpoint '{label}' failed ({type(e).__name__}); "
                    f"{'falling back to next' if more else 'no more endpoints'}",
                    file=sys.stderr,
                )
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


def get_llm(for_tool_use: bool = True, temperature: float | None = None, agent_name: str | None = None) -> BaseLLM:
    """Build an LLM instance.

    for_tool_use=True (default) adds parallel_tool_calls=False for the tool-calling crews. Pass False for a
    plain completion (e.g. README expansion): OpenAI-compatible endpoints reject parallel_tool_calls when no
    tools are supplied.

    `temperature` overrides the LLM_TEMPERATURE env default — the task-nature gate uses this to run factual
    work cool (~0.15) and creative work warm (~0.7).

    `agent_name` selects the per-crew provider profile from llm_providers.json (e.g. content crews -> grok,
    code crews -> a real coder). When omitted (the deterministic content pipelines call get_llm() directly),
    the `default` profile is used.
    """
    temperature = temperature if temperature is not None else float(os.getenv("LLM_TEMPERATURE", "0.5"))

    # --- Per-agent MODEL override (e.g. the agency's local-Ollama pick) — wins over everything, needs NO
    # llm_providers.json. This MUST be honored here: previously a model override was only consulted inside
    # the providers-file branch below, so on a machine WITHOUT llm_providers.json (the installed appliance!)
    # the Ollama choice was silently ignored and we fell through to the cloud OPENROUTER_API_KEY → crash.
    # (A "profile" override still needs the providers file to resolve, so it stays in _select_chain.)
    ov = agent_override(agent_name)
    if ov and ov.get("kind") == "model" and isinstance(ov.get("provider"), dict):
        eps = _flatten_endpoints([ov["provider"]], for_tool_use)
        if eps:
            print(
                f"[llm] {agent_name or '?'} -> override {ov.get('label', 'model')} (no providers file needed)",
                file=sys.stderr,
            )
            return MultiProviderLLM(eps, temperature)

    # --- Provider config (per-crew profile -> priority chain across providers + models) — wins when present ---
    pf = _providers_file()
    if pf:
        try:
            cfg = json.loads(open(pf, encoding="utf-8").read())
            providers, profile = _select_chain(cfg, agent_name)
            eps = _flatten_endpoints(providers, for_tool_use)
            if eps:
                if agent_name:
                    print(f"[llm] {agent_name} -> profile '{profile}' (primary {eps[0]['label']})", file=sys.stderr)
                return MultiProviderLLM(eps, temperature)
            print(f"[llm] {pf}: profile '{profile}' has no usable endpoints; using env config", file=sys.stderr)
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

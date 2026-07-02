"""Embedder cascade for opt-in CrewAI crew memory.

Deliverable-1 companion to `CrewSpec.memory`. When a crew opts into CrewAI's built-in (LanceDB-backed)
memory, it needs an EMBEDDER to vectorize what it remembers. This module picks one by PROBING an ordered
list of tiers and returning the first reachable — mirroring `llm.MultiProviderLLM` (probe availability,
fall through on failure, LOG LOUD which one is used, and NEVER silently default). If nothing is reachable
it raises LOUD with an actionable message — a crew that asked for memory must not silently run stateless.

Tiers (default order), cost-vs-privacy tunable:
  1. **ollama**  — local, free, private. Reachable = the ollama daemon is up AND the embed model is pulled.
  2. **nvidia**  — free cloud (NVIDIA NIM, OpenAI-compatible). Reachable = NVIDIA_API_KEY is set.
  3. **qwen**    — paid, private cloud (DashScope, OpenAI-compatible). Reachable = DASHSCOPE_API_KEY is set.

The `bias` flag reorders/filters this list (testers value money over privacy):
  - "privacy" (default): ollama first, then qwen; the free-but-cloud nvidia tier is DROPPED.
  - "cost":              ollama first, then the FREE nvidia tier, then paid qwen — never a paid endpoint
    when a free one exists.

CrewAI's embedder config is a declarative dict (`{"provider", "config"}`), so the "cascade" resolves to
ONE such dict at crew-build time (a build-time probe-and-select); the analysis LLM CrewAI's memory also
needs is wired separately in the scaffold via the crew's own `get_llm` chain (never the OpenAI default).

Storage is scoped per **owner / agent / principal** under AIMEAT_HOME so crews never cross-read each
other's memory and a DM-serviceable crew keeps each federation requester isolated. See
`memory_store_path` + `resolve_principal`. All logging is to stderr (stdout is the connector's channel)
and stays ASCII (a Windows cp1252 console rejects fancy arrows).
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# NVIDIA NIM + DashScope are both OpenAI-compatible embedding endpoints, so they ride CrewAI's `openai`
# embedder provider with a custom `api_base` (there is NO native `nvidia` provider in crewai 1.14.x).
_NVIDIA_BASE = "https://integrate.api.nvidia.com/v1"
_QWEN_BASE = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"

# Per-tier default embedding models (override via env). gemma/qwen CHAT models are NOT embedders — the
# ollama tier needs an actual embedding model pulled (default nomic-embed-text).
_OLLAMA_MODEL = os.getenv("AIMEAT_EMBED_OLLAMA_MODEL", "nomic-embed-text")
_NVIDIA_MODEL = os.getenv("AIMEAT_EMBED_NVIDIA_MODEL", "nvidia/nv-embedqa-e5-v5")
_QWEN_MODEL = os.getenv("AIMEAT_EMBED_QWEN_MODEL", "text-embedding-v3")


def _seg(s: str) -> str:
    """Coerce a path segment to a safe slug: [a-z0-9._-], collapse the rest, never empty and never `..`.

    Owner names / agent ids / federation ghii can carry chars unsafe for a directory; this guarantees no
    '/', '\\', '..' or empty component can escape the storage base. Mirrors brains.slug_agent_name."""
    out = re.sub(r"[^a-z0-9._-]+", "-", (s or "").lower()).strip("-._")
    return out or "x"


def _ollama_base() -> str:
    """Ollama base URL (OLLAMA_HOST wins), no trailing slash. Same convention as agency/cockpit."""
    return (os.getenv("OLLAMA_HOST") or "http://localhost:11434").rstrip("/")


def _ollama_reachable(model: str) -> tuple[bool, str]:
    """True iff the ollama daemon is up AND `model` (an embedding model) is pulled. Fail-soft probe.

    Reuses agency.cockpit._ollama_probe (a 2s GET /api/tags) so there is ONE ollama probe in the repo;
    additionally requires the embed model to be present (a running daemon without the model can't embed)."""
    try:
        from crewaimeat.agency.cockpit import _ollama_probe
    except Exception as exc:  # noqa: BLE001 — cockpit unimportable -> treat ollama as unavailable
        return False, f"ollama probe unavailable ({type(exc).__name__})"
    try:
        running, names = _ollama_probe()
    except Exception as exc:  # noqa: BLE001
        return False, f"ollama probe error ({type(exc).__name__})"
    if not running:
        return False, f"ollama daemon not reachable at {_ollama_base()}"
    if not any(n == model or n.startswith(model + ":") for n in names):
        return False, f"embed model '{model}' not pulled (run: ollama pull {model})"
    return True, "ok"


def _tier_embedder(tier: str) -> tuple[dict, str] | None:
    """The CrewAI embedder dict + a short storage tag for one tier (independent of reachability). None for
    an unknown tier. The tag is folded into the storage path so switching tiers (different vector dims)
    never corrupts an existing LanceDB table."""
    if tier == "ollama":
        # Ride ollama's OpenAI-COMPATIBLE endpoint (/v1) via crewai's `openai` provider rather than the
        # native `ollama` provider: the native one pulls in the extra `ollama` python package (chromadb's
        # OllamaEmbeddingFunction hard-requires it), whereas `openai` is already installed and lets all
        # three tiers share ONE code path. A non-empty dummy api_key satisfies the OpenAI client.
        return (
            {
                "provider": "openai",
                "config": {"api_key": "ollama", "api_base": f"{_ollama_base()}/v1", "model_name": _OLLAMA_MODEL},
            },
            f"ollama-{_seg(_OLLAMA_MODEL)}",
        )
    if tier == "nvidia":
        return (
            {
                "provider": "openai",
                "config": {
                    "api_key": os.getenv("NVIDIA_API_KEY", ""),
                    "api_base": _NVIDIA_BASE,
                    "model_name": _NVIDIA_MODEL,
                },
            },
            f"nvidia-{_seg(_NVIDIA_MODEL)}",
        )
    if tier == "qwen":
        return (
            {
                "provider": "openai",
                "config": {
                    "api_key": os.getenv("DASHSCOPE_API_KEY", ""),
                    "api_base": _QWEN_BASE,
                    "model_name": _QWEN_MODEL,
                },
            },
            f"qwen-{_seg(_QWEN_MODEL)}",
        )
    return None


def _tier_reachable(tier: str) -> tuple[bool, str]:
    """Is this tier usable right now? ollama = daemon up + model pulled; cloud tiers = their key is set."""
    if tier == "ollama":
        return _ollama_reachable(_OLLAMA_MODEL)
    if tier == "nvidia":
        return (True, "ok") if os.getenv("NVIDIA_API_KEY") else (False, "NVIDIA_API_KEY not set")
    if tier == "qwen":
        return (True, "ok") if os.getenv("DASHSCOPE_API_KEY") else (False, "DASHSCOPE_API_KEY not set")
    return False, f"unknown tier '{tier}'"


def _ordered_tiers(bias: str) -> list[str]:
    """Cascade order for a bias. ollama (free+local) is always first; the bias decides the cloud tail:
    privacy drops the free-but-cloud nvidia tier; cost promotes free nvidia ahead of paid qwen."""
    return ["ollama", "nvidia", "qwen"] if bias == "cost" else ["ollama", "qwen"]


def _resolve_bias(bias: str | None) -> str:
    b = (bias or os.getenv("EMBEDDER_BIAS", "privacy")).strip().lower()
    return b if b in ("privacy", "cost") else "privacy"


def _embedder_tag(embedder: dict) -> str:
    """A storage tag for an explicit override embedder dict (provider + model)."""
    cfg = embedder.get("config") or {}
    model = cfg.get("model_name") or cfg.get("model") or "model"
    return f"{_seg(str(embedder.get('provider') or 'custom'))}-{_seg(str(model))}"


def memory_preflight(bias: str | None = None) -> tuple[bool, str]:
    """Is ANY embedder tier reachable for the given bias? (ok, human reason). Non-raising — used by
    crew-forge to SURFACE the memory prerequisite at build time without gating (memory is a runtime
    concern). `run_crew` uses `resolve_embedder`, which fails loud instead."""
    bias = _resolve_bias(bias)
    tried = []
    for tier in _ordered_tiers(bias):
        ok, reason = _tier_reachable(tier)
        if ok:
            emb = _tier_embedder(tier)
            tag = emb[1] if emb else tier
            return True, f"embedder tier '{tier}' ({tag}) reachable [bias={bias}]"
        tried.append(f"{tier}: {reason}")
    return False, f"no embedder reachable [bias={bias}] -> " + "; ".join(tried)


def resolve_embedder(
    agent_name: str | None = None, *, bias: str | None = None, override: dict | None = None
) -> tuple[dict, str]:
    """Pick a CrewAI embedder for a memory-opted crew by probing tiers in bias order. Returns
    (embedder_dict, storage_tag). An explicit `override` dict bypasses the cascade. LOGS LOUD which tier
    is used; RAISES a loud, actionable RuntimeError if none is reachable (fail-loud: a crew that asked for
    memory must not silently run stateless)."""
    if override:
        tag = _embedder_tag(override)
        print(f"[embed] {agent_name or '?'} -> explicit memory_embedder override ({tag})", file=sys.stderr)
        return override, tag

    bias = _resolve_bias(bias)
    order = _ordered_tiers(bias)
    tried: list[str] = []
    for i, tier in enumerate(order):
        ok, reason = _tier_reachable(tier)
        if ok:
            emb, tag = _tier_embedder(tier)  # type: ignore[misc]
            print(f"[embed] {agent_name or '?'} -> tier '{tier}' ({tag}) [bias={bias}]", file=sys.stderr)
            return emb, tag
        tried.append(f"{tier}: {reason}")
        more = i + 1 < len(order)
        print(
            f"[embed] tier '{tier}' unavailable ({reason}); {'falling through to next' if more else 'no more tiers'}",
            file=sys.stderr,
        )
    raise RuntimeError(
        "crew memory is ON but NO embedder is reachable. Tried -> " + "; ".join(tried) + ". Fix one: "
        f"(1) start ollama and `ollama pull {_OLLAMA_MODEL}` (free, local, private); "
        "(2) set NVIDIA_API_KEY (free cloud) and use embedder_bias='cost'; or "
        "(3) set DASHSCOPE_API_KEY (qwen, paid, private)."
    )


# --------------------------------------------------------------------------- #
# Principal + scoped storage path
# --------------------------------------------------------------------------- #
def resolve_principal(task: dict) -> str:
    """Who/what this invocation is FOR — the isolation key so a crew never recalls the wrong caller's
    memories. A DM from a federation peer isolates by the peer's ghii; a delegated/workflow task isolates
    by its requester; an owner-queued task shares the owner's own brain. Unknown -> "owner" (same-owner,
    never a cross-owner leak). This is the correctness boundary the owner asked for."""
    task = task or {}
    src = task.get("_source")
    if src == "dm":
        return _seg(str(task.get("_dm_sender") or "dm-peer"))
    if src == "message":
        orig = task.get("_original") or {}
        s = orig.get("from") or orig.get("sender") or orig.get("from_agent")
        if s:
            return _seg(str(s))
    for k in ("requestedBy", "createdBy", "requester", "delegatedBy", "from"):
        v = task.get(k)
        if v:
            return _seg(str(v))
    return "owner"


def _discover_owner_safe(agent_name: str) -> str | None:
    """Owner for the storage top-segment: the token-filename owner, else AIMEAT_OWNER. Never raises."""
    try:
        from crewaimeat.generator_tool import _discover_owner

        return _discover_owner(agent_name)
    except Exception as exc:  # noqa: BLE001 — owner discovery is best-effort; caller falls back to "self"
        # LOG LOUD: memory would land under a fallback owner subtree; an operator should see this.
        print(
            f"[embed] owner discovery failed for {agent_name} ({type(exc).__name__}); "
            f"falling back to AIMEAT_OWNER/self for the memory storage path",
            file=sys.stderr,
        )
        return os.getenv("AIMEAT_OWNER")


def memory_store_path(
    agent_name: str,
    *,
    owner: str | None = None,
    principal: str = "owner",
    embedder_tag: str = "embed",
    scope: str = "principal",
    session: str | None = None,
) -> Path:
    """The scoped CrewAI-memory storage dir under AIMEAT_HOME. Layout:

        <AIMEAT_HOME>/crew_memory/<owner>/<agent>/<...scope...>/<embedder-tag>/

    - `<owner>` is ALWAYS the top segment and we never read across owners (the hard privacy wall).
    - scope "principal" (default): `.../<principal>/<tag>` — isolated per caller, accumulates across runs.
    - scope "agent": `.../_shared/<tag>` — one brain across all the owner's callers (opt-in accumulator).
    - scope "session": `.../<principal>/<session>/<tag>` — ephemeral, resets per task.

    Passed to `Memory(storage=str(path))` PER CREW (not the global CREWAI_STORAGE_DIR env) so it is
    thread-safe under the threaded fleet_host. Creates the dir. `.aimeat/` is gitignored."""
    from crewaimeat._home import aimeat_home

    owner_seg = _seg(owner or _discover_owner_safe(agent_name) or "self")
    base = aimeat_home() / "crew_memory" / owner_seg / _seg(agent_name)
    tag = _seg(embedder_tag)
    if scope == "agent":
        d = base / "_shared" / tag
    elif scope == "session":
        d = base / _seg(principal) / _seg(session or "manual") / tag
    else:  # "principal" (default) — anything unrecognized is treated as the safe default
        d = base / _seg(principal) / tag
    d.mkdir(parents=True, exist_ok=True)
    return d

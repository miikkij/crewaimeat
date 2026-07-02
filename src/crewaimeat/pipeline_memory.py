"""Semantic-memory primitives for DETERMINISTIC pipelines (Sanomat, briefings, forge precedent).

`CrewSpec.memory=True` serves LLM crews (auto recall/save inside `Crew.kickoff`). Deterministic
pipelines — where code drives the loop and the model writes only prose — need direct primitives:
REMEMBER one published artifact, RECALL prior art for a prompt, DEDUP-check a candidate against
what was already said. This module wraps the same stack as the crew path (cascade embedder +
scoped LanceDB store under AIMEAT_HOME, see `embedder_cascade`) behind a small API:

    store = open_store("laimeat-sanomat")                    # None (logged loud) if no embedder
    if store:
        block = store.prior_art_block(todays_topic, k=3)     # "" when nothing relevant
        ...inject block into the draft prompt...
        store.remember(final_text, source="editorial", metadata={"date": day})
        dup = store.dedup_check(candidate_item)              # .is_dup at semantic threshold

Availability contract (deliberate, LOGGED-LOUD degradation — not a silent fallback): a nightly
paper must still ship when the embedder host is down, so `open_store` returns None when no tier is
reachable and each helper method survives a backend error by logging the REAL cause to stderr and
returning its empty value. Pipelines that would rather crash pass `required=True`. LLM crews keep
using `CrewSpec.memory`, which fails loud in `run_crew`.

Scoring: stores are built semantic-only (recency/importance weights zeroed) so `MemoryMatch.score`
IS the semantic similarity — that makes `dedup_check` thresholds meaningful and prior-art ranking
purely topical. The encode-analysis LLM defaults to a LOCAL ollama model when the ollama embedder
tier was selected (so remembering costs $0 and never routes to a paid content profile like grok);
override with AIMEAT_MEMORY_ANALYSIS_MODEL or the `analysis_llm` param. ASCII-only stderr logging.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any

from crewaimeat.embedder_cascade import memory_store_path, resolve_embedder


def _memory_cls():  # seam for offline tests (monkeypatch this, never the crewai internals)
    from crewai.memory.unified_memory import Memory

    return Memory


def _default_analysis_llm(agent_name: str, embedder_tag: str) -> Any:
    """The LLM crewai's encoder uses to infer scope/categories/importance on remember().

    When the cascade picked local ollama, analysis rides a local model too (free, private —
    a content crew's get_llm profile may route to a PAID prose model like grok, which must never
    be spent on background metadata inference). Cloud embedder tiers fall back to the crew's own
    get_llm chain, logged."""
    if embedder_tag.startswith("ollama-"):
        from crewai import LLM

        model = os.getenv("AIMEAT_MEMORY_ANALYSIS_MODEL", "ollama/gemma4:latest")
        base = (os.getenv("OLLAMA_HOST") or "http://localhost:11434").rstrip("/")
        # max_tokens caps OBSERVED runaway generations (gemma4 occasionally loops on the encode-analysis
        # until the 65k limit ~ 10 GPU-minutes, then crewai falls back to defaults anyway). The analysis
        # output is a small metadata JSON, so 2048 never truncates a healthy response — a runaway just
        # fails 30x faster into the same defaults path. (NOT the reasoning-model max_tokens trap: this
        # is a plain instruct model emitting JSON, not burning budget on reasoning tokens.)
        return LLM(model=model, temperature=0.1, base_url=base, max_tokens=2048)
    from crewaimeat.llm import get_llm

    print(
        f"[pipemem] {agent_name}: non-ollama embedder tier ({embedder_tag}) -> "
        f"encode analysis uses the crew's own get_llm chain",
        file=sys.stderr,
    )
    return get_llm(agent_name=agent_name)


@dataclass
class MemoryHit:
    content: str
    score: float  # semantic similarity (recency/importance zeroed at store construction)
    metadata: dict = field(default_factory=dict)


@dataclass
class DedupResult:
    is_dup: bool
    best_score: float = 0.0
    best_content: str = ""
    best_metadata: dict = field(default_factory=dict)


class PipelineStore:
    """One agent's pipeline memory. Construct via `open_store` (never directly)."""

    def __init__(self, agent_name: str, mem: Any) -> None:
        self.agent_name = agent_name
        self._mem = mem

    # -- write ---------------------------------------------------------------
    def remember(self, text: str, *, source: str | None = None, metadata: dict | None = None) -> bool:
        """Store one artifact. True if saved; False (logged loud) on a backend error."""
        text = (text or "").strip()
        if not text:
            return False
        try:
            self._mem.remember(text, source=source, metadata=metadata or {})
            return True
        except Exception as exc:  # noqa: BLE001 — the availability contract: degrade loud, never kill the pipeline
            print(f"[pipemem] {self.agent_name}: remember FAILED ({type(exc).__name__}: {exc})", file=sys.stderr)
            return False

    # -- read ----------------------------------------------------------------
    def recall(self, query: str, k: int = 5, *, category: str | None = None) -> list[MemoryHit]:
        """Top-k semantically similar stored artifacts. [] (logged loud) on a backend error.

        `category` post-filters on the remember-time metadata "category" — one store per crew can
        serve several newspaper sections without a koodaus tip surfacing as pelit prior-art
        (over-fetches so the filtered list still fills k)."""
        try:
            matches = self._mem.recall(query, depth="shallow", limit=k if category is None else max(k * 4, k))
        except Exception as exc:  # noqa: BLE001
            print(f"[pipemem] {self.agent_name}: recall FAILED ({type(exc).__name__}: {exc})", file=sys.stderr)
            return []
        out: list[MemoryHit] = []
        for m in matches:
            rec = getattr(m, "record", m)
            hit = MemoryHit(
                content=str(getattr(rec, "content", "") or ""),
                score=float(getattr(m, "score", 0.0) or 0.0),
                metadata=dict(getattr(rec, "metadata", None) or {}),
            )
            if category is None or hit.metadata.get("category") == category:
                out.append(hit)
        return out[:k]

    def dedup_check(
        self, text: str, *, threshold: float = 0.87, k: int = 3, category: str | None = None
    ) -> DedupResult:
        """Is `text` a semantic near-duplicate of something already stored?

        The threshold is on pure semantic similarity (see module docstring). 0.87 is a
        conservative near-duplicate bar: paraphrased retellings of the same item land above it,
        same-topic-new-angle pieces land below. Tune per call site, log skips loudly there."""
        hits = self.recall(text, k=k, category=category)
        best = max(hits, key=lambda h: h.score, default=None)
        if best is not None and best.score >= threshold:
            return DedupResult(True, best.score, best.content, best.metadata)
        return DedupResult(
            False, best.score if best else 0.0, best.content if best else "", best.metadata if best else {}
        )

    def prior_art_block(
        self,
        query: str,
        *,
        k: int = 3,
        min_score: float = 0.35,
        label: str = "PRIOR ART",
        category: str | None = None,
        instruction: str = "previously published, do NOT repeat these angles; reference or build on them instead:",
    ) -> str:
        """A ready-to-inject prompt block of the most similar stored artifacts, oldest context first.

        "" when memory is empty/unavailable or nothing clears min_score — callers can always
        concatenate it unconditionally. Each entry carries its metadata date when present so the
        writer can reference it ("as we wrote on <date>...")."""
        hits = [h for h in self.recall(query, k=k, category=category) if h.score >= min_score and h.content]
        if not hits:
            return ""
        lines = [f"{label} — {instruction}"]
        for i, h in enumerate(hits, 1):
            date = h.metadata.get("date") or h.metadata.get("day") or ""
            tag = f" ({date})" if date else ""
            body = h.content if len(h.content) <= 700 else h.content[:700] + " ..."
            lines.append(f"{i}.{tag} {body}")
        return "\n".join(lines)


_OPEN: dict[tuple, PipelineStore] = {}  # per-process cache: probe the embedder ONCE per store, not per task


def open_store(
    agent_name: str,
    *,
    scope: str = "agent",
    bias: str | None = None,
    analysis_llm: Any = None,
    required: bool = False,
) -> PipelineStore | None:
    """Open (or create) the pipeline memory store for an agent.

    scope "agent" (default) = ONE shared brain for the pipeline across runs — the right scope for
    a newspaper's voice, a section's history, or crew-forge's build experience. Returns None and
    LOGS the real cause when no embedder is reachable (required=False, the pipeline default) so a
    nightly run ships without memory rather than dying for an enhancement; required=True re-raises
    (a caller that cannot run without memory).

    Successful opens are CACHED per (agent, scope, bias) for the process — a daemon calling this per
    task probes the embedder once. An unavailable result is NOT cached, so the next call re-probes
    and memory self-heals when e.g. ollama comes back up."""
    key = (agent_name, scope, bias or "")
    if key in _OPEN:
        return _OPEN[key]
    try:
        embedder, tag = resolve_embedder(agent_name, bias=bias)
    except RuntimeError as exc:
        if required:
            raise
        print(f"[pipemem] {agent_name}: memory UNAVAILABLE this run -> {exc}", file=sys.stderr)
        return None
    store_dir = memory_store_path(agent_name, principal="owner", embedder_tag=tag, scope=scope)
    mem = _memory_cls()(
        llm=analysis_llm if analysis_llm is not None else _default_analysis_llm(agent_name, tag),
        embedder=embedder,
        storage=str(store_dir),
        root_scope=f"/pipeline/{agent_name}",
        # Semantic-only scoring: recall/dedup rank by MEANING alone, so MemoryMatch.score is a
        # comparable similarity and dedup thresholds hold across runs (a recency-boosted score
        # would let an old duplicate slip under the bar).
        semantic_weight=1.0,
        recency_weight=0.0,
        importance_weight=0.0,
    )
    print(f"[pipemem] {agent_name}: store open (embedder={tag}, scope={scope}) -> {store_dir}", file=sys.stderr)
    _OPEN[key] = PipelineStore(agent_name, mem)
    return _OPEN[key]

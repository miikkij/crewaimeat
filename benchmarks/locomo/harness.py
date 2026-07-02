"""LOCOMO harness — ingest -> QA -> judge -> report, one row per memory arm.

Memory arms (all offline-capable):
  - **keyword**  : a deterministic token-overlap (BM25-lite) retriever over the ingested turns. No LLM,
    no network — the honest FLOOR that shows what recall the embedder buys. (Stands in for the durable but
    non-semantic local_memory tier, which has no text search.)
  - **crewai**   : the opt-in crewaimeat CrewAI memory built in Deliverable 1 — the cascade-selected
    embedder (local ollama by default) + a LanceDB store, isolated per conversation. The headline arm.
  - **mem0**     : the reference, if `mem0ai` is installed + configured. Skipped-with-a-loud-note otherwise.

The answer model and judge model are INJECTED as callables `(system, user) -> str`, so this module has no
hard LLM dependency and is unit-testable with stubs; scripts/run_locomo.py wires them to local ollama.
Fairness: the SAME answer_fn + judge_fn + top-k are used for every arm (the comparability lever).

All arm storage is isolated (a temp dir per run) — the harness NEVER touches the fleet's crew_memory or
any live node state.
"""

from __future__ import annotations

import statistics
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from benchmarks.locomo import metrics
from benchmarks.locomo.dataset import Conversation

LLMCallable = Callable[[str, str], str]  # (system, user) -> completion text

ANSWER_SYSTEM = "You answer questions about a long conversation using ONLY the retrieved memories provided."
ANSWER_TEMPLATE = """\
Reference date (treat as 'today'): {reference_date}
Retrieved memories:
{memories}

Question: {question}
Answer as briefly as possible using only the memories above. If they do not contain the answer, reply "I don't know"."""


def _approx_tokens(*texts: str) -> int:
    """Approximate token count (chars/4). Labeled 'approx' everywhere — exact counts need provider usage,
    which crewai's LLM.call does not surface; latency + J are the precise headline metrics."""
    return sum(len(t or "") for t in texts) // 4


# --------------------------------------------------------------------------- #
# Arms
# --------------------------------------------------------------------------- #
class KeywordArm:
    """Deterministic token-overlap retriever (BM25-lite). No LLM, no network — the recall floor."""

    name = "keyword"

    def __init__(self) -> None:
        self._docs: list[str] = []
        self._toks: list[set[str]] = []

    def reset(self) -> None:
        self._docs, self._toks = [], []

    def ingest(self, turn_texts: list[str], reference_date: str = "") -> None:  # noqa: ARG002
        for t in turn_texts:
            self._docs.append(t)
            self._toks.append(set(metrics._tokens(t)))

    def recall(self, query: str, k: int = 10) -> list[str]:
        q = set(metrics._tokens(query))
        if not q or not self._docs:
            return []
        scored = sorted(
            range(len(self._docs)),
            key=lambda i: len(q & self._toks[i]) / (1 + len(self._toks[i])),
            reverse=True,
        )
        return [self._docs[i] for i in scored[:k] if q & self._toks[i]]


class CrewAIMemoryArm:
    """The opt-in crewaimeat CrewAI memory (Deliverable 1): cascade embedder + LanceDB, isolated per
    conversation under a temp storage root. Uses `depth='shallow'` recall (pure vector search, no extra
    LLM hop) for a fair, fast retrieval comparison."""

    name = "crewai"

    def __init__(self, storage_root, analysis_llm, embedder: dict) -> None:
        self._root = storage_root
        self._llm = analysis_llm
        self._embedder = embedder
        self._mem = None
        self._n = 0

    def reset(self) -> None:
        from pathlib import Path

        from crewai.memory.unified_memory import Memory

        self._n += 1
        store = Path(self._root) / f"conv-{self._n}"
        store.mkdir(parents=True, exist_ok=True)
        self._mem = Memory(llm=self._llm, embedder=self._embedder, storage=str(store), root_scope="/locomo")

    def ingest(self, turn_texts: list[str], reference_date: str = "") -> None:  # noqa: ARG002
        for t in turn_texts:
            try:
                self._mem.remember(t, source="locomo")
            except Exception as exc:  # noqa: BLE001 — one bad turn must not abort the ingest
                print(f"[locomo] crewai ingest error on a turn: {type(exc).__name__}: {exc}", file=sys.stderr)

    def recall(self, query: str, k: int = 10) -> list[str]:
        try:
            hits = self._mem.recall(query, depth="shallow", limit=k)
        except Exception as exc:  # noqa: BLE001
            print(f"[locomo] crewai recall error: {type(exc).__name__}: {exc}", file=sys.stderr)
            return []
        out = []
        for h in hits:
            rec = getattr(h, "record", h)
            out.append(str(getattr(rec, "content", h)))
        return out


class Mem0Arm:
    """Reference arm — mem0. Only usable if `mem0ai` is installed (and configured). Constructed lazily."""

    name = "mem0"

    def __init__(self, config: dict | None = None) -> None:
        from mem0 import Memory as Mem0Memory  # raises ImportError if not installed (caller handles)

        self._factory = lambda: Mem0Memory.from_config(config) if config else Mem0Memory()
        self._m = None
        self._uid = "locomo"
        self._n = 0

    def reset(self) -> None:
        self._n += 1
        self._uid = f"locomo-{self._n}"
        self._m = self._factory()

    def ingest(self, turn_texts: list[str], reference_date: str = "") -> None:  # noqa: ARG002
        for t in turn_texts:
            try:
                self._m.add(t, user_id=self._uid)
            except Exception as exc:  # noqa: BLE001
                print(f"[locomo] mem0 ingest error: {type(exc).__name__}: {exc}", file=sys.stderr)

    def recall(self, query: str, k: int = 10) -> list[str]:
        try:
            # mem0 2.x: search() rejects a top-level user_id — the entity filter moved into `filters`
            # (add() still takes user_id top-level). Verified against installed mem0ai 2.0.11.
            res = self._m.search(query, filters={"user_id": self._uid}, limit=k)
        except Exception as exc:  # noqa: BLE001
            print(f"[locomo] mem0 recall error: {type(exc).__name__}: {exc}", file=sys.stderr)
            return []
        items = res.get("results", res) if isinstance(res, dict) else res
        return [str(i.get("memory", i)) if isinstance(i, dict) else str(i) for i in (items or [])]


# --------------------------------------------------------------------------- #
# Answering + per-QA scoring
# --------------------------------------------------------------------------- #
def generate_answer(answer_fn: LLMCallable, question: str, contexts: list[str], reference_date: str) -> str:
    memories = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(contexts)) or "(no memories retrieved)"
    user = ANSWER_TEMPLATE.format(reference_date=reference_date or "unknown", memories=memories, question=question)
    return answer_fn(ANSWER_SYSTEM, user)


@dataclass
class QAResult:
    category: int
    correct: bool
    f1: float
    bleu1: float
    search_s: float
    answer_s: float
    approx_tokens: int  # approx query-time tokens (answer + judge), chars/4


def score_qa(arm, qa, answer_fn: LLMCallable, judge_fn: LLMCallable, reference_date: str, k: int) -> QAResult:
    t0 = time.perf_counter()
    contexts = arm.recall(qa.question, k)
    t1 = time.perf_counter()
    answer = generate_answer(answer_fn, qa.question, contexts, reference_date)
    t2 = time.perf_counter()
    judge_prompt = metrics.build_judge_prompt(qa.question, qa.answer, answer)
    verdict = judge_fn(metrics.JUDGE_SYSTEM, judge_prompt)
    correct = metrics.parse_judge_label(verdict)
    tokens = _approx_tokens(*contexts, qa.question, answer, judge_prompt, verdict)
    return QAResult(
        category=qa.category,
        correct=correct,
        f1=metrics.f1(qa.answer, answer),
        bleu1=metrics.bleu1(qa.answer, answer),
        search_s=t1 - t0,
        answer_s=t2 - t1,
        approx_tokens=tokens,
    )


@dataclass
class ArmReport:
    arm: str
    n_qa: int = 0
    ingest_seconds: float = 0.0
    n_turns: int = 0
    results: list[QAResult] = field(default_factory=list)

    def j_overall(self) -> float:
        return 100.0 * sum(r.correct for r in self.results) / len(self.results) if self.results else 0.0

    def j_by_category(self) -> dict[int, float]:
        by: dict[int, list[bool]] = {}
        for r in self.results:
            by.setdefault(r.category, []).append(r.correct)
        return {c: 100.0 * sum(v) / len(v) for c, v in sorted(by.items())}

    def _pct(self, attr: str, q: float) -> float:
        vals = sorted(getattr(r, attr) for r in self.results)
        if not vals:
            return 0.0
        idx = min(len(vals) - 1, int(q * (len(vals) - 1) + 0.5))
        return vals[idx]

    def summary(self) -> dict:
        return {
            "arm": self.arm,
            "n_qa": len(self.results),
            "n_turns": self.n_turns,
            "ingest_seconds": round(self.ingest_seconds, 2),
            "j_overall": round(self.j_overall(), 1),
            "j_by_category": {c: round(v, 1) for c, v in self.j_by_category().items()},
            "mean_f1": round(statistics.mean([r.f1 for r in self.results]), 3) if self.results else 0.0,
            "mean_bleu1": round(statistics.mean([r.bleu1 for r in self.results]), 3) if self.results else 0.0,
            "search_p50_s": round(self._pct("search_s", 0.50), 3),
            "search_p95_s": round(self._pct("search_s", 0.95), 3),
            "answer_p95_s": round(self._pct("answer_s", 0.95), 3),
            "approx_tokens_per_qa": int(statistics.mean([r.approx_tokens for r in self.results]))
            if self.results
            else 0,
        }


def run_arm(
    arm,
    conversations: list[Conversation],
    answer_fn: LLMCallable,
    judge_fn: LLMCallable,
    *,
    k: int = 10,
    max_qa_per_conv: int | None = None,
) -> ArmReport:
    """Ingest each conversation into `arm` (reset between conversations = per-conversation isolation), then
    answer + judge its scored (category 1-4) QA. Returns an ArmReport."""
    rep = ArmReport(arm=arm.name)
    for conv in conversations:
        arm.reset()
        turns = conv.turn_texts()
        t0 = time.perf_counter()
        arm.ingest(turns, conv.reference_date)
        rep.ingest_seconds += time.perf_counter() - t0
        rep.n_turns += len(turns)
        scored = [q for q in conv.qa if q.category in metrics.SCORED_CATEGORIES and q.question]
        if max_qa_per_conv is not None:
            dropped = len(scored) - max_qa_per_conv
            if dropped > 0:
                print(
                    f"[locomo] {arm.name}/{conv.sample_id}: capping to {max_qa_per_conv} of {len(scored)} "
                    f"scored QA (dropping {dropped}) — remove --max-qa to score all.",
                    file=sys.stderr,
                )
            scored = scored[:max_qa_per_conv]
        for qa in scored:
            rep.results.append(score_qa(arm, qa, answer_fn, judge_fn, conv.reference_date, k))
    return rep


def render_report(reports: list[ArmReport], *, note: str = "") -> str:
    lines = ["", "LOCOMO results — J score (LLM-judge, categories 1-4) + latency + approx tokens", "=" * 78]
    if note:
        lines.append(note)
    header = f"{'ARM':<10} {'J%':>6} {'F1':>6} {'BLEU1':>6} {'srch p50':>9} {'srch p95':>9} {'ans p95':>8} {'~tok/qa':>8} {'#QA':>5}"
    lines += [header, "-" * len(header)]
    for rep in reports:
        s = rep.summary()
        lines.append(
            f"{s['arm']:<10} {s['j_overall']:>6} {s['mean_f1']:>6} {s['mean_bleu1']:>6} "
            f"{s['search_p50_s']:>9} {s['search_p95_s']:>9} {s['answer_p95_s']:>8} "
            f"{s['approx_tokens_per_qa']:>8} {s['n_qa']:>5}"
        )
        bycat = "  ".join(f"{metrics_cat_name(c)}={v}" for c, v in s["j_by_category"].items())
        lines.append(f"           J by category: {bycat}")
    lines.append("=" * 78)
    lines.append("J = % judged CORRECT (mem0's binary rubric). F1/BLEU1 are secondary lexical proxies. ~tok/qa is")
    lines.append("approximate (chars/4) query-time tokens. Latencies are wall-clock seconds on this machine.")
    return "\n".join(lines)


def metrics_cat_name(c: int) -> str:
    from benchmarks.locomo.dataset import CATEGORY_NAMES

    return CATEGORY_NAMES.get(c, str(c))

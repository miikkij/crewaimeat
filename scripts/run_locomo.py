"""Run the LOCOMO proof harness: AIMEAT/crewaimeat memory vs mem0 (and a keyword floor).

OPT-IN and OFFLINE-BY-DEFAULT — the answer + judge models default to a LOCAL ollama model (zero OpenRouter
spend) and the crewai arm embeds via local ollama. It never touches the live fleet or a node.

Cost discipline (deliberate, per the owner): defaults to ONE conversation (`--sample 1`); pass `--full`
for all 10. `--estimate` meters ONE conversation, prints actual latency + approx tokens, and extrapolates
the full-run cost BEFORE you spend anything larger. Whatever is sampled or capped is logged loudly.

Examples:
  uv run python scripts/run_locomo.py --estimate                 # meter one conversation, extrapolate
  uv run python scripts/run_locomo.py                            # quick: 1 conversation, keyword+crewai
  uv run python scripts/run_locomo.py --full --arms keyword,crewai,mem0 --out results.json
  uv run python scripts/run_locomo.py --model openrouter/openai/gpt-4o-mini   # opt into a cloud model
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

# crewai in a script: silence its telemetry (matches the fleet_host convention). Same for mem0.
os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")
os.environ.setdefault("OTEL_SDK_DISABLED", "true")
os.environ.setdefault("MEM0_TELEMETRY", "false")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchmarks.locomo import dataset, harness  # noqa: E402


def make_llm_callable(model: str, base_url: str, temperature: float = 0.1):
    """Build an (system, user) -> text callable from a crewai LLM. Default target is local ollama."""
    from crewai import LLM

    kwargs = {"model": model, "temperature": temperature}
    if model.startswith("ollama/"):
        kwargs["base_url"] = base_url
    llm = LLM(**kwargs)

    def _call(system: str, user: str) -> str:
        try:
            return str(llm.call([{"role": "system", "content": system}, {"role": "user", "content": user}]))
        except Exception as exc:  # noqa: BLE001 — a failed answer/judge call becomes an empty answer (WRONG), not a crash
            print(f"[locomo] LLM call failed ({type(exc).__name__}: {exc})", file=sys.stderr)
            return ""

    return _call


def _mem0_local_config(model: str, ollama_base: str, storage_root: str) -> dict | None:
    """A fully-local mem0 config for an ollama/<model>: the SAME answer model + embed model as the other
    arms (the fairness lever), riding ollama's OpenAI-compatible /v1 (the embedder-cascade trick, so no
    extra `ollama` package), with an isolated on-disk qdrant + history db under the run's temp root.
    None for a non-ollama model — then mem0's own default config applies (needs OPENAI_API_KEY)."""
    if not model.startswith("ollama/"):
        return None
    base = ollama_base.rstrip("/") + "/v1"
    embed_model = os.getenv("AIMEAT_EMBED_OLLAMA_MODEL", "nomic-embed-text")  # mirrors the crewai cascade tier
    return {
        "llm": {
            "provider": "openai",
            "config": {
                "model": model.removeprefix("ollama/"),
                "api_key": "ollama",
                "openai_base_url": base,
                "temperature": 0.1,
            },
        },
        # embedding_dims deliberately NOT set on the embedder (ollama's /v1 may reject a `dimensions`
        # param); the qdrant collection is sized to nomic's 768 instead — a mismatch fails loud.
        "embedder": {
            "provider": "openai",
            "config": {"model": embed_model, "api_key": "ollama", "openai_base_url": base},
        },
        "vector_store": {
            "provider": "qdrant",
            "config": {
                "path": os.path.join(storage_root, "mem0_qdrant"),
                "collection_name": "locomo",
                "embedding_model_dims": 768,
                "on_disk": True,
            },
        },
        "history_db_path": os.path.join(storage_root, "mem0_history.db"),
    }


def build_arm(name: str, *, analysis_llm, embed_bias: str, storage_root: str, model: str, ollama_base: str):
    if name == "keyword":
        return harness.KeywordArm()
    if name == "crewai":
        from crewaimeat.embedder_cascade import resolve_embedder

        embedder, tag = resolve_embedder("locomo-bench", bias=embed_bias)
        print(f"[locomo] crewai arm embedder: {tag}", file=sys.stderr)
        return harness.CrewAIMemoryArm(storage_root, analysis_llm, embedder)
    if name == "mem0":
        try:
            cfg = _mem0_local_config(model, ollama_base, storage_root)
            # mem0 pitfall (mem0/llms/openai.py): if OPENROUTER_API_KEY is set it SILENTLY routes all LLM
            # calls to OpenRouter, ignoring the configured openai_base_url — which sends a local ollama
            # model id to OpenRouter and 400s every add(). Neutralize it for THIS process on a local run.
            if cfg and os.environ.pop("OPENROUTER_API_KEY", None):
                print(
                    "[locomo] NOTE: unset OPENROUTER_API_KEY for this benchmark process — mem0 would "
                    "silently prefer OpenRouter over the configured local ollama endpoint.",
                    file=sys.stderr,
                )
            print(
                f"[locomo] mem0 arm config: {'local ollama via /v1 (same models as other arms)' if cfg else 'mem0 defaults (non-ollama --model; needs OPENAI_API_KEY)'}",
                file=sys.stderr,
            )
            return harness.Mem0Arm(cfg)
        except Exception as exc:  # noqa: BLE001 — mem0 optional; skip loudly rather than crash the whole run
            print(
                f"[locomo] SKIPPING mem0 arm: {type(exc).__name__}: {exc} (pip install mem0ai to enable)",
                file=sys.stderr,
            )
            return None
    print(f"[locomo] unknown arm '{name}' — skipping", file=sys.stderr)
    return None


def main() -> None:
    ap = argparse.ArgumentParser(description="LOCOMO proof harness (crewaimeat memory vs mem0).")
    ap.add_argument("--arms", default="keyword,crewai", help="comma list: keyword, crewai, mem0")
    ap.add_argument("--sample", type=int, default=1, help="number of conversations (default 1; deterministic first-N)")
    ap.add_argument("--full", action="store_true", help="run all 10 conversations (overrides --sample)")
    ap.add_argument("--max-qa", type=int, default=None, help="cap scored QA per conversation (logged); default all")
    ap.add_argument("--k", type=int, default=10, help="top-k memories retrieved per question")
    ap.add_argument("--model", default="ollama/gemma4:latest", help="answer+judge model (default local ollama gemma4)")
    ap.add_argument("--ollama-base", default=os.getenv("OLLAMA_HOST", "http://localhost:11434"))
    ap.add_argument("--embed-bias", default="privacy", choices=["privacy", "cost"], help="embedder cascade bias")
    ap.add_argument("--estimate", action="store_true", help="meter ONE conversation + extrapolate; do not run full")
    ap.add_argument("--out", default="", help="write the JSON report to this path")
    args = ap.parse_args()

    convs_all = dataset.load()
    total_scored = sum(
        1 for c in convs_all for q in c.qa if q.category in harness.metrics.SCORED_CATEGORIES and q.question
    )

    answer_fn = make_llm_callable(args.model, args.ollama_base)
    judge_fn = answer_fn  # same model judges (held constant across arms — the fairness lever)
    storage_root = tempfile.mkdtemp(prefix="locomo_crewai_")
    print(
        f"[locomo] answer+judge model: {args.model} | crewai storage (temp, isolated): {storage_root}", file=sys.stderr
    )

    if args.estimate:
        conv = dataset.sample_conversations(convs_all, 1)
        estimate_arm = "crewai" if "crewai" in args.arms else args.arms.split(",")[0].strip()
        arm = build_arm(
            estimate_arm,
            analysis_llm=_analysis_llm(args),
            embed_bias=args.embed_bias,
            storage_root=storage_root,
            model=args.model,
            ollama_base=args.ollama_base,
        )
        if arm is None:
            sys.exit("[locomo] estimate arm unavailable")
        rep = harness.run_arm(arm, conv, answer_fn, judge_fn, k=args.k, max_qa_per_conv=args.max_qa)
        s = rep.summary()
        per_qa_answer = s["answer_p95_s"]
        scored_in_sample = s["n_qa"]
        print(harness.render_report([rep], note=f"ESTIMATE on {scored_in_sample} QA (1 conversation)"))
        print("\n[locomo] EXTRAPOLATION to the full benchmark (all 10 conversations):", file=sys.stderr)
        print(f"  full scored QA ~= {total_scored}  (this sample: {scored_in_sample})", file=sys.stderr)
        print(
            f"  approx tokens/QA (chars/4): {s['approx_tokens_per_qa']}  ->  full ~= {s['approx_tokens_per_qa'] * total_scored:,} tokens",
            file=sys.stderr,
        )
        print(
            f"  ingest wall-time this conversation: {s['ingest_seconds']}s x10 convs ~= {round(s['ingest_seconds'] * 10)}s",
            file=sys.stderr,
        )
        print(
            f"  QA wall-time ~= answer_p95 {per_qa_answer}s x {total_scored} QA ~= {round(per_qa_answer * total_scored / 60, 1)} min (per arm, rough)",
            file=sys.stderr,
        )
        print(
            "  On the default LOCAL ollama model, OpenRouter cost = $0. A cloud --model at full scale is typically ~$1-3/arm.",
            file=sys.stderr,
        )
        return

    convs = convs_all if args.full else dataset.sample_conversations(convs_all, args.sample)
    reports = []
    for name in [a.strip() for a in args.arms.split(",") if a.strip()]:
        arm = build_arm(
            name,
            analysis_llm=_analysis_llm(args),
            embed_bias=args.embed_bias,
            storage_root=storage_root,
            model=args.model,
            ollama_base=args.ollama_base,
        )
        if arm is None:
            continue
        print(f"[locomo] running arm '{name}' over {len(convs)} conversation(s) ...", file=sys.stderr)
        reports.append(harness.run_arm(arm, convs, answer_fn, judge_fn, k=args.k, max_qa_per_conv=args.max_qa))

    note = f"{len(convs)} conversation(s), model={args.model}, k={args.k}" + (
        "" if args.full else "  [SAMPLE — pass --full for all 10]"
    )
    print(harness.render_report(reports, note=note))
    if args.out:
        payload = {"note": note, "version": dataset.LOCOMO_VERSION, "arms": [r.summary() for r in reports]}
        from pathlib import Path

        Path(args.out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"[locomo] wrote {args.out}", file=sys.stderr)


def _analysis_llm(args):
    """A crewai LLM instance for the crewai arm's memory analysis (its own object, local ollama by default)."""
    from crewai import LLM

    kwargs = {"model": args.model, "temperature": 0.1}
    if args.model.startswith("ollama/"):
        kwargs["base_url"] = args.ollama_base
    return LLM(**kwargs)


if __name__ == "__main__":
    main()

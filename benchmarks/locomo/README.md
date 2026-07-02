# LOCOMO proof harness — crewaimeat memory vs mem0

An **opt-in, offline-by-default** benchmark that puts the crewaimeat opt-in CrewAI memory (Deliverable 1)
head-to-head with **mem0** on **LOCOMO**, the long-term-conversational-memory benchmark mem0 markets itself
against. It reports **recall/accuracy (J-score) + latency + (approx) token cost** so we can show our memory
approach is competitive.

It never touches the live fleet or a node: all arm storage is an isolated temp dir, and the default answer/
judge/embedder models are **local ollama** (zero OpenRouter spend).

## Dataset (stated explicitly)

- **Benchmark:** LOCOMO — Maharana et al., *"Evaluating Very Long-Term Conversational Memory of LLM
  Agents"*, ACL 2024, [arXiv:2402.17753](https://arxiv.org/abs/2402.17753).
- **Split/version:** `data/locomo10.json` from [`snap-research/locomo`](https://github.com/snap-research/locomo)
  — the released **10-conversation** subset (the canonical split every mem0-style comparison uses),
  **1,986 QA pairs**. Pinned as `snap-research/locomo@main:data/locomo10.json`.
- It is **downloaded on first run** into `benchmarks/locomo/.data/` (gitignored) and its **sha256 + size**
  are printed — a run is auditable. It is **not** vendored into the repo. If the download fails the harness
  raises loud with the manual step (never a silent empty dataset).

## Metrics (replicating mem0 for comparability)

- **J-score (headline):** a binary LLM-as-judge verdict (CORRECT/WRONG) per QA; **J = % CORRECT**. The judge
  prompt mirrors mem0's deliberately-generous rubric — partial credit on list answers, paraphrases count,
  **±14-day** date tolerance, **±50%** duration tolerance, WRONG only if *zero* gold items appear.
- **Scored categories = 1–4** (multi-hop, temporal, open-domain, single-hop). **Category 5 (adversarial) is
  excluded** — those rows carry no ground-truth answer (same exclusion mem0 uses).
- **Secondary:** token-level **F1** and **BLEU-1** (weak lexical proxies, per mem0's own caveat).
- **Cost/latency:** search **p50/p95**, answer **p95** (wall-clock), and **approx tokens/QA** (chars/4 —
  labeled approximate because crewai's `LLM.call` does not surface provider usage; latency + J are exact).

**Fairness lever:** the same answer model, judge model + prompt, and top-k are held constant across all arms.

## Arms

| arm | what it is | offline? |
|-----|-----------|----------|
| `keyword` | deterministic token-overlap (BM25-lite) retriever — the honest recall **floor** | yes |
| `crewai` | the opt-in crewaimeat CrewAI memory (cascade embedder + LanceDB), isolated per conversation | yes (local ollama) |
| `mem0` | the reference — used only if `mem0ai` is installed + configured; skipped-with-a-loud-note otherwise | depends on mem0 config |

> A live-node arm over `aimeat_memory_search` is intentionally **not** on the offline path (it needs the live
> node); it can be added later behind an explicit `--live` flag.

## Running it

```bash
# meter ONE conversation, print latency + approx tokens, extrapolate the full-run cost BEFORE spending
uv run python scripts/run_locomo.py --estimate

# quick: 1 conversation (default), keyword + crewai arms, local gemma — $0 OpenRouter
uv run python scripts/run_locomo.py

# full 10-conversation run, add mem0 as the reference, write a JSON report
uv run python scripts/run_locomo.py --full --arms keyword,crewai,mem0 --out locomo_results.json

# opt into a cloud model (spends money — see below)
uv run python scripts/run_locomo.py --model openrouter/openai/gpt-4o-mini --full
```

Key flags: `--sample N` (default 1, deterministic first-N; whatever is sampled/capped is logged loudly),
`--full` (all 10), `--max-qa N` (cap scored QA per conversation), `--k` (top-k retrieved), `--model`
(answer+judge model, default `ollama/gemma4:latest`), `--embed-bias privacy|cost`, `--estimate`, `--out`.

## Prerequisites

- **ollama** running with the answer model (`ollama pull gemma4`) and an embedding model
  (`ollama pull nomic-embed-text`) for the `crewai` arm. If no embedder is reachable, the crewai arm fails
  loud (the embedder cascade names the fix).
- **mem0** arm (optional): `uv pip install mem0ai` and configure it for a local backend.

## Cost

On the default **local** models, OpenRouter spend is **$0**. A cloud `--model` at **full** scale is
typically **~$1–3 per arm** (mem0's own numbers used gpt-4o-mini). Use `--estimate` first — it meters one
conversation and extrapolates before you commit to a big run. Reproducing mem0's *absolute* published J is
finicky (their own repro is); hold the harness constant across arms and read the **relative** comparison.

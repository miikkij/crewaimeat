"""Model-capability check — can a model actually drive crewaimeat?

Runs a small battery against each model in your `llm_providers.json` (or `--models a,b,c`) and prints a
scorecard. The battery mirrors what crewaimeat crews really need:

  - completion : returns non-empty text
  - json       : returns parseable JSON (structured-output tasks, e.g. the quiz)
  - search     : a real researcher Agent + the SearXNG tool returns ACTUAL article titles
                 (this is the fetcher's core behaviour and the usual failure point —
                 weak models build garbage queries or never call the tool)

A model that passes `search` is considered crewaimeat-capable. Local Ollama models are included if the
config lists them. Usage:

    uv run python scripts/check_models.py                 # test the models in llm_providers.json
    uv run python scripts/check_models.py --quick         # skip the (slow) search-crew test
    uv run python scripts/check_models.py --models openrouter:openai/gpt-oss-120b:free,ollama:qwen2.5:7b
"""

from __future__ import annotations

import argparse
import json
import re
import sys

from dotenv import load_dotenv

load_dotenv()

from crewai import Agent, Crew, LLM, Task  # noqa: E402
from crewaimeat.llm import _flatten_endpoints, _providers_file  # noqa: E402
from crewaimeat.searxng_search import SearxngSearchTool  # noqa: E402

_PREFIX = {"openrouter": "openrouter/", "ollama": "ollama/", "xai": "xai/", "openai": "", "generic": ""}
_BASE = {"openrouter": "https://openrouter.ai/api/v1", "ollama": "http://localhost:11434"}


def _build_llm(ep: dict) -> LLM:
    kw: dict = dict(model=ep["model"], temperature=0.2)
    if ep.get("base_url"):
        kw["base_url"] = ep["base_url"]
    if ep.get("api_key"):
        kw["api_key"] = ep["api_key"]
    return LLM(**kw)


def _endpoints_from_models(spec: str) -> list[dict]:
    """`--models` form: comma-separated 'provider:model_id' (provider defaults to openrouter)."""
    import os
    eps = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        ptype, _, mid = item.partition(":") if ":" in item and item.split(":", 1)[0] in _PREFIX else ("openrouter", "", item)
        if not mid:
            ptype, mid = "openrouter", item
        prefix = _PREFIX.get(ptype, "")
        lm = mid if (not prefix or mid.startswith(prefix)) else prefix + mid
        key = None
        if ptype in ("openrouter", "xai"):
            key = os.getenv("OPENROUTER_API_KEY" if ptype == "openrouter" else "XAI_API_KEY")
        eps.append({"label": f"{ptype}:{mid}", "model": lm, "base_url": _BASE.get(ptype), "api_key": key})
    return eps


def t_completion(ep) -> str:
    try:
        out = _build_llm(ep).call([{"role": "user", "content": "Reply with exactly: OK"}])
        return "PASS" if (out and out.strip()) else "empty"
    except Exception as e:
        return f"ERR:{type(e).__name__}"


def t_json(ep) -> str:
    try:
        out = _build_llm(ep).call([{"role": "user", "content": 'Return ONLY valid JSON, no prose: {"ok": true, "n": 3}'}])
        m = re.search(r"\{.*\}", out.strip().strip("`"), re.S)
        json.loads(m.group(0))
        return "PASS"
    except Exception as e:
        return f"ERR:{type(e).__name__}"


def t_search(ep) -> str:
    try:
        agent = Agent(role="Finnish news researcher",
                      goal="Search the web and report real article titles.",
                      backstory="You call the Web Search tool with PLAIN keyword queries (no dates) and report what you find.",
                      llm=_build_llm(ep), tools=[SearxngSearchTool()], max_iter=6, verbose=False, allow_delegation=False)
        task = Task(description="Call the Web Search tool with the query 'talous uutiset Suomi' and return TWO actual "
                                "article titles (verbatim from the results). If nothing is found, reply NONE.",
                    agent=agent, expected_output="Two real article titles, or NONE.")
        out = str(Crew(agents=[agent], tasks=[task], verbose=False).kickoff()).strip()
        return "PASS" if (len(out) > 30 and "NONE" not in out.upper()[:12]) else "no-results"
    except Exception as e:
        return f"ERR:{type(e).__name__}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", help="comma-separated provider:model_id (else read llm_providers.json)")
    ap.add_argument("--quick", action="store_true", help="skip the slow search-crew test")
    args = ap.parse_args()

    if args.models:
        eps = _endpoints_from_models(args.models)
    else:
        pf = _providers_file()
        if not pf:
            print("No llm_providers.json found and no --models given.", file=sys.stderr)
            sys.exit(2)
        cfg = json.loads(open(pf, encoding="utf-8").read())
        eps = _flatten_endpoints(cfg, for_tool_use=True)
    if not eps:
        print("No endpoints to test.", file=sys.stderr)
        sys.exit(2)

    print(f"{'MODEL':40s} {'completion':12s} {'json':10s} " + ("" if args.quick else f"{'search':12s} ") + "VERDICT")
    print("-" * (64 if args.quick else 78))
    for ep in eps:
        comp = t_completion(ep)
        js = t_json(ep)
        if args.quick:
            verdict = "ok" if comp == "PASS" and js == "PASS" else "weak"
            print(f"{ep['label']:40s} {comp:12s} {js:10s} {verdict}")
        else:
            srch = t_search(ep)
            verdict = "CAPABLE" if srch == "PASS" else ("partial" if comp == "PASS" else "FAIL")
            print(f"{ep['label']:40s} {comp:12s} {js:10s} {srch:12s} {verdict}")


if __name__ == "__main__":
    main()

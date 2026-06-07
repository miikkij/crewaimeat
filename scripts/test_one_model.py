"""Test ONE model: does it respond? does it support tools (function-calling)? — live, via the provider API.

Give it a model id (OpenRouter by default) and it runs two live probes and prints a verdict:
  - respond : a plain completion returns non-empty text
  - tools   : given a `multiply` tool + a prompt that needs it, the model emits a tool_call

Usage:
    uv run python scripts/test_one_model.py owl-alpha
    uv run python scripts/test_one_model.py openai/gpt-oss-120b
    uv run python scripts/test_one_model.py minimax/minimax-m2.7 --prompt "Say hi in Finnish"
    uv run python scripts/test_one_model.py --list owl        # just list OpenRouter ids matching a name

Defaults to OpenRouter (OPENROUTER_API_KEY). Cloaked models (owl-alpha etc.) live there.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv()

BASE = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
KEY = os.environ.get("OPENROUTER_API_KEY", "")
HEAD = {"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
_MUL_TOOL = [{
    "type": "function",
    "function": {
        "name": "multiply",
        "description": "Multiply two integers and return the product.",
        "parameters": {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                       "required": ["a", "b"]},
    },
}]


def list_models(needle: str) -> None:
    r = requests.get(f"{BASE}/models", headers=HEAD, timeout=20)
    for m in r.json().get("data", []):
        mid = m.get("id", "")
        if needle.lower() in mid.lower():
            p = m.get("pricing", {})
            st = m.get("supported_parameters", []) or []
            print(f"  {mid:40s} ctx={m.get('context_length')} tools={'tools' in st or 'tool_choice' in st} "
                  f"in={p.get('prompt')} out={p.get('completion')}")


def _chat(model: str, messages: list, tools=None, timeout=60) -> tuple[int, dict, float]:
    body = {"model": model, "messages": messages, "max_tokens": 200}
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    t0 = time.time()
    r = requests.post(f"{BASE}/chat/completions", headers=HEAD, json=body, timeout=timeout)
    dt = time.time() - t0
    try:
        return r.status_code, r.json(), dt
    except Exception:
        return r.status_code, {"_raw": r.text[:300]}, dt


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("model", nargs="?", help="model id (OpenRouter), e.g. owl-alpha or openai/gpt-oss-120b")
    ap.add_argument("--list", metavar="NEEDLE", help="list OpenRouter model ids matching NEEDLE and exit")
    ap.add_argument("--prompt", default="Reply with exactly: OK", help="completion probe prompt")
    args = ap.parse_args()

    if not KEY:
        print("OPENROUTER_API_KEY missing in env/.env", file=sys.stderr)
        sys.exit(2)
    if args.list:
        print(f"OpenRouter models matching '{args.list}':")
        list_models(args.list)
        return
    if not args.model:
        ap.error("give a model id (or --list NEEDLE)")

    model = args.model
    # cloaked ids usually need the openrouter/ vendor prefix for the bare codenames
    if "/" not in model:
        model = f"openrouter/{model}"
    print(f"== testing model: {model} ==")

    # probe 1: respond
    sc, body, dt = _chat(model, [{"role": "user", "content": args.prompt}])
    if sc != 200:
        err = (body.get("error") or {}).get("message") or body.get("_raw") or body
        print(f"  respond : FAIL (HTTP {sc}) {str(err)[:160]}")
        print(f"\nVERDICT: {model} is NOT responding (HTTP {sc}).")
        return
    choice = (body.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content") or ""
    print(f"  respond : {'PASS' if content.strip() else 'EMPTY'} ({dt:.1f}s)  sample: {content.strip()[:80]!r}")

    # probe 2: tools (function-calling)
    sc2, body2, dt2 = _chat(model, [{"role": "user", "content": "Use the multiply tool to compute 12 * 7."}], tools=_MUL_TOOL)
    if sc2 != 200:
        err = (body2.get("error") or {}).get("message") or body2.get("_raw") or body2
        print(f"  tools   : FAIL (HTTP {sc2}) {str(err)[:160]}")
    else:
        msg = ((body2.get("choices") or [{}])[0].get("message")) or {}
        tcs = msg.get("tool_calls") or []
        if tcs:
            fn = (tcs[0].get("function") or {})
            print(f"  tools   : PASS ({dt2:.1f}s)  called {fn.get('name')}({fn.get('arguments')})")
        else:
            print(f"  tools   : NO TOOL CALL ({dt2:.1f}s)  model answered in text instead: {str(msg.get('content'))[:60]!r}")

    responds = bool(content.strip())
    has_tools = sc2 == 200 and bool((((body2.get("choices") or [{}])[0].get("message")) or {}).get("tool_calls"))
    print(f"\nVERDICT: {model} — responds={responds}, supports_tools={has_tools}")


if __name__ == "__main__":
    main()

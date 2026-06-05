"""Pytest bootstrap for the AIMEAT crew test floor (L1/L2, deterministic, no LLM, no network).

Puts the repo root and tests/ on sys.path (so ``import crews.<x>`` and ``import crew_fixtures``
work as namespace packages) and provides dummy env so any incidental ``get_llm()`` constructs an
LLM object without a real key. These tests never hit the network: they exercise pure scaffold
functions, the guardrails, and each crew's ``build_domain`` wiring with a stub context.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for _p in (str(ROOT), str(ROOT / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Construction-only: a dummy key lets get_llm()/LLM(...) build without raising; nothing is called.
os.environ.setdefault("OPENROUTER_API_KEY", "test-not-used")
# Keep the default offline web-search path (SearXNG) and the OpenRouter LLM path.
os.environ.pop("USE_TAVILY", None)
os.environ.pop("USE_XAI", None)

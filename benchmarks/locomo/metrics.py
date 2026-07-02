"""Scoring for the LOCOMO harness — replicates mem0's methodology so numbers are comparable.

Headline metric = the **J score** (LLM-as-judge): a binary CORRECT/WRONG verdict per QA, J = % CORRECT.
The judge prompt mirrors mem0's deliberately-generous rules (partial credit on list answers, paraphrases
count, ±14-day date tolerance, ±50% duration tolerance) so a fair, comparable J is produced regardless of
which memory arm generated the answer. Secondary lexical metrics (token F1, BLEU-1) are computed too but,
as mem0 notes, are weak proxies for factual correctness — J is the headline.

Only categories 1-4 are scored; category 5 (adversarial) is excluded because those rows carry no
ground-truth `answer` (same exclusion mem0 uses).

Sources: mem0ai/memory-benchmarks (benchmarks/locomo/prompts.py), Mem0 paper arXiv:2504.19413.
"""

from __future__ import annotations

import json
import math
import re

SCORED_CATEGORIES: tuple[int, ...] = (1, 2, 3, 4)

# The binary judge prompt — faithful to mem0's generous J-score rubric.
JUDGE_SYSTEM = "You are evaluating conversational AI memory recall. Return JSON only with the format requested."

JUDGE_TEMPLATE = """\
You are grading whether a GENERATED answer is correct against a GOLD answer for a question about a long \
conversation. Apply these rules exactly:
- PARTIAL CREDIT: if the generated answer includes AT LEAST ONE correct item from the gold answer's list, \
mark CORRECT. Only mark WRONG if NONE of the gold items appear.
- PARAPHRASES COUNT: a correct fact said in different words is CORRECT.
- EXTRA DETAIL IS FINE: extra correct or harmless detail does not make it WRONG.
- SAME REFERENT: the same entity named differently is CORRECT.
- DATE TOLERANCE: dates within 14 days of each other are CORRECT. Durations within 50% are CORRECT.
- ONLY mark WRONG if the generated answer contains ZERO correct items from the gold answer.

QUESTION: {question}
GOLD ANSWER: {gold}
GENERATED ANSWER: {generated}

Return JSON only: {{"reasoning": "<one sentence>", "label": "CORRECT" or "WRONG"}}"""


def build_judge_prompt(question: str, gold: str, generated: str) -> str:
    return JUDGE_TEMPLATE.format(question=question, gold=gold, generated=generated)


def parse_judge_label(text: str) -> bool:
    """True iff the judge said CORRECT. Robust to code fences / extra prose: try JSON, then fall back to a
    word search. Defaults to WRONG (False) when genuinely ambiguous — never silently mark a miss correct."""
    if not text:
        return False
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            obj = json.loads(m.group(0))
            label = str(obj.get("label", "")).strip().upper()
            if label in ("CORRECT", "WRONG"):
                return label == "CORRECT"
        except (ValueError, TypeError):
            pass
    up = text.upper()
    if "CORRECT" in up and "WRONG" not in up:
        return True
    if "WRONG" in up and "CORRECT" not in up:
        return False
    # both/neither present -> take the LAST explicit verdict word if any, else WRONG
    last_correct = up.rfind("CORRECT")
    last_wrong = up.rfind("WRONG")
    return last_correct > last_wrong


_WORD = re.compile(r"[a-z0-9]+")


def _tokens(s: str) -> list[str]:
    return _WORD.findall((s or "").lower())


def f1(gold: str, pred: str) -> float:
    """Token-level F1 (multiset overlap). Secondary metric — mem0 keeps it as a weak lexical proxy."""
    g, p = _tokens(gold), _tokens(pred)
    if not g and not p:
        return 1.0
    if not g or not p:
        return 0.0
    from collections import Counter

    common = Counter(g) & Counter(p)
    overlap = sum(common.values())
    if overlap == 0:
        return 0.0
    precision = overlap / len(p)
    recall = overlap / len(g)
    return 2 * precision * recall / (precision + recall)


def bleu1(gold: str, pred: str) -> float:
    """BLEU-1: unigram precision with a brevity penalty (secondary lexical metric)."""
    g, p = _tokens(gold), _tokens(pred)
    if not p or not g:
        return 0.0
    from collections import Counter

    gcount = Counter(g)
    clipped = 0
    pcount = Counter(p)
    for tok, c in pcount.items():
        clipped += min(c, gcount.get(tok, 0))
    precision = clipped / len(p)
    bp = 1.0 if len(p) >= len(g) else math.exp(1 - len(g) / len(p))
    return bp * precision

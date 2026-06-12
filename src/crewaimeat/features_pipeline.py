"""DETERMINISTIC evening features — koodaus + prompt-niksi + matikka + news quiz, all via direct grok.

The daily-features crew skipped tasks (koodaus/matikka came up empty). Here each piece is a direct grok call
in a code loop, and the quiz JSON is parsed + validated before it is stored — so nothing is silently dropped.
"""

from __future__ import annotations

import json
import re

from crewaimeat.aimeat_crew import _aimeat_call
from crewaimeat.llm import get_llm

_TIDBITS = {
    "koodaus": ("PÄIVÄN KOODAUSOSIO — yksi näppärä, itsenäinen ohjelmointivinkki (kikka, kuvio tai sudenkuoppa) "
                "lyhyellä oikealla ```koodi```-lohkolla ja 2-4 lauseen suomenkielisellä selityksellä. "
                "Allekirjoita '— Koodi-Kalle'."),
    "prompt-niksi": ("PROMPT-NIKSINURKKA — yksi käytännön prompt-engineering-vinkki jonka lukija voi käyttää "
                     "tänään, 3-5 lausetta suomeksi + lyhyt esimerkkikehote. Allekirjoita '— Prompt-Pia'."),
    "matikka": ("MATEMATIIKKAHETKI — yksi ihastuttava matemaattinen uteliaisuus, pulma tai elegantti fakta, "
                "selkeä suomenkielinen selitys JA vastaus (piilota vastaus loppuun riville 'Vastaus:'). "
                "Allekirjoita '— Matikka-Make'."),
}
_QUIZ_EXCL = {"koodaus", "prompt-niksi", "matikka"}


def build_features(agent_name: str, date: str, edition: str) -> str:
    llm = get_llm(for_tool_use=False, temperature=0.7)
    lines = []
    for cat, brief in _TIDBITS.items():
        out = llm.call([{"role": "user", "content": "Kirjoita markdownina: " + brief}])
        out = out if isinstance(out, str) else str(out)
        if len(out.strip()) < 80:  # hiccup → retry once
            out = llm.call([{"role": "user", "content": "Kirjoita markdownina: " + brief}])
            out = out if isinstance(out, str) else str(out)
        _aimeat_call(agent_name, "aimeat_memory_write",
                     {"key": f"news.{date}.{edition}.article.{cat}", "value": out, "visibility": "public"})
        lines.append(f"{cat}={len(out)}c")

    lines.append(build_quiz(agent_name, date, edition))
    return "features: " + ", ".join(lines)


_MIN_QUIZ_ARTICLES = 3  # below this the quiz is SKIPPED, never fabricated
_TEMPLATE_OPTIONS = ["a", "b", "c", "d", "e"]  # the prompt's example echoed back = garbage


def _valid_quiz(quiz: dict) -> str | None:
    """Structural validation. Returns an error string, or None when the quiz is sound."""
    qs = quiz.get("questions")
    if not isinstance(qs, list) or len(qs) < 3:
        return f"only {len(qs) if isinstance(qs, list) else 0} questions"
    for i, q in enumerate(qs):
        text = (q.get("q") or "").strip()
        opts = q.get("options")
        correct = q.get("correct")
        if len(text) < 10 or "ei artikkele" in text.lower():
            return f"question {i + 1} is placeholder/too short: {text[:40]!r}"
        if not isinstance(opts, list) or len(opts) != 5 or [str(o).strip().lower() for o in opts] == _TEMPLATE_OPTIONS:
            return f"question {i + 1} has invalid/template options"
        if not isinstance(correct, list) or not correct or not all(
                isinstance(c, int) and 0 <= c < 5 for c in correct):
            return f"question {i + 1} has invalid correct indices"
    return None


def build_quiz(agent_name: str, date: str, edition: str) -> str:
    """The news quiz from the day's articles — SKIPPED loudly when articles aren't readable yet
    (the 17:45 schedule can race the 17:25 writers); a retry guard re-calls this until they are.
    A failed/skipped build never writes, so an existing good quiz is never clobbered."""
    arts = []
    r = _aimeat_call(agent_name, "aimeat_memory_list",
                     {"owner_scope": True, "prefix": f"news.{date}.{edition}.article."})
    for it in (r or {}).get("items") or []:
        cat = it.get("key", "").rsplit(".", 1)[-1]
        if cat in _QUIZ_EXCL:
            continue
        v = it.get("value") or (_aimeat_call(agent_name, "aimeat_memory_read", {"key": it.get("key")}) or {}).get("value")
        if isinstance(v, str) and v.strip():
            arts.append(f"[{cat}] {v[:500]}")
    if len(arts) < _MIN_QUIZ_ARTICLES:
        import sys
        print(f"[{agent_name}] quiz SKIPPED: only {len(arts)} readable articles for {date} {edition} "
              f"(writers still running or read lag) — will not fabricate", file=sys.stderr)
        return f"quiz=SKIPPED({len(arts)} articles)"
    llm = get_llm(for_tool_use=False, temperature=0.7)
    quiz_prompt = (
        "Rakenna PÄIVÄN UUTISVISA näistä uutisartikkeleista. TASAN 5 kysymystä, kukin 5 vaihtoehtoa, yksi TAI "
        "useampi oikein. Perusta kysymykset VAIN siihen mitä artikkelit sanovat. Palauta VAIN JSON (ei muuta):\n"
        f'{{"title":"Päivän uutisvisa","date":"{date}","edition":"{edition}","questions":'
        '[{"q":"...","options":["a","b","c","d","e"],"correct":[0,2],"explain":"..."}]}\n'
        "questions-listassa TASAN 5 alkiota. correct = 0-pohjaiset numeeriset indeksit. options = OIKEAT "
        "vastausvaihtoehdot tekstinä (esimerkin a-e ovat vain paikanvaraajia).\n\nUUTISET:\n" + "\n\n".join(arts))
    for attempt in (1, 2):
        out = llm.call([{"role": "user", "content": quiz_prompt}])
        out = out if isinstance(out, str) else str(out)
        try:
            m = re.search(r"\{.*\}", out.strip().strip("`"), re.S)
            quiz = json.loads(m.group(0))
        except Exception as e:  # noqa: BLE001
            if attempt == 2:
                return f"quiz=FAILED({type(e).__name__})"
            continue
        err = _valid_quiz(quiz)
        if err is None:
            _aimeat_call(agent_name, "aimeat_memory_write",
                         {"key": f"news.{date}.{edition}.quiz", "value": quiz, "visibility": "public"})
            return f"quiz={len(quiz['questions'])}Q"
        if attempt == 2:
            return f"quiz=FAILED({err})"
    return "quiz=FAILED(unreachable)"


def make_features_tools(agent_name: str) -> list:
    from crewai.tools import tool

    @tool("write_features")
    def write_features(date: str, edition: str) -> str:
        """Deterministically write the evening features — koodaus, prompt-niksi, matikka (each a direct grok
        call) AND the news quiz (validated JSON built from the day's news articles). Call ONCE with the
        resolved date+edition. The loop runs in code, so no feature is skipped. Returns a report."""
        return build_features(agent_name, (date or "").strip(), (edition or "").strip())

    write_features.cache_function = lambda *_a, **_k: False
    return [write_features]

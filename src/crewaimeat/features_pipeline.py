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

    # quiz from the day's NEWS articles
    arts = []
    r = _aimeat_call(agent_name, "aimeat_memory_list", {"owner_scope": True, "prefix": f"news.{date}.{edition}.article."})
    for it in (r or {}).get("items") or []:
        cat = it.get("key", "").rsplit(".", 1)[-1]
        if cat in _QUIZ_EXCL:
            continue
        v = it.get("value") or (_aimeat_call(agent_name, "aimeat_memory_read", {"key": it.get("key")}) or {}).get("value")
        if isinstance(v, str) and v.strip():
            arts.append(f"[{cat}] {v[:500]}")
    quiz_prompt = (
        "Rakenna PÄIVÄN UUTISVISA näistä uutisartikkeleista. TASAN 5 kysymystä, kukin 5 vaihtoehtoa, yksi TAI "
        "useampi oikein. Perusta kysymykset VAIN siihen mitä artikkelit sanovat. Palauta VAIN JSON (ei muuta):\n"
        f'{{"title":"Päivän uutisvisa","date":"{date}","edition":"{edition}","questions":'
        '[{"q":"...","options":["a","b","c","d","e"],"correct":[0,2],"explain":"..."}]}\n'
        "questions-listassa TASAN 5 alkiota. correct = 0-pohjaiset numeeriset indeksit.\n\nUUTISET:\n" + "\n\n".join(arts))
    out = llm.call([{"role": "user", "content": quiz_prompt}])
    out = out if isinstance(out, str) else str(out)
    try:
        m = re.search(r"\{.*\}", out.strip().strip("`"), re.S)
        quiz = json.loads(m.group(0))
        if len(quiz.get("questions", [])) >= 1:
            _aimeat_call(agent_name, "aimeat_memory_write",
                         {"key": f"news.{date}.{edition}.quiz", "value": quiz, "visibility": "public"})
            lines.append(f"quiz={len(quiz['questions'])}Q")
        else:
            lines.append("quiz=FAILED(empty)")
    except Exception as e:  # noqa: BLE001
        lines.append(f"quiz=FAILED({type(e).__name__})")
    return "features: " + ", ".join(lines)


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

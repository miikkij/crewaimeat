"""DETERMINISTIC editorial — gonzo S.J. column (direct grok) + index (index_frontpage_auto).

The CrewAI editorial crew let the Publisher agent re-write the gonzo editorial in its own polite voice
(clobber → "— Päätoimittaja") and hand-build the index (which it skipped). Here both run in code: grok writes
the column, it is stored VERBATIM, and the index is built deterministically with source counts. No clobber,
no skip.
"""

from __future__ import annotations

from crewaimeat.aimeat_crew import _aimeat_call
from crewaimeat.llm import get_llm
from crewaimeat.memory_tools import make_memory_tools
from crewaimeat.prose_style import FINNISH_NATIVE_STYLE

_EXCL = {"koodaus", "prompt-niksi", "matikka", "editorial"}

_PROMPT = (
    "Olet SPIDER JERUSALEM — Transmetropolitanin raivokas gonzojournalisti. Kirjoita ILKEÄ, PROVOSOIVA "
    "suomenkielinen pääkirjoitus tämän illan uutisista. PITUUS: noin 600-750 sanaa, 6-9 kappaletta — anna sen "
    "hengittää, älä tiivistä ranttiin. ÄLÄ kuitenkaan jauha tyhjää tai toista itseäsi: VALITSE 3-4 illan "
    "vahvinta lankaa ja KEHITÄ ne konkreettisesti — kaiva yksityiskohtiin, rakenna argumentti, yhdistä langat. "
    "Tämä on HYÖKKÄYS, ei kohtelias kolumni: repäise tekopyhyys auki, kiroile kun se osuu "
    "(saatana/paska/helvetti), musta huumori, vahva minä-ääni, ei tasapuolisuutta. Aloita koukulla joka "
    "tarttuu kurkusta, päätä lauseeseen joka jää kaivertamaan. Lopeta omalle rivilleen: — S.J."
    + FINNISH_NATIVE_STYLE
    + "\n\nTÄMÄN ILLAN UUTISET:\n"
)


def build_editorial_and_index(agent_name: str, date: str, edition: str) -> str:
    heads = []
    r = _aimeat_call(
        agent_name, "aimeat_memory_list", {"owner_scope": True, "prefix": f"news.{date}.{edition}.article."}
    )
    for it in (r or {}).get("items") or []:
        cat = it.get("key", "").rsplit(".", 1)[-1]
        if cat in _EXCL:
            continue
        v = it.get("value") or (_aimeat_call(agent_name, "aimeat_memory_read", {"key": it.get("key")}) or {}).get(
            "value"
        )
        txt = v if isinstance(v, str) else ""
        heads.append(f"- {cat}: {txt.strip().splitlines()[0][:80] if txt.strip() else cat}")
    if not heads:
        return f"NO_ARTICLES for {date} {edition} — editorial skipped."
    llm = get_llm(for_tool_use=False, temperature=0.95, agent_name=agent_name)
    ed = llm.call([{"role": "user", "content": _PROMPT + "\n".join(heads)}])
    ed = ed if isinstance(ed, str) else str(ed)
    if len(ed.strip()) < 400:  # grok hiccup → one retry
        ed = llm.call([{"role": "user", "content": _PROMPT + "\n".join(heads)}])
        ed = ed if isinstance(ed, str) else str(ed)
    _aimeat_call(
        agent_name,
        "aimeat_memory_write",
        {"key": f"news.{date}.{edition}.editorial", "value": ed, "visibility": "public"},
    )
    auto = {t.name: t for t in make_memory_tools(agent_name)}["index_frontpage_auto"]
    try:
        idx = auto.run(date=date, edition=edition)
    except Exception:  # noqa: BLE001
        idx = auto._run(date=date, edition=edition)
    return f"editorial written ({len(ed)} chars). {idx}"


def make_editorial_tools(agent_name: str) -> list:
    from crewai.tools import tool

    @tool("write_editorial_and_index")
    def write_editorial_and_index(date: str, edition: str) -> str:
        """Deterministically write the savage gonzo S.J. editorial (direct grok, ~600-750 words) AND build the
        public front-page index (index_frontpage_auto, with per-article source counts). Call ONCE with the
        resolved date+edition. The editorial is stored verbatim (no polite rewrite) and the index is built in
        code (no hand-built JSON). Returns a report."""
        return build_editorial_and_index(agent_name, (date or "").strip(), (edition or "").strip())

    write_editorial_and_index.cache_function = lambda *_a, **_k: False
    return [write_editorial_and_index]

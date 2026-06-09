"""DETERMINISTIC article writing — the loop is code, the prose is grok.

The CrewAI writer crews left "which categories to write" to the LLM, and grok skipped ~30% (and wrote some
empty). Here the loop over categories runs in plain code: every category that has non-empty raw gets a
full-length article written by a direct grok call from the rich scraped raw. No category is silently dropped.

`write_edition_articles(agent_name, date, edition, categories)` writes news.<date>.<edition>.article.<cat>
for each category with raw and returns a report.
"""

from __future__ import annotations

import json

from crewaimeat.aimeat_crew import _aimeat_call
from crewaimeat.llm import get_llm

PERSONAS: dict[str, str] = {
    "talous": "Markus Markka", "politiikka-suomi": "Valtteri Valta", "politiikka-globaali": "Maija Maailma",
    "paikallinen": "Eila Espoo", "paivankohtaiset": "Antti Ajankohtainen", "kulttuuri": "Tuula Taide",
    "urheilu": "Tapio Kenttä", "tiede": "Aino Virta", "terveys": "Liisa Terve", "kevennykset": "Pekka Pilke",
    "saa": "Sää-Salla", "tekoaly": "Neela Verkko", "pelit": "Lumi Peliranta", "pelidevaus": "Devi Koodimaa",
    "startup": "Yrjö Kasvu", "yliluonnolliset": "Aave-Aino", "ruoka": "Maku-Matti",
    "luonto": "Erä-Eero", "mieli": "Mielen-Mervi", "filosofia": "Sofia Pohdiskelu",
}
DESK_A = ["talous", "paikallinen", "saa", "tiede", "politiikka-suomi", "politiikka-globaali",
          "paivankohtaiset", "urheilu", "kulttuuri", "terveys", "kevennykset"]
DESK_B = ["tekoaly", "pelit", "pelidevaus", "startup", "yliluonnolliset", "ruoka", "luonto",
          "mieli", "filosofia"]
_NEEDS = {  # extra per-category steer
    "yliluonnolliset": "Raportoi väitteet KRIITTISESTI, älä esitä yliluonnollista todistettuna.",
    "mieli": "Ei hälyttävä eikä diagnosoiva; kannusta hakemaan apua raskaissa aiheissa.",
}


def _read_raw(agent_name: str, category: str, date: str, edition: str) -> list:
    key = f"news.{date}.{edition}.raw.{category}"
    r = _aimeat_call(agent_name, "aimeat_memory_read", {"key": key})  # own gaii first
    v = r.get("value") if isinstance(r, dict) else None
    if v is None:  # the raw is written by news-fetcher (a sibling) → owner-scope cross-agent read
        lr = _aimeat_call(agent_name, "aimeat_memory_list", {"owner_scope": True, "prefix": key})
        for it in (((lr or {}).get("items") if isinstance(lr, dict) else None) or []):
            if it.get("key") == key and it.get("value") is not None:
                v = it.get("value")
                break
    if isinstance(v, str) and v.strip()[:1] == "[":
        try:
            v = json.loads(v)
        except Exception:  # noqa: BLE001
            v = []
    return v if isinstance(v, list) else []


def write_edition_articles(agent_name: str, date: str, edition: str, categories: list[str]) -> str:
    llm = get_llm(for_tool_use=False, temperature=0.7)
    lines = [f"deterministic write — {date} {edition} ({agent_name})"]
    for cat in categories:
        raw = _read_raw(agent_name, cat, date, edition)
        # require real scraped substance (not just a stub)
        body_chars = sum(len(str((a or {}).get("content") or "")) for a in raw if isinstance(a, dict))
        if not raw or body_chars < 200:
            lines.append(f"  {cat:18s} skip (no/thin raw, {body_chars} chars)")
            continue
        persona = PERSONAS.get(cat, cat.capitalize())
        extra = _NEEDS.get(cat, "")
        src = json.dumps(raw, ensure_ascii=False)[:10000]
        prompt = (f"Kirjoita TÄYSIMITTAINEN, syvällinen suomenkielinen uutisartikkeli kategoriaan '{cat}' näistä "
                  "lähteistä. VÄHINTÄÄN 4-6 kappaletta — ei stub, ei yksi kappale. Journalistinen ote, omin "
                  "sanoin (älä kopioi suoraan), taustoita ja yhdistä lähteet luontevaksi jutuksi. Aloita "
                  f"otsikolla. {extra} Lopeta omalle rivilleen '— {persona}'.\n\nLÄHTEET (JSON):\n{src}")
        art = llm.call([{"role": "user", "content": prompt}])
        art = art if isinstance(art, str) else str(art)
        if len(art.strip()) < 200:  # grok hiccup → one retry
            art = llm.call([{"role": "user", "content": prompt}])
            art = art if isinstance(art, str) else str(art)
        _aimeat_call(agent_name, "aimeat_memory_write",
                     {"key": f"news.{date}.{edition}.article.{cat}", "value": art, "visibility": "public"})
        lines.append(f"  {cat:18s} {len(art)} chars")
    return "\n".join(lines)


def make_write_tools(agent_name: str, desk: str) -> list:
    from crewai.tools import tool
    cats = DESK_A if desk.upper() == "A" else DESK_B

    @tool("write_edition_articles")
    def write_edition_articles_tool(date: str, edition: str) -> str:
        """Deterministically write a full Finnish article for EVERY category in this desk that has non-empty
        raw. Call ONCE with the resolved date+edition; the loop runs in code (no category skipped) and grok
        writes each article from the scraped raw. Returns a per-category char-count report."""
        return write_edition_articles(agent_name, (date or "").strip(), (edition or "").strip(), cats)

    write_edition_articles_tool.cache_function = lambda *_a, **_k: False
    return [write_edition_articles_tool]

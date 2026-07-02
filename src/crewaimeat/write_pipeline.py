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
from crewaimeat.prose_style import FINNISH_NATIVE_STYLE

PERSONAS: dict[str, str] = {
    "talous": "Markus Markka",
    "politiikka-suomi": "Valtteri Valta",
    "politiikka-globaali": "Maija Maailma",
    "paikallinen": "Eila Espoo",
    "paivankohtaiset": "Antti Ajankohtainen",
    "kulttuuri": "Tuula Taide",
    "urheilu": "Tapio Kenttä",
    "tiede": "Aino Virta",
    "terveys": "Liisa Terve",
    "kevennykset": "Pekka Pilke",
    "saa": "Sää-Salla",
    "tekoaly": "Neela Verkko",
    "pelit": "Lumi Peliranta",
    "pelidevaus": "Devi Koodimaa",
    "startup": "Yrjö Kasvu",
    "yliluonnolliset": "Aave-Aino",
    "ruoka": "Maku-Matti",
    "luonto": "Erä-Eero",
    "mieli": "Mielen-Mervi",
    "filosofia": "Sofia Pohdiskelu",
}
DESK_A = [
    "talous",
    "paikallinen",
    "saa",
    "tiede",
    "politiikka-suomi",
    "politiikka-globaali",
    "paivankohtaiset",
    "urheilu",
    "kulttuuri",
    "terveys",
    "kevennykset",
]
DESK_B = ["tekoaly", "pelit", "pelidevaus", "startup", "yliluonnolliset", "ruoka", "luonto", "mieli", "filosofia"]
_NEEDS = {  # extra per-category steer
    "yliluonnolliset": "Raportoi väitteet KRIITTISESTI, älä esitä yliluonnollista todistettuna.",
    "mieli": "Ei hälyttävä eikä diagnosoiva; kannusta hakemaan apua raskaissa aiheissa.",
}


class RawReadError(RuntimeError):
    """The raw read FAILED at the transport level (tunnel/serve down) — distinct from raw that is
    genuinely empty. We must not conflate the two: a failed read that looks 'empty' silently drops
    the category (the 06-20 incident, where a tunnel nykäys lost 7 article categories)."""


class WriteIncomplete(RuntimeError):
    """One or more categories could not be read or published (transport/LLM failure). The desk write
    is INCOMPLETE — raise so the step goes RED and the workflow retries it, never a silent partial."""

    def __init__(self, report: str, failed: list[str]):
        self.report = report
        self.failed = list(failed)
        super().__init__(
            f"write incomplete — {len(self.failed)} categ. failed (transport/LLM): {', '.join(self.failed)}"
        )


def _coerce_list(v) -> list:
    if isinstance(v, str) and v.strip()[:1] == "[":
        try:
            v = json.loads(v)
        except Exception:  # noqa: BLE001
            return []
    return v if isinstance(v, list) else []


def _read_raw(agent_name: str, category: str, date: str, edition: str) -> list:
    """The scraped raw for one category, or [] if it is genuinely empty/absent. Raises RawReadError
    if the read FAILS at the transport level — so the caller fails loud instead of silently treating
    a tunnel drop as 'no raw'. `_aimeat_call` already retries transient failures, so a None here means
    the failure persisted."""
    key = f"news.{date}.{edition}.raw.{category}"
    # Fast path: own-gaii read (best-effort; the owner-scope list below is the authoritative source).
    r = _aimeat_call(agent_name, "aimeat_memory_read", {"key": key})
    if isinstance(r, dict) and r.get("value") is not None:
        return _coerce_list(r.get("value"))
    # Authoritative: news-fetcher (a sibling) wrote the raw with owner visibility → owner-scope list.
    lr = _aimeat_call(agent_name, "aimeat_memory_list", {"owner_scope": True, "prefix": key})
    if lr is None:
        # Transport failure that survived the dispatcher's retries — do NOT pretend the raw is empty.
        raise RawReadError(f"raw read failed for '{category}' ({key}) — tunnel/transport down")
    for it in (lr.get("items") or []) if isinstance(lr, dict) else []:
        if it.get("key") == key and it.get("value") is not None:
            return _coerce_list(it.get("value"))
    return []  # the list call SUCCEEDED but the key is genuinely absent/empty


def _publish_article(agent_name: str, date: str, edition: str, category: str, article: str) -> bool:
    """Publish one article; True on success. `_aimeat_call` retries transient transport failures, so
    None back means the publish genuinely failed (tunnel down longer than the retries)."""
    res = _aimeat_call(
        agent_name,
        "aimeat_memory_write",
        {"key": f"news.{date}.{edition}.article.{category}", "value": article, "visibility": "public"},
    )
    return res is not None


def write_edition_articles(agent_name: str, date: str, edition: str, categories: list[str]) -> str:
    """Write a full article for every category with real raw. Resilient: a read/publish that fails
    at the transport level, or an LLM error on one category, is recorded and the loop CONTINUES with
    the rest — then, if anything failed, it raises WriteIncomplete so the step is honestly RED (and
    retried) rather than a silent partial. Idempotent — re-running fills only the gaps."""
    llm = get_llm(for_tool_use=False, temperature=0.7, agent_name=agent_name)
    # DESK MEMORY (delta reporting): recall what this desk already published on a similar story and
    # show it to the writer — news that resurfaces gets framed as "what changed", not retold from
    # zero. Optional enhancement: open_store degrades LOUD to None and the desk writes without it.
    from crewaimeat.pipeline_memory import open_store

    store = open_store(agent_name)
    lines = [f"deterministic write — {date} {edition} ({agent_name})"]
    failed: list[str] = []
    for cat in categories:
        try:
            raw = _read_raw(agent_name, cat, date, edition)
        except RawReadError as exc:
            lines.append(f"  {cat:18s} READ FAILED — {exc}")
            failed.append(cat)
            continue
        # require real scraped substance (not just a stub) — a genuinely-thin category is skipped, OK
        body_chars = sum(len(str((a or {}).get("content") or "")) for a in raw if isinstance(a, dict))
        if not raw or body_chars < 200:
            lines.append(f"  {cat:18s} skip (no/thin raw, {body_chars} chars)")
            continue
        persona = PERSONAS.get(cat, cat.capitalize())
        extra = _NEEDS.get(cat, "")
        src = json.dumps(raw, ensure_ascii=False)[:10000]
        # Prior coverage for THIS category, matched on today's raw sources: a resurfacing story is
        # written as its delta ("mitä uutta"), a fresh one is unaffected ("" when nothing similar).
        prior = (
            store.prior_art_block(
                src[:4000],
                k=3,
                min_score=0.45,
                label="AIEMMIN JULKAISTUA (tämä osasto)",
                category=cat,
                instruction=(
                    "olet jo kirjoittanut näistä aiheista alla olevat jutut. ÄLÄ toista niitä: jos päivän "
                    "lähteet ovat samaa tarinaa, kirjoita MITÄ UUTTA on tapahtunut ja viittaa aiempaan "
                    "lyhyesti; muuten jätä nämä huomiotta:"
                ),
            )
            if store
            else ""
        )
        prompt = (
            f"Kirjoita TÄYSIMITTAINEN, syvällinen suomenkielinen uutisartikkeli kategoriaan '{cat}' näistä "
            "lähteistä. VÄHINTÄÄN 4-6 kappaletta — ei stub, ei yksi kappale. Journalistinen ote, omin "
            "sanoin (älä kopioi suoraan), taustoita ja yhdistä lähteet luontevaksi jutuksi. Aloita "
            f"otsikolla. {extra} Lopeta omalle rivilleen '— {persona}'."
            + FINNISH_NATIVE_STYLE
            + (f"\n\n{prior}" if prior else "")
            + f"\n\nLÄHTEET (JSON):\n{src}"
        )
        try:
            art = llm.call([{"role": "user", "content": prompt}])
            art = art if isinstance(art, str) else str(art)
            if len(art.strip()) < 200:  # grok hiccup → one retry
                art = llm.call([{"role": "user", "content": prompt}])
                art = art if isinstance(art, str) else str(art)
        except Exception as exc:  # noqa: BLE001 — one bad LLM call must not lose the rest of the desk
            lines.append(f"  {cat:18s} WRITE FAILED — llm error: {exc}")
            failed.append(cat)
            continue
        if not _publish_article(agent_name, date, edition, cat, art):
            lines.append(f"  {cat:18s} {len(art)} chars — PUBLISH FAILED (tunnel/transport)")
            failed.append(cat)
            continue
        if store:  # remembered only when actually published — memory mirrors the paper
            store.remember(art, source="article", metadata={"date": date, "edition": edition, "category": cat})
        lines.append(f"  {cat:18s} {len(art)} chars")
    report = "\n".join(lines)
    if failed:
        raise WriteIncomplete(report, failed)
    return report


def make_write_tools(agent_name: str, desk: str) -> list:
    from crewai.tools import tool

    cats = DESK_A if desk.upper() == "A" else DESK_B

    @tool("write_edition_articles")
    def write_edition_articles_tool(date: str, edition: str) -> str:
        """Deterministically write a full Finnish article for EVERY category in this desk that has non-empty
        raw. Call ONCE with the resolved date+edition; the loop runs in code (no category skipped) and grok
        writes each article from the scraped raw. Returns a per-category char-count report."""
        try:
            return write_edition_articles(agent_name, (date or "").strip(), (edition or "").strip(), cats)
        except WriteIncomplete as exc:
            # Surface the partial report + the loud failure tail so the agent reports it; the workflow's
            # article-count gate still flags the desk RED, and the step retry re-runs to fill the gaps.
            return f"{exc.report}\n\nINCOMPLETE: {exc}"

    write_edition_articles_tool.cache_function = lambda *_a, **_k: False
    return [write_edition_articles_tool]

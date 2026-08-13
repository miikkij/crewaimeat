"""DETERMINISTIC article writing — the loop is code, the prose is grok.

The CrewAI writer crews left "which categories to write" to the LLM, and grok skipped ~30% (and wrote some
empty). Here the loop over categories runs in plain code: every category that has non-empty raw gets a
full-length article written by a direct grok call from the rich scraped raw. No category is silently dropped.

`write_edition_articles(agent_name, date, edition, categories)` writes news.<date>.<edition>.article.<cat>
for each category with raw and returns a report.
"""

from __future__ import annotations

import json

from aimeat_crewai.provenance import HumanInvolvement, Level, Method, declare, source

from crewaimeat.aimeat_crew import _aimeat_call
from crewaimeat.edition_status import step_status
from crewaimeat.llm import get_llm, resolved_model, resolved_provider
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
    "lukijoilta": "Vilma Vinkki",
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
    "lukijoilta",
]
DESK_B = ["tekoaly", "pelit", "pelidevaus", "startup", "yliluonnolliset", "ruoka", "luonto", "mieli", "filosofia"]
_NEEDS = {  # extra per-category steer
    "yliluonnolliset": "Raportoi väitteet KRIITTISESTI, älä esitä yliluonnollista todistettuna.",
    "mieli": "Ei hälyttävä eikä diagnosoiva; kannusta hakemaan apua raskaissa aiheissa.",
    "lukijoilta": (
        "Nämä ovat lukijoiden/omistajan ITSE kertomia uutisia (haastattelu tai vinkki, sanomat-desk keräsi ne). "
        "Säilytä kerrotut tapahtumat ja faktat sellaisenaan — satiiri saa näkyä tyylissä, EI keksityissä "
        "tapahtumissa tai henkilöissä. Jos lähteen 'images'-listassa on kuva-URLeja, upota ne juttuun "
        "markdown-kuvina (![kuvaus](url))."
    ),
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


def _read_edition_raw(agent_name: str, date: str, edition: str) -> dict | None:
    """The edition's ONE raw record as {category: [items]}, or None when that key does not exist.

    None means "this edition predates the single-key raw" and the caller falls back to the old
    per-category keys — which is what lets the fetcher and the desks ship in the same deploy without
    a flag day, and lets every one of the 68 already-published editions keep working untouched.
    Distinct from {}: an EMPTY categories map is a real (if useless) new-shape record, not an absence.

    Raises RawReadError on a transport-level failure, for the same reason `_read_raw` does: a tunnel
    drop that reads as 'no raw' silently drops the whole desk."""
    key = f"news.{date}.{edition}.raw"
    # quiet=True for the same reason as in _read_raw: news-fetcher wrote it, so the writer's own-gaii
    # probe is DESIGNED to miss and its NOT_FOUND line reads like a failure mid-healthy-edition.
    r = _aimeat_call(agent_name, "aimeat_memory_read", {"key": key}, quiet=True)
    value = r.get("value") if isinstance(r, dict) else None
    if value is None:
        lr = _aimeat_call(agent_name, "aimeat_memory_list", {"owner_scope": True, "prefix": key})
        if lr is None:
            raise RawReadError(f"raw read failed for {date} {edition} ({key}) — tunnel/transport down")
        for it in (lr.get("items") or []) if isinstance(lr, dict) else []:
            # An exact-key match: the prefix also matches the OLD news.<date>.<edition>.raw.<cat> keys.
            if it.get("key") == key and it.get("value") is not None:
                value = it.get("value")
                break
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    cats = value.get("categories") if isinstance(value, dict) else None
    return {k: _coerce_list(v) for k, v in cats.items()} if isinstance(cats, dict) else None


def _read_raw(agent_name: str, category: str, date: str, edition: str) -> list:
    """The scraped raw for one category from the OLD per-category key, or [] if it is genuinely
    empty/absent. Raises RawReadError if the read FAILS at the transport level — so the caller fails
    loud instead of silently treating a tunnel drop as 'no raw'. `_aimeat_call` already retries
    transient failures, so a None here means the failure persisted.

    THE FALLBACK PATH. Editions from before the single-key raw are read through here; today's are
    read once by `_read_edition_raw`. Remove it after one real 17:00 run has been checked end to end
    — not before, and not by deleting the old keys, which age out on their own."""
    key = f"news.{date}.{edition}.raw.{category}"
    # Fast path: own-gaii read. quiet=True because this probe is DESIGNED to miss — the raw was
    # written by news-fetcher, so it is never under the writer's own GAII, and the owner-scope list
    # below is the real source. Logging it printed a NOT_FOUND line per category per edition that
    # reads like a failure while the edition is publishing perfectly.
    r = _aimeat_call(agent_name, "aimeat_memory_read", {"key": key}, quiet=True)
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


def _publish_article(
    agent_name: str,
    date: str,
    edition: str,
    category: str,
    article: str,
    raw: list | None = None,
    model: str | None = None,
    provider: str | None = None,
) -> bool:
    """Publish one article; True on success. `_aimeat_call` retries transient transport failures, so
    None back means the publish genuinely failed (tunnel down longer than the retries).

    PROVENANCE: this is the write a READER actually sees, so it is where the label belongs. A model
    wrote the prose from real scraped material at the desk's direction — that is SYNTHESIZED, and the
    `sources` are the scraped URLs, which is what lets a reader follow the claim instead of taking it
    on trust. human_involvement stays NONE: the desk runs on a schedule (18:00) and nobody reads the
    article before it goes public. The owner queueing the edition is not a person reading the
    substance with the power to reject it."""
    # Cite title + retrieved_at when the raw recorded them, so a reader gets a followable reference
    # rather than a bare link. NEVER reconstruct either: an item scraped before fetch_pipeline started
    # stamping `fetched_at` simply has no retrieval time, and omitting it is the honest answer. An
    # invented source list is worse than an empty one — it is the exact failure this paper exists to
    # expose, published under a label that claims machine-checkable provenance.
    srcs, seen = [], set()
    for a in raw or []:
        if not isinstance(a, dict) or not a.get("url"):
            continue
        u = str(a["url"])
        if u in seen:
            continue
        seen.add(u)
        srcs.append(source(u, title=(a.get("title") or None), retrieved_at=(a.get("fetched_at") or None)))
    res = _aimeat_call(
        agent_name,
        "aimeat_memory_write",
        {
            "key": f"news.{date}.{edition}.article.{category}",
            "value": article,
            "visibility": "public",
            "ai_provenance": declare(
                Level.SYNTHESIZED,
                method=Method.SYNTHESIZED,
                human_involvement=HumanInvolvement.NONE,
                model=model,
                provider=provider,  # who SERVED it — a router alias answers neither question alone
                sources=srcs,
            ),
        },
    )
    return res is not None


def write_edition_articles(
    agent_name: str, date: str, edition: str, categories: list[str], *, status_step: str | None = None
) -> str:
    """Write a full article for every category with real raw. Resilient: a read/publish that fails
    at the transport level, or an LLM error on one category, is recorded and the loop CONTINUES with
    the rest — then, if anything failed, it raises WriteIncomplete so the step is honestly RED (and
    retried) rather than a silent partial. Idempotent — re-running fills only the gaps.

    `status_step` ("writeA" | "writeB") names the field this run owns in the edition's shared status
    record. It is explicit and defaults to None — an ad-hoc partial re-write of three categories is
    not the workflow's Desk A step, and inferring one from the category list would let it claim to
    be. The workflow's two call sites (make_write_tools, the inspector's re-run) pass it; nothing
    else needs to."""
    if status_step is None:
        return _write_edition_articles(agent_name, date, edition, categories)
    with step_status(agent_name, date, edition, status_step):
        return _write_edition_articles(agent_name, date, edition, categories)


def _write_edition_articles(agent_name: str, date: str, edition: str, categories: list[str]) -> str:
    llm = get_llm(for_tool_use=False, temperature=0.7, agent_name=agent_name)
    # DESK MEMORY (delta reporting): recall what this desk already published on a similar story and
    # show it to the writer — news that resurfaces gets framed as "what changed", not retold from
    # zero. Optional enhancement: open_store degrades LOUD to None and the desk writes without it.
    from crewaimeat.pipeline_memory import open_store

    store = open_store(agent_name)
    lines = [f"deterministic write — {date} {edition} ({agent_name})"]
    failed: list[str] = []
    # ONE read for the whole desk (the edition's single raw record), instead of one per category.
    # None = an edition written before the consolidation → fall back to the old per-category keys.
    try:
        edition_raw = _read_edition_raw(agent_name, date, edition)
    except RawReadError as exc:
        # The one read the whole desk depends on failed at the transport level. Report it in the
        # desk's own shape (every category failed) so the step goes RED and is retried — the same
        # contract as before, when the failure surfaced one category at a time.
        lines.append(f"  RAW READ FAILED — {exc}")
        raise WriteIncomplete("\n".join(lines), list(categories)) from exc
    if edition_raw is None:
        lines.append("  (no single-key raw — reading the pre-consolidation news.*.raw.<category> keys)")
    for cat in categories:
        try:
            raw = edition_raw.get(cat) if edition_raw is not None else None
            if raw is None:
                # Not in the consolidated record. TWO reasons, one rule: either this edition predates
                # the consolidation, or the category has a DIFFERENT PRODUCER — `lukijoilta` is written
                # by sanomat-desk as reader tips arrive by DM, which can be long after the 17:00 fetch,
                # so it keeps its own key rather than being merged into a record news-fetcher owns.
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
        # THE DATING RULE. On 2026-08-13 a desk wrote "Espoon Tapiolassa viime torstaina aamulla
        # sattunut kävelysillan romahdus" about an accident from 2024: the source was a category
        # LANDING PAGE whose teasers span years, the raw carried no publication date, and the model
        # filled the gap with recency nobody had claimed. The fetch step now records `published_at`
        # (None when the source states none) — this tells the desk what to do with it. It is stated
        # before the sources, in the imperative, because it is the one error that turns the paper
        # into misinformation rather than merely bad prose.
        dating = (
            "\n\nAJANKOHTA — EHDOTON SÄÄNTÖ:\n"
            "Jokaisella lähteellä on kenttä `published_at`. Se on lähteen OMA julkaisupäivä, tai "
            "null jos lähde ei kerro sitä. `fetched_at` on vain se hetki jolloin me haimme sivun — "
            "se EI kerro milloin tapahtuma sattui.\n"
            "- ÄLÄ KOSKAAN kirjoita 'tänään', 'eilen', 'viime torstaina', 'viikonloppuna' tai muuta "
            "ajankohtaa, ellei se lue lähteen tekstissä tai käy ilmi `published_at`-kentästä.\n"
            "- Jos `published_at` on null, kirjoita tapahtumasta ILMAN ajankohtaa. Se on täysin "
            "hyväksyttävää: 'Tapiolassa on sattunut kävelysillan romahdus' on oikein, "
            "'viime torstaina sattunut' on väärin jos kukaan ei ole niin sanonut.\n"
            "- Jos `published_at` on selvästi vanha, älä esitä asiaa uutena. Kerro se taustana tai "
            "jätä se pois.\n"
            "- Listasivu tai hälytysnäkymä lähteenä: siinä on eri-ikäisiä nostoja. Älä oleta että "
            "mikään niistä on tuore."
        )
        prompt = (
            f"Kirjoita TÄYSIMITTAINEN, syvällinen suomenkielinen uutisartikkeli kategoriaan '{cat}' näistä "
            "lähteistä. VÄHINTÄÄN 4-6 kappaletta — ei stub, ei yksi kappale. Journalistinen ote, omin "
            "sanoin (älä kopioi suoraan), taustoita ja yhdistä lähteet luontevaksi jutuksi. Aloita "
            f"otsikolla. {extra} Lopeta omalle rivilleen '— {persona}'."
            + FINNISH_NATIVE_STYLE
            + dating
            + (f"\n\n{prior}" if prior else "")
            + f"\n\nTÄNÄÄN ON {date}.\n\nLÄHTEET (JSON):\n{src}"
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
        # The model that actually served THIS article — read immediately after the generating call,
        # before anything else can route a completion (the desk loops over categories).
        used_model, used_provider = resolved_model(llm), resolved_provider()
        if not _publish_article(agent_name, date, edition, cat, art, raw=raw, model=used_model, provider=used_provider):
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
    step = "writeA" if desk.upper() == "A" else "writeB"

    @tool("write_edition_articles")
    def write_edition_articles_tool(date: str, edition: str) -> str:
        """Deterministically write a full Finnish article for EVERY category in this desk that has non-empty
        raw. Call ONCE with the resolved date+edition; the loop runs in code (no category skipped) and grok
        writes each article from the scraped raw. Returns a per-category char-count report."""
        try:
            return write_edition_articles(
                agent_name, (date or "").strip(), (edition or "").strip(), cats, status_step=step
            )
        except WriteIncomplete as exc:
            # Surface the partial report + the loud failure tail so the agent reports it; the workflow's
            # article-count gate still flags the desk RED, and the step retry re-runs to fill the gaps.
            return f"{exc.report}\n\nINCOMPLETE: {exc}"

    write_edition_articles_tool.cache_function = lambda *_a, **_k: False
    return [write_edition_articles_tool]

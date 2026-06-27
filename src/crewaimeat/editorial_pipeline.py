"""DETERMINISTIC editorial — gonzo S.J. column (direct grok) + index (index_frontpage_auto).

The CrewAI editorial crew let the Publisher agent re-write the gonzo editorial in its own polite voice
(clobber → "— Päätoimittaja") and hand-build the index (which it skipped). Here both run in code; the
editorial is stored VERBATIM and the index is built deterministically with source counts. No clobber, no skip.

TWO-STEP editorial (since grok writing Finnish in one shot produced English-calqued, word-hallucinating
prose — "perunagruuvi", "keittiön hattit"): (1) grok writes the gonzo column in ENGLISH — its strongest
register, coherent, full Spider-Jerusalem voice, no invented words; (2) a LOCALISE pass rewrites it as
NATIVE Finnish gonzo (anchored to the English so meaning + every barb survive, lower temperature for
fidelity, instructed to rewrite as a Finnish gonzo writer — not translate). Only the Finnish is stored.
"""

from __future__ import annotations

from crewaimeat.aimeat_crew import _aimeat_call
from crewaimeat.llm import get_llm
from crewaimeat.memory_tools import make_memory_tools
from crewaimeat.prose_style import FINNISH_NATIVE_STYLE

_EXCL = {"koodaus", "prompt-niksi", "matikka", "editorial"}

# STEP 1 — compose the gonzo column in ENGLISH (grok's strongest register; no Finnish word-hallucination).
_PROMPT_EN = (
    "You are SPIDER JERUSALEM — the savage gonzo journalist of Transmetropolitan. Write a VICIOUS, "
    "PROVOCATIVE editorial column about tonight's news. LENGTH: ~600-750 words, 6-9 paragraphs — let it "
    "breathe, don't compress to a rant. But DON'T pad or repeat yourself: PICK the 3-4 strongest threads and "
    "DEVELOP them concretely — dig into specifics, build the argument, connect the threads. This is an "
    "ATTACK, not a polite column: rip hypocrisy open, swear when it lands (fuck/shit/hell), black humour, a "
    "strong first-person voice, no false balance. Open with a hook that grabs the throat; end on a line that "
    "keeps cutting. Finish on its own line: — S.J.\n\nTONIGHT'S NEWS:\n"
)

# STEP 2 — localise that English column into NATIVE Finnish gonzo. Anchored to the English (meaning + barbs
# survive), rewritten idiomatically (NOT translated word-for-word), gonzo voice + profanity fully preserved.
_PROMPT_LOCALIZE = (
    "Olet suomalainen gonzo-toimittaja — Spider Jerusalem suomeksi. Alla on englanninkielinen gonzo-"
    "pääkirjoitus. KIRJOITA SE UUDELLEEN raivokkaaksi, syntyperäiseksi suomeksi. TÄRKEÄÄ: älä käännä sanasta "
    "sanaan — kirjoita kuin suomalainen gonzo-kirjoittaja kirjoittaisi tämän kolumnin alusta asti. SÄILYTÄ "
    "JOKAINEN piikki, koko satiiri ja musta huumori, rant-rytmi, vahva minä-ääni JA kiroilu "
    "(saatana/paska/helvetti) — älä pehmennä äläkä siistitä. ÄLÄ keksi sanoja: jos et tiedä suomenkielistä "
    "ilmaisua, käytä luontevaa arkisuomea, älä englannin idiomin sananmukaista käännöstä. Pidä sama pituus ja "
    "kappalejako. Lopuksi omalle rivilleen: — S.J." + FINNISH_NATIVE_STYLE + "\n\nENGLANNINKIELINEN PÄÄKIRJOITUS:\n"
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

    # STEP 1: English gonzo draft (high temperature for voice).
    llm_en = get_llm(for_tool_use=False, temperature=0.95, agent_name=agent_name)
    en = llm_en.call([{"role": "user", "content": _PROMPT_EN + "\n".join(heads)}])
    en = en if isinstance(en, str) else str(en)
    if len(en.strip()) < 400:  # grok hiccup → one retry
        en = llm_en.call([{"role": "user", "content": _PROMPT_EN + "\n".join(heads)}])
        en = en if isinstance(en, str) else str(en)

    # STEP 2: native-Finnish gonzo localisation (lower temperature for fidelity — anchored to the English).
    # Route to the grok-FREE default profile (content-free -> gpt-oss-120b): grok garbles Finnish (English
    # calques + invented words — proven), gpt-oss writes it natively. agent_name=None -> the default profile.
    llm_fi = get_llm(for_tool_use=False, temperature=0.65, agent_name=None)
    ed = llm_fi.call([{"role": "user", "content": _PROMPT_LOCALIZE + en.strip()}])
    ed = ed if isinstance(ed, str) else str(ed)
    if len(ed.strip()) < 400:  # localise hiccup → one retry
        ed = llm_fi.call([{"role": "user", "content": _PROMPT_LOCALIZE + en.strip()}])
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
    return f"editorial written (EN draft {len(en.strip())} -> FI {len(ed.strip())} chars). {idx}"


def make_editorial_tools(agent_name: str) -> list:
    from crewai.tools import tool

    @tool("write_editorial_and_index")
    def write_editorial_and_index(date: str, edition: str) -> str:
        """Deterministically write the savage gonzo S.J. editorial in TWO steps — (1) grok drafts it in
        English, (2) a localise pass rewrites it as native Finnish gonzo (so the Finnish reads natively and
        stops calquing English / inventing words, while the voice survives) — AND build the public front-page
        index (index_frontpage_auto, with per-article source counts). Call ONCE with the resolved
        date+edition. Only the Finnish is stored (verbatim, no polite rewrite). Returns a report."""
        return build_editorial_and_index(agent_name, (date or "").strip(), (edition or "").strip())

    write_editorial_and_index.cache_function = lambda *_a, **_k: False
    return [write_editorial_and_index]

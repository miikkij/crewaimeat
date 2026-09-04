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

from aimeat_crewai.provenance import HumanInvolvement, Level, Method, declare

from crewaimeat.aimeat_crew import _aimeat_call
from crewaimeat.edition_status import step_status
from crewaimeat.llm import get_llm, resolved_model, resolved_provider
from crewaimeat.memory_tools import make_memory_tools
from crewaimeat.prose_style import FINNISH_NATIVE_STYLE

_EXCL = {"koodaus", "prompt-niksi", "matikka", "editorial"}

# THE VOICE LIVES IN THE SKILL, NOT HERE. `skills/sanomat-editorial-style/SKILL.md` is the house
# editorial craft — voice, structure, the two-step language rule, the hard rules. It used to be a
# parallel copy of the two prompt strings below, which is the worst of both worlds: a versioned,
# shareable, registry-publishable pack that nothing read, sitting beside the prompts that actually
# ran and slowly diverging from them. Now the skill IS the instruction and these prompts only frame
# the concrete step. Editing the voice means editing the skill — one place, and it travels.
#
# Loaded lazily (not at import) so a missing skill fails the EDITORIAL STEP loudly, marked failed on
# the edition's status record, instead of taking the whole fleet down at import time.
EDITORIAL_SKILL = "sanomat-editorial-style"

# STEP 1 — compose the gonzo column in ENGLISH (the model's strongest register; no Finnish
# word-hallucination). The skill's "two-step language rule" is why this step exists at all.
_TASK_EN = (
    "\n\nYOUR TASK NOW — STEP 1 of the two-step language rule: write TONIGHT'S column in ENGLISH, "
    "applying everything above. Output the column only, nothing else."
    "\n\nTONIGHT'S NEWS:\n"
)

# STEP 2 — localise that English column into NATIVE Finnish gonzo. Anchored to the English (meaning +
# barbs survive), rewritten idiomatically (NOT translated word-for-word), voice + profanity preserved.
_TASK_LOCALIZE = (
    "\n\nTEHTÄVÄSI NYT — kaksivaiheisen kielisäännön VAIHE 2: kirjoita alla oleva englanninkielinen "
    "kolumni uudelleen syntyperäiseksi suomalaiseksi gonzoksi. Älä käännä sanasta sanaan — kirjoita "
    "kuin suomalainen gonzo-kirjoittaja kirjoittaisi sen alusta asti. Sama pituus ja kappalejako, "
    "JOKAINEN piikki, satiiri, musta huumori ja kiroilu säilytettynä. Tulosta pelkkä kolumni."
    + FINNISH_NATIVE_STYLE
    + "\n\nENGLANNINKIELINEN PÄÄKIRJOITUS:\n"
)


def _voice() -> str:
    """The house editorial craft, from the skill pack. Raises loudly if it cannot be loaded."""
    from crewaimeat.skills import skill_body

    return skill_body(EDITORIAL_SKILL)


def prompt_en() -> str:
    return _voice() + _TASK_EN


def prompt_localize() -> str:
    return _voice() + _TASK_LOCALIZE


def build_editorial_and_index(agent_name: str, date: str, edition: str) -> str:
    with step_status(agent_name, date, edition, "editorial") as st:
        report = _build_editorial_and_index(agent_name, date, edition)
        # NO_ARTICLES is a refusal, not a finished column — the desks have not delivered yet.
        if report.startswith("NO_ARTICLES"):
            st.fail()
        return report


def _build_editorial_and_index(agent_name: str, date: str, edition: str) -> str:
    heads = []
    r = _aimeat_call(
        agent_name, "aimeat_memory_list", {"owner_scope": True, "prefix": f"news.{date}.{edition}.article."}
    )
    for it in (r or {}).get("items") or []:
        cat = it.get("key", "").rsplit(".", 1)[-1]
        if cat in _EXCL:
            continue
        # owner_scope because the listing above asks for it — these articles belong to the owner's
        # family, and a read without the scope answers None for every one of them (measured
        # 2026-09-04: 21 present, 0 seen). The front page would then be built from nothing.
        v = (_aimeat_call(agent_name, "aimeat_memory_read", {"key": it.get("key"), "owner_scope": True}) or {}).get(
            "value"
        )
        txt = v if isinstance(v, str) else ""
        heads.append(f"- {cat}: {txt.strip().splitlines()[0][:80] if txt.strip() else cat}")
    if not heads:
        return f"NO_ARTICLES for {date} {edition} — editorial skipped."

    # EDITORIAL MEMORY (continuity + anti-repetition): recall the most similar PAST editorials for
    # tonight's headlines and hand them to the drafting model as prior art — don't repeat an angle,
    # optionally call back to one by date (a real editorial voice across days). Optional enhancement:
    # open_store degrades LOUD to None when no embedder is reachable and the paper still ships.
    from crewaimeat.pipeline_memory import open_store

    store = open_store(agent_name)
    prior = (
        store.prior_art_block("\n".join(heads), k=3, label="YOUR PREVIOUS COLUMNS (in Finnish)", category="editorial")
        if store
        else ""
    )
    if prior:
        prior = (
            "\n\n" + prior + "\n"
            "Those are YOUR previous columns. DO NOT reuse their angles or punchlines — find a fresh "
            "attack. You MAY call back to ONE of them by its date in a single line (continuity, not reruns).\n"
        )

    # STEP 1: English gonzo draft (high temperature for voice).
    en_prompt = prompt_en() + "\n".join(heads) + prior
    llm_en = get_llm(for_tool_use=False, temperature=0.95, agent_name=agent_name)
    en = llm_en.call([{"role": "user", "content": en_prompt}])
    en = en if isinstance(en, str) else str(en)
    if len(en.strip()) < 400:  # grok hiccup → one retry
        en = llm_en.call([{"role": "user", "content": en_prompt}])
        en = en if isinstance(en, str) else str(en)

    # STEP 2: native-Finnish gonzo localisation (lower temperature for fidelity — anchored to the English).
    # This step used to be forced onto the DEFAULT profile (agent_name=None -> content-free -> gpt-oss),
    # because grok garbles Finnish (English calques, invented words) and gpt-oss did not. That constraint
    # was about GROK, not about gpt-oss being the best Finnish available — and it quietly meant the
    # editorial's SHIPPING text ignored whatever the desk was routed to.
    # Since 2026-08-12 the Sanomat crews run the `news` profile (DeepSeek V4 Pro), whose Finnish was
    # measured before switching: idiomatic, no calques. So the localisation now uses the crew's OWN
    # routing like every other step. The two-step itself stays — the English draft is what carries the
    # gonzo voice, and anchoring the Finnish to it is why the column reads as written rather than translated.
    llm_fi = get_llm(for_tool_use=False, temperature=0.65, agent_name=agent_name)
    ed = llm_fi.call([{"role": "user", "content": prompt_localize() + en.strip()}])
    ed = ed if isinstance(ed, str) else str(ed)
    if len(ed.strip()) < 400:  # localise hiccup → one retry
        ed = llm_fi.call([{"role": "user", "content": prompt_localize() + en.strip()}])
        ed = ed if isinstance(ed, str) else str(ed)

    # PROVENANCE: two models wrote this — one drafted the English column from the day's own articles,
    # a second localised it to native Finnish. SYNTHESIZED (real material recombined at the desk's
    # direction) and the model named is the one whose words actually ship, the Finnish localiser.
    # No `sources`: the inputs are this paper's own internal article keys, not URLs a reader could
    # follow, and a source list nobody can open would dress the column up without informing anyone.
    # Scheduled publish, no reviewer -> human_involvement NONE.
    _aimeat_call(
        agent_name,
        "aimeat_memory_write",
        {
            "key": f"news.{date}.{edition}.editorial",
            "value": ed,
            "visibility": "public",
            "ai_provenance": declare(
                Level.SYNTHESIZED,
                method=Method.SYNTHESIZED,
                human_involvement=HumanInvolvement.NONE,
                model=resolved_model(llm_fi),
                provider=resolved_provider(),
            ),
        },
    )
    # Remember the PUBLISHED Finnish column (query language = storage language, so tomorrow's FI
    # headlines match against FI columns) — this is what future prior-art recalls rank against.
    if store:
        store.remember(ed, source="editorial", metadata={"date": date, "edition": edition, "category": "editorial"})
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

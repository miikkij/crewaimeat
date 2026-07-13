"""Legal screen — the intake gate for EXTERNAL user material entering (L)AIMEAT Sanomat.

Scope (owner's decision, 2026-07-13): the screen covers ONLY material that arrives from OUTSIDE —
a federation user's tip, an attached photo, a correction request's quoted claim. Our OWN production
(the fetch->write pipeline, the owner's interview answers) is NOT screened here.

This is a SCREEN, not legal advice: one low-temperature LLM pass against fixed criteria relevant to
a satirical publication (identifiable private persons, defamation risk, personal data, image rights,
plainly illegal content). The verdict is strict JSON; anything unparseable RAISES — a broken screen
must never silently wave material through (fail loud). A flagged tip is declined at the boundary and
the owner is notified; it never becomes raw.
"""

from __future__ import annotations

import json
import re

from crewaimeat.llm import get_llm

CRITERIA = (
    "1. YKSITYISHENKILÖT: nimetäänkö tai kuvataanko tunnistettava yksityishenkilö (ei-julkinen henkilö)?\n"
    "2. KUNNIANLOUKKAUS: esitetäänkö kenestäkään todellisesta henkilöstä tai yrityksestä halventavia "
    "VÄITTEITÄ tosiasioina (satiiriksi tunnistamaton)?\n"
    "3. HENKILÖTIEDOT: sisältääkö materiaali arkaluontoisia henkilötietoja (osoite, hetu, terveystieto, "
    "taloustieto) jostakusta muusta kuin lähettäjästä itsestään?\n"
    "4. KUVAOIKEUDET: viittaako mikään siihen, että liitekuva on jonkun muun ottama/omistama tai esittää "
    "sivullisia tunnistettavasti ilman asiayhteyden tukea?\n"
    "5. LAITON SISÄLTÖ: uhkaus, vihapuhe, väkivaltaan yllyttäminen tai muu selvästi laiton aines?"
)


class LegalScreenUnavailable(RuntimeError):
    """The screening LLM call failed or returned unparseable output. The caller MUST treat this as
    'material not accepted' — never as a pass."""


def _parse_verdict(raw: str) -> dict:
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        raise LegalScreenUnavailable(f"screen verdict unparseable: {raw[:200]!r}")
    try:
        data = json.loads(m.group(0))
    except Exception as exc:  # noqa: BLE001
        raise LegalScreenUnavailable(f"screen verdict bad JSON: {raw[:200]!r}") from exc
    if not isinstance(data, dict) or "ok" not in data:
        raise LegalScreenUnavailable(f"screen verdict missing 'ok': {raw[:200]!r}")
    return {
        "ok": bool(data.get("ok")),
        "issues": [str(i) for i in data.get("issues") or [] if str(i).strip()],
        "summary": str(data.get("summary") or "").strip(),
    }


def screen_external(agent: str, *, sender: str, text: str, attachment_notes: str = "") -> dict:
    """Screen one piece of EXTERNAL material. Returns {"ok": bool, "issues": [...], "summary": str}.
    ok=True -> the material may enter the pipeline; ok=False -> decline at the boundary.
    Raises LegalScreenUnavailable when the screen itself cannot run (callers decline, loudly)."""
    llm = get_llm(for_tool_use=False, temperature=0.1, agent_name=agent)
    prompt = (
        "Olet satiirisen verkkolehden ((L)AIMEAT Sanomat) lakiosaston seula. Ulkopuolinen käyttäjä "
        f"({sender}) lähetti toimitukselle materiaalia. Arvioi VAIN alla olevat kriteerit — tyyli, laatu "
        "tai uutisarvo EIVÄT kuulu sinulle. Lehti on avoimesti satiirinen; se ei tee väitteistä laillisia, "
        "mutta huumori itsessään ei ole ongelma.\n\n"
        f"KRITEERIT:\n{CRITERIA}\n\n"
        f"MATERIAALI:\n-----\n{text[:6000]}\n-----\n"
        + (f"\nLIITTEIDEN ANALYYSI:\n{attachment_notes[:3000]}\n" if attachment_notes else "")
        + "\nVastaa PELKÄLLÄ JSON-objektilla: "
        '{"ok": true/false, "issues": ["kriteerin numero + lyhyt syy", ...], "summary": "yksi lause"}. '
        "ok=false vain jos jokin kriteeri AIDOSTI täyttyy — älä liputa varmuuden vuoksi."
    )
    try:
        raw = llm.call([{"role": "user", "content": prompt}])
    except Exception as exc:  # noqa: BLE001
        raise LegalScreenUnavailable(f"screen LLM call failed: {exc!r}") from exc
    return _parse_verdict(raw if isinstance(raw, str) else str(raw))

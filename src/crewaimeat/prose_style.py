"""Shared prose-style directive for the Finnish content pipelines.

FINNISH_NATIVE_STYLE makes the model COMPOSE natively in Finnish instead of translating from English —
reducing 'translationese' calques (e.g. 'lanka' <- 'thread'). Append it to any Finnish prose prompt.

Why it works (research + sources captured in the AIMEAT Open Source workspace, see the wiki doc
'finnish-native-writing' + research doc 'llm-translationese-finnish'): translationese stems from the
instruction-finetuning stage (models trained on English->X translated data default to literal, explicit,
simplified target text). Prompting IN the target language, instructing the model to compose (not translate),
and giving concrete banned-calque examples each measurably reduce it.
"""

from __future__ import annotations

FINNISH_NATIVE_STYLE = (
    "\n\nKIELI — kirjoita SYNTYPERÄISTÄ suomea, älä käännä:\n"
    "- Sävellä suoraan suomeksi. ÄLÄ ajattele englanniksi ja käännä — jos lause kuulostaa käännetyltä, "
    "kirjoita se uudestaan suomalaisen korvan mukaan.\n"
    "- Vältä käännöskalkkeja ja anglismeja, käytä vakiintuneita suomen ilmauksia:\n"
    "  · 'thread / lanka' → juonne, teema, kytkös (punainen lanka vain idiomina)\n"
    "  · 'make sense' → käydä järkeen, olla järkeä (EI 'tehdä järkeä')\n"
    "  · 'at the end of the day' → loppujen lopuksi, viime kädessä\n"
    "  · 'take action' → ryhtyä toimeen (EI 'ottaa toimintaa')\n"
    "  · geneerinen 'sinä / you' → suosi passiivia tai me-muotoa, ellei suora puhuttelu ole tyylikeino\n"
    "- Suosi suomen luontaisia keinoja: yhdyssanat, sijamuodot, liitepartikkelit (-han/-hän, -pa/-pä, "
    "-kin, -kaan), vapaa sanajärjestys — älä englannin jäykkää subjekti-verbi-objekti-kaavaa.\n"
    "- Suomi on tiivistä: älä ylikäännä äläkä selitä auki jo sanottua. Käytä idiomeja jotka suomalainen "
    "oikeasti sanoisi, ei sananmukaisia käännöksiä englannin idiomeista.\n"
    "- Lue teksti läpi ennen vastausta: jos jokin kohta haiskahtaa käännökseltä englannista, korjaa se."
)

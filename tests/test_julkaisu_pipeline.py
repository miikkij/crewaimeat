"""JULKAISUPÖYTÄ floor — the parts that must never depend on a model.

Which key each run writes, that the editor's material is required, that the house rules are actually
enforced before anything is stored, and that a number is never invented. No LLM and no network: the
write paths run with a stubbed ``_aimeat_call`` and a stubbed LLM, so every assertion is about the
pipeline's decisions rather than a model's prose.
"""

from __future__ import annotations

import re

import pytest

import crewaimeat.julkaisu_desk as jd
import crewaimeat.julkaisu_pipeline as jp

# ── shared fixtures ──────────────────────────────────────────────────────────────────────────────
AINEISTO = {
    "valittu": "Decide what your AI may do the moment you connect it",
    "paiva": "2026-08-24",
    "kulma": "Yhteys ei ole enää valmis ennen kuin olet päättänyt, mitä agentti saa tehdä.",
    "ennen": "Yhteys syntyi niillä oikeuksilla jotka agentilla sattui olemaan, ja muuttaminen vaati "
    "koko yhteyden purkamisen.",
    "nyt": "Hyväksymisikkuna kysyy oikeudet ennen kuin yhteys valmistuu.",
    "kenelle": "ihmiset jotka kytkevät claude.ai:n omaan dataansa ensimmäistä kertaa",
    "todiste": "uudelleenkytkentä katosi kokonaan",
    "ei_kerrota": ["agentin nimi identiteettinä, jonka alle tekemiset kirjataan (GAII)"],
    "varmuus": "En löytänyt lukua keskeytyksistä.",
    "lahde": "https://aimeat.io/changelog.json#2026-08-24",
}


# KANSI v3: the writers are handed the angle A PERSON chose, plus the sourced research behind it.
ANGLE = {
    "nro": 3,
    "otsikko": "Määräpäivä tekee tästä pakollisen",
    "kulma": "Yhteys ei ole enää valmis ennen kuin olet päättänyt, mitä agentti saa tehdä.",
    "avaus": "Sinulla on yksitoista kuukautta aikaa siihen, että tämä ikkuna on pakko olla.",
    "miksi_toimii": "Kohdeyleisö reagoi määräpäivään, ei käyttöliittymäpäivitykseen.",
    "kenelle": "integraatioita rakentavat kehittäjät",
    "nojaa": "Artikla 50 alkaa 2.8.2026",
    "todennakoisyys": 74,
    "perustelu": "Kova fakta, mutta kuiva aihe.",
    "ohjaaja_ele": "inspired by David Fincher: yksi luku ruudulla",
    "riski": "Kuulostaa pelottelulta jos määräpäivä on ainoa argumentti.",
}
VALINTA = {
    "vastaus": "valittu",
    "nro": 3,
    "kulma": ANGLE,
    "ohjaaja": {"id": "fincher", "kaytto": "inspired-by"},
    "tyyli": "asiallinen",
    "poimitut": [1],
    "lisaohje": "",
}
TAUSTA = {
    "loydokset": [
        {
            "vaite": "Artikla 50 alkaa 2.8.2026",
            "lahde": "https://ai-act-service-desk.ec.europa.eu/en/faq",
            "julkaistu": "2026-06-18",
            "merkitys": "Oikeuksien kysyminen muuttuu vaatimukseksi.",
        },
        {
            "vaite": "Suostumusinfrastruktuurista kirjoitetaan nyt",
            "lahde": "https://usercentrics.com/knowledge-hub/eu-ai-act-high-risk-delay-article-50-transparency-consent/",
            "julkaistu": "2026-08-01",
            "merkitys": "Aihe on jo liikkeessä.",
        },
    ],
    "vertailu": [
        {
            "kuka": "Usercentrics",
            "mita_tekee": "Myy suostumusinfrastruktuuria.",
            "lahde": "https://usercentrics.com/knowledge-hub/eu-ai-act-high-risk-delay-article-50-transparency-consent/",
        }
    ],
    "ajankohtaisuus": "Elokuun 2026 määräpäivä on lähellä.",
    "vastavaite": "Pieni käyttöliittymämuutos, kukaan ei vaihda palvelua sen takia.",
    "ei_loytynyt": "En löytänyt lukua keskeytyksistä GAII-tunnisteen osalta.",
}
OHJAAJAT = {
    "versio": 3,
    "kaytto": {
        "full": "Ohjaajan koko kieli.",
        "inspired-by": "Vain henki ja yksi ele.",
        "opposite-of": "Käännetään ylösalaisin.",
        "blend": "Kaksi tai kolme yhdessä.",
        "free-hand": "Vapaat kädet: saa poiketa, kerro missä poikkesit.",
    },
    "tyylit": [
        {"id": "asiallinen", "nimi": "Asiallinen", "kuvaus": "Sanoo mikä muuttui ja kenelle."},
        {"id": "lyhyt", "nimi": "Tiukka", "kuvaus": "Puolet lyhyempi kuin luulisi tarvitsevansa."},
        {"id": "numeroilla", "nimi": "Numeroilla", "kuvaus": "Rakentuu mitatun luvun ympärille."},
        {"id": "villi", "nimi": "Villi synteesi", "kuvaus": "Keksi jotain jota lähteissä ei ole."},
    ],
    "ohjaajat": [
        {
            "id": "fincher",
            "nimi": "David Fincher",
            "kuva": "Kaikki on juuri siinä missä pitää.",
            "rytmi": "Kylmä täsmällisyys.",
            "vari": "Vihertävä pimeys.",
            "aani": "Matala pohja.",
            "sopii": "Uskottavuuteen.",
            "ei_sovi": "Iloiseen.",
            "teksti": "Ei yhtään adjektiivia jota ei voi mitata.",
            "esimerkki": "Neljätoista päivää. Nyt luku on nolla.",
        },
        {
            "id": "gondry",
            "nimi": "Michel Gondry",
            "kuva": "Käsin tehty.",
            "rytmi": "Toisto joka kasvaa.",
            "vari": "Haalistunut.",
            "aani": "Kolisee.",
            "sopii": "Ilahduttavaan.",
            "ei_sovi": "Uhkaavaan.",
        },
    ],
}


# The dispatch every test run gets. RULE 2: the scope carries the run variable, so the address is
# BUILT from it — never generated. A test that passed a bare id would be testing the old defect.
RUN_ID = "2026-08-24"
TASK = {"id": "t-9", "scope": [{"name": "var.date", "type": "text", "value": RUN_ID}]}


class _StubLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts: list[str] = []

    def call(self, messages):
        self.prompts.append(messages[0]["content"])
        if not self.replies:
            raise AssertionError("the pipeline asked for more attempts than the test provided")
        return self.replies.pop(0)


def _piece(text: str, notes: str = "jätin pois päivämäärän") -> str:
    return f"<TEKSTI>\n{text}\n</TEKSTI>\n<HUOMIOT>\n{notes}\n</HUOMIOT>"


# ── the address: read from the dispatch, NEVER generated ─────────────────────────────────────────
# The defect these pin: with no key in the dispatch the agents generated one (p69c3e53, p6605be9,
# p55ff4e1 on three prod runs), wrote good work there, and the engine — looking at the key it knows —
# recorded the step as having produced nothing. Three runs done and thrown away.
def test_rule_1_the_named_key_wins_and_is_used_character_for_character():
    task = {
        "id": "t-1",
        "scope": [
            {"name": "deliverable_key", "type": "memory_key", "value": "julkaisu.2026-08-24.linkedin"},
            {"name": "var.date", "type": "text", "value": "2026-08-01"},
        ],
    }
    key, run_id, rule = jp.run_address(task, "linkedin")
    assert key == "julkaisu.2026-08-24.linkedin", "the named key is final — not rebuilt from a variable"
    assert run_id == "2026-08-24", "the run id is read back OUT of the named key, so the input matches"
    assert rule.startswith("saanto 1")


def test_rule_1_takes_a_named_key_even_in_a_shape_we_do_not_recognise():
    """Not our template, not our business: the engine named it, so it is written there verbatim."""
    task = {"scope": [{"name": "deliverable_key", "value": "kansi/2026-W34/li"}]}
    key, run_id, rule = jp.run_address(task, "linkedin")
    assert key == "kansi/2026-W34/li" and rule.startswith("saanto 1")
    assert run_id == jp.today_id(), "an unparseable key still needs an id for the INPUT side"


def test_rule_2_builds_the_key_from_the_runs_variables():
    for field, value in (("var.ref", "viikko34"), ("var.date", "2026-08-24")):
        key, run_id, rule = jp.run_address({"scope": [{"name": field, "value": value}]}, "x")
        assert key == f"julkaisu.{value}.x" and run_id == value
        assert rule.startswith("saanto 2")


def test_rule_2_also_reads_a_vars_object():
    key, _id, rule = jp.run_address(
        {"scope": {"vars": {"date": "2026-08-24"}}}, "video"
    )  # mapping shape still accepted
    assert key == "julkaisu.2026-08-24.video" and rule.startswith("saanto 2")


def test_rule_3_is_todays_date_and_nothing_else():
    key, run_id, rule = jp.run_address({"id": "t-1", "description": "kirjoita jotain"}, "aineisto")
    assert run_id == jp.today_id() and key == f"julkaisu.{jp.today_id()}.aineisto"
    assert rule.startswith("saanto 3")
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", run_id), "the id is a date, never a generated token"


def test_no_id_is_ever_generated():
    """The whole defect in one assertion: whatever the dispatch looks like, the id is either given or
    it is today's date. Nothing in this module may produce anything else."""
    for task in ({}, {"id": "t-1"}, {"scope": []}, {"scope": [{"name": "offer", "value": "x"}]}, None):
        _key, run_id, _rule = jp.run_address(task, "linkedin")
        assert run_id == jp.today_id()
    assert not hasattr(jp, "resolve_ref"), "the old ref-sniffing resolver must be gone, not deprecated"
    assert not hasattr(jp, "newest_aineisto_ref"), "guessing the newest aineisto is a guess"
    assert not hasattr(jd, "entry_ref"), "minting an id from the changelog entry is what broke prod"


def test_the_key_rule_is_in_the_prompt_of_every_writing_agent():
    """The code already makes an invented key impossible, but the rule is stated to the model too —
    the failure it prevents is one a model talks itself into when it cannot see a key."""
    from pathlib import Path

    crews = Path(__file__).resolve().parent.parent / "crews"
    for name in ("toimittaja", "linkedin", "x", "video", "kuva"):
        src = (crews / f"julkaisu_{name}_crew.py").read_text(encoding="utf-8")
        assert "KEY_RULE" in src and "KEY_RULE_BACKSTORY" in src, f"julkaisu-{name} does not carry the key rule"
        assert "THIS RUN: you read" in src, f"julkaisu-{name} does not name its own two keys"
    assert "deliverable_key" in jp.KEY_RULE and "TODAY'S DATE" in jp.KEY_RULE


def test_no_invented_example_id_is_shown_as_data_anywhere():
    """An example id is a specification. A published sample reading "ref": "p1a2b3c" is what taught
    five agents to make one up, so that shape must not appear as DATA — as a field value or inside a
    key. Prose naming the three ids from the incident is the opposite of the problem, and stays."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    as_data = re.compile(
        r"""["'](?:ref|id|deliverable_key)["']\s*:\s*["']p[0-9a-f]{6,}["']|julkaisu\.p[0-9a-f]{6,}\."""
    )
    offenders = []
    for path in [*(root / "crews").glob("julkaisu_*.py"), *(root / "src" / "crewaimeat").glob("julkaisu_*.py")]:
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if as_data.search(line):
                offenders.append(f"{path.name}:{i}: {line.strip()[:70]}")
    assert not offenders, f"invented example ids present as data: {offenders}"


# ── the editor's material is the input, and it is never invented ─────────────────────────────────
def test_a_missing_choice_fails_loud_and_writes_nothing(monkeypatch):
    writes: list = []
    monkeypatch.setattr(jp, "read_owner_key", lambda agent, key: None)
    monkeypatch.setattr(jp, "_aimeat_call", lambda a, tool, payload: writes.append(tool) or {"ok": True})
    out = jp.write_julkaisu("julkaisu-x", "x", TASK)
    assert out.startswith("FAILED") and "julkaisu.2026-08-24.valinta" in out
    assert "aimeat_memory_write" not in writes


def test_more_angles_is_not_permission_to_choose_one(monkeypatch):
    """The angle gate takes two answers, because the app has a "Lisaa kulmia" button. `lisaa` means
    the person has NOT decided — a writer that treated it as a green light would be choosing on their
    behalf, which is the exact thing v3 turned the chain around to stop."""
    monkeypatch.setattr(jp, "read_owner_key", lambda agent, key: {"vastaus": "lisaa", "lisaohje": "lisaa persoonia"})
    with pytest.raises(LookupError, match="lisaa"):
        jp.read_valinta("julkaisu-x", RUN_ID)


def test_a_choice_with_no_angle_is_not_a_brief(monkeypatch):
    monkeypatch.setattr(jp, "read_owner_key", lambda agent, key: {"vastaus": "valittu", "nro": 3})
    with pytest.raises(LookupError, match="no chosen angle"):
        jp.read_valinta("julkaisu-x", RUN_ID)


# ── the two writers get the same angle through a different door ─────────────────────────
def test_linkedin_opens_on_the_written_line_and_x_opens_on_the_tension():
    """The structural guarantee that the Finnish post and the English thread are two pieces rather
    than one text twice. 'Reads like a translation' is a judgement no check can make across two
    languages, so the divergence is built into what each writer is handed FIRST."""
    fi = jp.story_block(VALINTA, TAUSTA, OHJAAJAT, lead="avaus")
    en = jp.story_block(VALINTA, TAUSTA, OHJAAJAT, lead="riski")
    assert fi.index("AVAUS") < fi.index("RISKI"), "the LinkedIn writer sees the written opening first"
    assert en.index("RISKI") < en.index("AVAUS"), "the X writer sees the tension first"


def test_the_director_reaches_the_writing_not_only_the_video():
    """A Fincher LinkedIn post is not the same post as a Gondry one."""
    block = jp.story_block(VALINTA, TAUSTA, OHJAAJAT)
    assert "David Fincher" in block and "Kylmä täsmällisyys" in block
    assert "OHJAAJA KOSKEE MYÖS KIRJOITTAMISTA" in block
    assert "Asiallinen" in block, "the ordered style travels with the angle"


def test_the_research_and_its_sources_travel_with_the_angle():
    block = jp.story_block(VALINTA, TAUSTA, OHJAAJAT)
    assert ["https:/", "ai-act-service-desk.europa.eu"][0] in block or "ai-act-service-desk" in block
    assert "VASTAVÄITE" in block and "EI LÖYTYNYT" in block, "the writer must not claim what was not found"


def test_picked_angles_are_material_not_a_second_subject():
    kulmat = [{"nro": 1, "otsikko": "Vastaväite etunenässä", "kulma": "Pieni muutos, paitsi ettei ole."}]
    block = jp.story_block(VALINTA, TAUSTA, OHJAAJAT, kulmat)
    assert "POIMITUT KULMAT" in block and "Vastaväite etunenässä" in block
    assert "EIVÄT ole toinen aihe" in block


# ── the subscriber's OWN material reaches the writers ────────────────────────────────
# The defect these pin: `merkinnat` was read only when BUILDING the research queries, and `tuote`
# only at the grok step, AFTER the video writer. So a finished 60-second script came back as a new
# 12-scene story, and the writer invented pwademo.fi while `tilaus.tuote.osoite` said aimeat.io.
# The angle carried the CONTENT, which is why this looked fine until material had to survive
# word for word.
ORDER_WITH_MATERIAL = {
    **VALINTA,
    "merkinnat": [
        {
            "date": "2026-08-30",
            "title": "Valmis käsikirjoitus",
            "body": "0-3 s: ruudulla lukee KAHDEKSANTOISTA SEKUNTIA. 3-9 s: sama valikko kahdesti.",
        }
    ],
    "tuote": {"nimi": "AIMEAT", "osoite": "https://aimeat.io", "mika": "Agenttien substraatti."},
    "lisaohje": "Älä mainitse hinnoittelua.",
}


def test_the_subscribers_own_entries_reach_the_writer_and_the_order_of_the_brief_holds():
    """A finished script is the STARTING POINT, not background to write a new one from. It only
    reaches the writer if the brief carries it, and LISÄOHJE stays last because it is the boundary
    that beats the house defaults — material comes before the boundary, not after it."""
    block = jp.story_block(ORDER_WITH_MATERIAL, TAUSTA, OHJAAJAT)

    assert "KAHDEKSANTOISTA SEKUNTIA" in block, "the subscriber's own text must reach the writer"
    assert "Valmis käsikirjoitus" in block and "2026-08-30" in block
    assert "TILAAJAN OMA AINEISTO" in block
    assert "SANATARKASTI" in block, "the writer must be told this is kept, not rewritten"

    # nothing the brief already carried was displaced
    assert ANGLE["kulma"] in block and "Artikla 50 alkaa 2.8.2026" in block
    assert "David Fincher" in block and "LISÄOHJE TILAAJALTA" in block
    assert block.index("TILAAJAN OMA AINEISTO") < block.index("LISÄOHJE TILAAJALTA")
    assert block.rstrip().endswith("Älä mainitse hinnoittelua."), "LISÄOHJE is last"


def test_a_truncated_entry_says_so_rather_than_looking_short():
    """The researcher's 2500-character cut is sized for reading the gist. Handed to a writer it is
    the very defect being fixed here: a script cut mid-scene does not look broken, it looks short,
    and the model completes the ending itself."""
    long_body = "kohtaus. " * 3000
    order = {**ORDER_WITH_MATERIAL, "merkinnat": [{"date": "d", "title": "t", "body": long_body}]}
    block = jp.story_block(order, TAUSTA, OHJAAJAT)

    assert len(long_body) > 2500, "the fixture must exceed the researcher's own limit"
    assert block.count("kohtaus.") > 300, "a writer gets far more than the researcher's 2500 chars"
    assert "[aineisto katkaistu tähän]" in block, "a cut the model cannot see is a cut it writes over"


def test_the_ordered_product_and_its_address_reach_the_writer():
    """pwademo.fi again (2026-08-27): the writer invented an address because it was never given one.
    The nameserver check in the grok step is the second layer; this is the cause."""
    block = jp.story_block(ORDER_WITH_MATERIAL, TAUSTA, OHJAAJAT)

    assert "https://aimeat.io" in block and "AIMEAT" in block
    assert "Agenttien substraatti" in block
    assert "AINOA osoite" in block, "the address is a fact and the only one, not background colour"


def test_an_absent_product_leaves_no_empty_heading():
    """A heading with nothing under it reads as a field the writer failed to fill.

    Asserted on the rendered section rather than on the words: EI LÖYTYNYT names the material's
    heading on purpose, so a bare substring search finds that cross-reference instead."""
    order = {**VALINTA, "merkinnat": []}
    assert jp._subscriber_material(order) == "", "no material, no section"
    block = jp.story_block(order, TAUSTA, OHJAAJAT)
    assert "TILAAJAN TUOTE" not in block
    assert "TILAAJAN OMA AINEISTO —" not in block, "the heading itself, not the reference to it"


# ── house rules, enforced in code ────────────────────────────────────────────────────────────────
GOOD_LINKEDIN = (
    "Yhteys valmistuu nyt vasta kun olet päättänyt, mitä agentti saa tehdä. " * 4
    + "\n\n"
    + "Hyväksymisikkuna kysyy sen heti: pidä nykyiset oikeudet, vain luku, vakio tai täydet. " * 4
)


def test_linkedin_rules():
    assert 600 <= len(GOOD_LINKEDIN) <= 1200 and jp.check_linkedin(GOOD_LINKEDIN) == []
    assert any("merkkiä" in v for v in jp.check_linkedin("liian lyhyt"))
    assert any("aihetunniste" in v for v in jp.check_linkedin(GOOD_LINKEDIN + "\n#a #b #c"))
    assert any("innoissani" in v for v in jp.check_linkedin("Olen innoissani " + GOOD_LINKEDIN))
    assert any("kysymys" in v for v in jp.check_linkedin("Mitä jos yhteys olisi turvallinen?\n" + GOOD_LINKEDIN))


def test_an_excluded_topic_leaking_into_a_piece_is_caught():
    """Narrow by design: two NAMEABLE things (a capitalised name or a number) from the same excluded
    item, neither of which appears in the angle itself. A looser rule would block good prose for
    sharing an ordinary word, and a check that cries wolf gets switched off."""
    leaked = GOOD_LINKEDIN + "\n\nAgentin nimi on GAII, ja GAII on se identiteetti jonka alle Profiili kirjaa."
    assert any("poissuljettua" in v for v in jp.check_linkedin(leaked, AINEISTO))
    assert jp.check_linkedin(GOOD_LINKEDIN, AINEISTO) == [], "a clean post must not trip the leak check"


GOOD_X = "A connection that finishes before you decide is a decision made for you.\n\nThe window asks first.\n\nOne choice, once."


def test_x_rules():
    assert jp.check_x(GOOD_X) == []
    assert any("3 to 6" in v for v in jp.check_x("only one post"))
    assert any("280" in v for v in jp.check_x(GOOD_X.replace("The window asks first.", "x" * 300)))
    assert any("thread" in v for v in jp.check_x("🧵 here we go\n\nsecond\n\nthird"))
    assert any("emoji" in v for v in jp.check_x("A claim.\n\n👉 a bullet\n\nthird post."))
    assert any("engagement" in v for v in jp.check_x(GOOD_X.replace("One choice, once.", "Follow me for more.")))


# ── the video is a shot list, not prose with brackets glued on ───────────────────────────────────
def _shot(nro, kesto=5, kuvakoko="ruutukaappaus", kuvassa="hyväksymisikkuna auki", **kw):
    base = {
        "nro": nro,
        "kesto_s": kesto,
        "kuvakoko": kuvakoko,
        "kuvassa": kuvassa,
        "liike": "still",
        "puhe": "Yhteys ei ole valmis ennen kuin olet päättänyt.",
        "ruututeksti": "Päätä ennen yhdistämistä",
        "aani": "puhe",
    }
    base.update(kw)
    return base


def _script(n=11, **kw):
    shots = [_shot(i) for i in range(1, n + 1)]
    doc = {
        "kesto_s": sum(s["kesto_s"] for s in shots),
        "muoto": "9:16",
        "kohtaukset": shots,
        "kuvapyynnot": [],
        "text": "1 (5 s) …",
        "notes": "jätin pois tilirajauksen",
    }
    doc.update(kw)
    return doc


def test_a_real_shot_list_passes():
    assert jp.check_video(_script()) == []


def test_video_rules():
    assert any("kohtauksia" in v for v in jp.check_video(_script(n=3)))
    assert any("9:16" in v for v in jp.check_video(_script(muoto="16:9")))
    long_shot = _script()
    long_shot["kohtaukset"][2]["kesto_s"] = 9
    long_shot["kesto_s"] = sum(s["kesto_s"] for s in long_shot["kohtaukset"])
    assert any("yli 6 s" in v for v in jp.check_video(long_shot))
    stock = _script()
    stock["kohtaukset"][4]["kuvassa"] = "kuvituskuva kädet näppäimistöllä"
    assert any("kuvituskuvaohje" in v for v in jp.check_video(stock))
    logo = _script()
    logo["kohtaukset"][0]["kuvassa"] = "yrityksen logo ruudun keskellä"
    assert any("logo" in v for v in jp.check_video(logo))
    wordy = _script()
    wordy["kohtaukset"][1]["ruututeksti"] = "yksi kaksi kolme neljä viisi kuusi seitsemän"
    assert any("ruututeksti" in v for v in jp.check_video(wordy))
    short = _script(n=6)  # 6 shots x 5 s = 30 s, under the 45 s floor
    assert any("kokonaiskesto" in v for v in jp.check_video(short))
    mismatch = _script()
    mismatch["kesto_s"] = 99
    assert any("täsmätä" in v for v in jp.check_video(mismatch))
    bad_enum = _script()
    bad_enum["kohtaukset"][0]["liike"] = "dolly zoom"
    assert any("liike" in v for v in jp.check_video(bad_enum))


def test_an_image_is_never_requested_for_a_screen_recording():
    """kuvapyynnot exist for the shots a screen recording cannot cover. Asking for a generated image
    of a screen we can simply record spends money on a worse picture."""
    doc = _script(kuvapyynnot=[{"nro": 1, "prompt": "a laptop"}])
    assert any("ruutukaappaus" in v for v in jp.check_video(doc))
    doc2 = _script(kuvapyynnot=[{"nro": 99, "prompt": "a laptop"}])
    assert any("jota ei ole" in v for v in jp.check_video(doc2))
    doc3 = _script()
    doc3["kohtaukset"][3]["kuvakoko"] = "puolikuva"
    doc3["kuvapyynnot"] = [{"nro": 4, "prompt": "A person pausing mid-setup, vertical framing"}]
    assert jp.check_video(doc3) == []


def test_a_reply_that_is_not_json_is_not_stored_as_a_script():
    assert jp.parse_json_object("Sure! Here is your script: ...") is None
    assert jp.parse_json_object('```json\n{"a": 1}\n```')["a"] == 1


# ── the write: the exact key, the object shape, and the task's deliverable_key ───────────────────
@pytest.fixture
def stubbed(monkeypatch):
    writes: list[dict] = []

    def _read(agent, key):
        if key.endswith(".valinta"):
            return dict(VALINTA)
        if key.endswith(".tausta"):
            return dict(TAUSTA)
        if key.endswith(".kulmat"):
            return {"kulmat": []}
        return None

    monkeypatch.setattr(jp, "read_owner_key", _read)
    monkeypatch.setattr("crewaimeat.julkaisu_brief.read_owner_key", lambda agent, key: dict(OHJAAJAT))
    monkeypatch.setattr(
        jp, "_aimeat_call", lambda a, tool, payload: writes.append({"tool": tool, **payload}) or {"ok": True}
    )
    monkeypatch.setattr(jp, "resolved_model", lambda llm: "test-model")
    monkeypatch.setattr(jp, "resolved_provider", lambda: "test-provider")
    return writes


def test_the_piece_lands_at_the_runs_own_key(stubbed, monkeypatch):
    llm = _StubLLM([_piece(GOOD_LINKEDIN)])
    monkeypatch.setattr(jp, "get_llm", lambda **k: llm)
    recorded: list = []
    monkeypatch.setattr(jp, "record_deliverable_key", lambda tid, key: recorded.append((tid, key)))

    out = jp.write_julkaisu("julkaisu-linkedin", "linkedin", TASK, task_id="t-9")

    write = next(w for w in stubbed if w["tool"] == "aimeat_memory_write")
    assert write["key"] == "julkaisu.2026-08-24.linkedin"
    assert write["visibility"] == "owner"
    assert set(write["value"]) == {"text", "notes"} and write["value"]["text"].startswith("Yhteys")
    assert recorded == [("t-9", "julkaisu.2026-08-24.linkedin")], "the task must point at the piece, not the report"
    assert out.startswith("OK:") and "julkaisu.2026-08-24.linkedin" in out


def test_the_script_lands_as_a_structured_shot_list(stubbed, monkeypatch):
    import json

    llm = _StubLLM([json.dumps(_script(), ensure_ascii=False)])
    monkeypatch.setattr(jp, "get_llm", lambda **k: llm)
    monkeypatch.setattr(jp, "record_deliverable_key", lambda tid, key: None)

    out = jp.write_julkaisu("julkaisu-video", "video", TASK)

    write = next(w for w in stubbed if w["tool"] == "aimeat_memory_write")
    assert write["key"] == "julkaisu.2026-08-24.video"
    assert len(write["value"]["kohtaukset"]) == 11 and write["value"]["muoto"] == "9:16"
    assert "kohtausta" in out


def test_a_violation_is_handed_back_and_the_rewrite_is_what_lands(stubbed, monkeypatch):
    llm = _StubLLM([_piece("Olen innoissani! " + GOOD_LINKEDIN), _piece(GOOD_LINKEDIN)])
    monkeypatch.setattr(jp, "get_llm", lambda **k: llm)
    monkeypatch.setattr(jp, "record_deliverable_key", lambda tid, key: None)

    jp.write_julkaisu("julkaisu-linkedin", "linkedin", TASK)

    assert "innoissani" in llm.prompts[1], "the violation must be fed back into the rewrite"


# ── the language field decides the language; the free text does not ──────────────────────────────
# Kalle's order said "KAIKKI julkaistava teksti ENGLANNIKSI" in `lisaohje`, and the brief hands that
# over under a heading whose own words are "this beats the house defaults". It beat nothing:
# `languages_for` reads `kielet`, a field nothing told him about. Three languages in one package.
def _reading(monkeypatch, tilaus: dict, valinta: dict):
    def _read(agent, key):
        if key.endswith(".valinta"):
            return dict(valinta)
        if key.endswith(".tausta"):
            return dict(TAUSTA)
        if key.endswith(".kulmat"):
            return {"kulmat": []}
        if key.endswith(".tilaus"):
            return dict(tilaus)
        return None

    llm = _StubLLM([_piece(GOOD_LINKEDIN)] * 4)
    monkeypatch.setattr(jp, "read_owner_key", _read)
    monkeypatch.setattr(jp, "get_llm", lambda **k: llm)
    monkeypatch.setattr(jp, "record_deliverable_key", lambda tid, key: None)
    return llm


ASKS_ENGLISH = {**VALINTA, "lisaohje": "KAIKKI julkaistava teksti ENGLANNIKSI."}


def test_a_language_asked_for_in_the_free_text_is_said_out_loud(stubbed, monkeypatch, capsys):
    """The writer's own default decided, and the subscriber asked for something else in prose. That
    is not a language setting, so it is reported — with the field that would have worked."""
    _reading(monkeypatch, {}, ASKS_ENGLISH)
    jp.write_julkaisu("julkaisu-linkedin", "linkedin", TASK)

    err = capsys.readouterr().err
    assert "KIELIRISTIRIITA" in err
    assert "kielet" in err and "linkedin" in err, "the warning must name the field that decides"
    assert "[julkaisu-linkedin]" in err, "same shape as every other line this run prints"


def test_a_language_the_order_actually_set_is_not_warned_about(stubbed, monkeypatch, capsys):
    """The same free text, but the order named the language. The subscriber decided; a warning here
    would fire on every run until someone switched the check off."""
    _reading(monkeypatch, {"kielet": {"linkedin": "en"}}, ASKS_ENGLISH)
    jp.write_julkaisu("julkaisu-linkedin", "linkedin", TASK)

    assert "KIELIRISTIRIITA" not in capsys.readouterr().err


def test_the_free_text_never_silently_changes_the_language(stubbed, monkeypatch, capsys):
    """A quiet switch on a guessed reading is the same defect the other way round: warn, do not fix."""
    llm = _reading(monkeypatch, {}, ASKS_ENGLISH)
    jp.write_julkaisu("julkaisu-linkedin", "linkedin", TASK)

    assert "kielellä 'fi'" in llm.prompts[0], "the resolved language still governs the writing"
    assert "kielellä 'en'" not in llm.prompts[0], "prose in lisaohje must not switch the language"
    write = next(w for w in stubbed if w["tool"] == "aimeat_memory_write")
    assert not write["value"]["text"].lower().startswith("olen innoissani")


def test_a_piece_that_never_meets_the_rules_is_kept_with_them_recorded(stubbed, monkeypatch):
    """This asserted the opposite until 2026-08-26, and the opposite was wrong.

    Discarding meant a finished run vanished over a rule the person could have fixed in seconds —
    and since nothing here publishes, there is no danger being prevented. The piece is stored, the
    unmet rules travel with it, and the person decides at the gate."""
    llm = _StubLLM([_piece("liian lyhyt")] * jp._MAX_ATTEMPTS)
    monkeypatch.setattr(jp, "get_llm", lambda **k: llm)
    monkeypatch.setattr(jp, "record_deliverable_key", lambda tid, key: None)

    out = jp.write_julkaisu("julkaisu-linkedin", "linkedin", TASK)

    write = next(w for w in stubbed if w["tool"] == "aimeat_memory_write")
    assert write["value"]["text"] == "liian lyhyt"
    assert any("merkkiä" in v for v in write["value"]["rikkeet"])
    assert out.startswith("OK:") and "jäi täyttymättä" in out


# ── the images: a URL alone is not enough ────────────────────────────────────────────────────────
def test_images_are_recorded_with_their_storage_key(monkeypatch):
    writes: list[dict] = []
    script = _script()
    script["kohtaukset"][3]["kuvakoko"] = "puolikuva"
    script["kuvapyynnot"] = [{"nro": 4, "prompt": "A person pausing mid-setup"}]
    monkeypatch.setattr(jp, "read_owner_key", lambda agent, key: script)
    monkeypatch.setattr(
        jp, "_aimeat_call", lambda a, tool, payload: writes.append({"tool": tool, **payload}) or {"ok": True}
    )
    monkeypatch.setattr(jp, "record_deliverable_key", lambda tid, key: None)
    monkeypatch.setattr(
        "crewaimeat.seedream_gen.generate_image",
        lambda agent, prompt, **kw: {
            "ok": True,
            "url": "https://aimeat.io/v1/pub/g/images/a.png",
            "key": "images/a.png",
        },
    )

    out = jp.tee_kuvat("julkaisu-kuva", TASK)

    write = next(w for w in writes if w["tool"] == "aimeat_memory_write")
    assert write["key"] == "julkaisu.2026-08-24.kuvat"
    kuva = write["value"]["kuvat"][0]
    assert kuva["storage_key"] == "images/a.png" and kuva["url"].startswith("https://")
    assert kuva["nro"] == 4 and out.startswith("OK:")


def test_an_image_without_a_storage_key_is_not_usable(monkeypatch):
    """The app attaches by key. An upload that comes back with only a URL is not a usable image, and
    recording it as one would hand the app something it cannot attach."""
    script = _script()
    script["kohtaukset"][3]["kuvakoko"] = "puolikuva"
    script["kuvapyynnot"] = [{"nro": 4, "prompt": "A person pausing mid-setup"}]
    monkeypatch.setattr(jp, "read_owner_key", lambda agent, key: script)
    monkeypatch.setattr(jp, "_aimeat_call", lambda a, tool, payload: {"ok": True})
    monkeypatch.setattr(
        "crewaimeat.seedream_gen.generate_image",
        lambda agent, prompt, **kw: {"ok": True, "url": "https://aimeat.io/v1/pub/g/images/a.png"},
    )
    out = jp.tee_kuvat("julkaisu-kuva", TASK)
    assert out.startswith("FAILED") and "storage key" in out


def test_a_script_that_asks_for_no_images_is_not_a_failure_to_carry_out(monkeypatch):
    """Every shot a screen recording is a CORRECT script, so the image step has nothing to do — and
    a step with nothing to do is done, not broken. It used to raise, and the raise carried a message
    that said in its own words this was "not a failure to carry out"."""
    writes: list[dict] = []
    monkeypatch.setattr(jp, "read_owner_key", lambda agent, key: _script())
    monkeypatch.setattr(
        jp, "_aimeat_call", lambda a, tool, payload: writes.append({"tool": tool, **payload}) or {"ok": True}
    )
    monkeypatch.setattr(jp, "record_deliverable_key", lambda tid, key: None)

    out = jp.tee_kuvat("julkaisu-kuva", TASK)

    assert not out.startswith("FAILED"), out
    write = next(w for w in writes if w["tool"] == "aimeat_memory_write")
    assert write["key"] == "julkaisu.2026-08-24.kuvat"
    assert write["value"] == {"kuvat": []}, "the record has to exist, or the next step reads a hole"


def test_read_kuvapyynnot_returns_empty_and_still_raises_for_a_missing_script(monkeypatch):
    monkeypatch.setattr(jp, "read_owner_key", lambda agent, key: _script())
    assert jp.read_kuvapyynnot("julkaisu-kuva", "2026-08-24") == []

    monkeypatch.setattr(jp, "read_owner_key", lambda agent, key: None)
    with pytest.raises(LookupError) as exc:
        jp.read_kuvapyynnot("julkaisu-kuva", "2026-08-24")
    assert "missing" in str(exc.value), "a script that never ran is a different thing from one asking for nothing"


def test_every_request_failing_is_still_a_failure(monkeypatch):
    """Zero images out of zero requests is a finished run. Zero out of three is a broken one, and
    collapsing the two would turn a step that generated nothing green."""
    script = _script(kuvapyynnot=[{"nro": i, "prompt": f"p{i}"} for i in (2, 4, 6)])
    monkeypatch.setattr(jp, "read_owner_key", lambda agent, key: script)
    monkeypatch.setattr(jp, "_aimeat_call", lambda a, tool, payload: {"ok": True})
    monkeypatch.setattr(jp, "record_deliverable_key", lambda tid, key: None)
    monkeypatch.setattr(
        "crewaimeat.seedream_gen.generate_image", lambda agent, prompt, **kw: {"ok": False, "error": "429"}
    )

    out = jp.tee_kuvat("julkaisu-kuva", TASK)
    assert out.startswith("FAILED"), out


def test_a_partial_run_says_so_because_the_signal_no_longer_can(monkeypatch):
    """`exists` passes a run that asked for three images and made one. The check that used to live in
    the success signal moves into the step's own answer, with both numbers in it."""
    script = _script(kuvapyynnot=[{"nro": i, "prompt": f"p{i}"} for i in (2, 4, 6)])
    monkeypatch.setattr(jp, "read_owner_key", lambda agent, key: script)
    monkeypatch.setattr(jp, "_aimeat_call", lambda a, tool, payload: {"ok": True})
    monkeypatch.setattr(jp, "record_deliverable_key", lambda tid, key: None)
    calls = {"n": 0}

    def _gen(agent, prompt, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"ok": True, "url": "https://x/a.png", "key": "images/a.png"}
        return {"ok": False, "error": "429"}

    monkeypatch.setattr("crewaimeat.seedream_gen.generate_image", _gen)

    out = jp.tee_kuvat("julkaisu-kuva", TASK)
    assert "1/3" in out, "both numbers, so the reader knows which is which"
    assert "PARTIAL" in out and not out.startswith("FAILED"), out


def test_the_image_step_gates_on_the_script_not_on_the_request_count():
    """The step needs a SCRIPT to read. How many images that script asks for is the step's own
    business — as an entry condition it kept a correct run from ever starting."""
    from crewaimeat.offers import offers_doc_any

    offer = offers_doc_any("julkaisu-kuva", with_samples=False)["offers"][0]
    assert offer["required_to_function"]["op"] == "exists"
    assert offer["required_to_function"]["key"] == "julkaisu.{ref}.video"
    assert offer["success_signal"]["op"] == "exists"
    assert offer["success_signal"]["key"] == "julkaisu.{ref}.kuvat"


# ── a name the subscriber gave is not a name the writer invented ─────────────────────────────────
# The research failed to find the product on the open web, so its name and address landed in
# `ei_loytynyt` -> `ei_kerrota`, and the leak check — whose only allowed source was the one-sentence
# angle — treated the subscriber's own product name as a forbidden word. An order that named the
# product produced a piece that never named it.
def test_a_name_the_subscriber_gave_is_not_a_leak():
    brief = {
        "kulma": "Yhteys valmistuu vasta kun olet päättänyt.",
        "ei_kerrota": ["AIMEAT ei löytynyt hakukoneesta."],
        "oma_aineisto": "NIMI: AIMEAT\nOSOITE: https://aimeat.io",
    }
    assert jp.excluded_leak("AIMEAT tekee tämän näkyväksi.", brief) == []


def test_a_name_only_the_research_could_not_find_is_still_a_leak():
    """The narrow check still does its job: without the subscriber saying it, an unverified name in
    the piece was invented by the writer."""
    brief = {
        "kulma": "Yhteys valmistuu vasta kun olet päättänyt.",
        "ei_kerrota": ["AIMEAT ei löytynyt hakukoneesta."],
        "oma_aineisto": "",
    }
    assert jp.excluded_leak("AIMEAT tekee tämän näkyväksi.", brief) != []


def test_the_writers_brief_carries_the_subscribers_own_material(stubbed, monkeypatch):
    """The helper being right is not the fix; the brief has to actually carry the field. Without the
    wiring, `excluded_leak` reads an absent `oma_aineisto` and rejects the name all over again."""
    tausta = {**TAUSTA, "ei_loytynyt": "AIMEAT ei löytynyt hakukoneesta."}
    valinta = {**VALINTA, "tuote": {"nimi": "AIMEAT", "osoite": "https://aimeat.io"}}

    def _read(agent, key):
        if key.endswith(".valinta"):
            return dict(valinta)
        if key.endswith(".tausta"):
            return dict(tausta)
        if key.endswith(".kulmat"):
            return {"kulmat": []}
        return None

    monkeypatch.setattr(jp, "read_owner_key", _read)
    monkeypatch.setattr(jp, "record_deliverable_key", lambda tid, key: None)
    llm = _StubLLM([_piece(GOOD_LINKEDIN + " AIMEAT on tässä nimeltä.")])
    monkeypatch.setattr(jp, "get_llm", lambda **k: llm)

    out = jp.write_julkaisu("julkaisu-linkedin", "linkedin", TASK)

    assert "poissuljettua aihetta" not in out, "the subscriber's own product name is not a leak"
    assert out.startswith("OK:"), out
    assert len(llm.prompts) == 1, "no rewrite was demanded, so no attempt was spent on it"


def test_generate_image_returns_the_key_it_uploaded_under():
    """seedream_gen computed the storage key and threw it away, so every caller had to re-derive it
    or give up. The signature is the contract julkaisu-kuva depends on."""
    import inspect

    from crewaimeat import seedream_gen

    src = inspect.getsource(seedream_gen.generate_image)
    assert '"key": key' in src and '"gaii": gaii' in src


# ── the editor ───────────────────────────────────────────────────────────────────────────────────
ENTRIES = [
    {
        "date": "2026-08-24",
        "kind": "feature",
        "title": {"en": "Decide what your AI may do"},
        "body": {"en": "long body"},
    },
    {"date": "2026-08-20", "kind": "fix", "title": {"en": "Faster app publish"}, "body": {"en": "another body"}},
]


def test_the_changelog_must_actually_be_read(monkeypatch):
    """An agent that cannot read the changelog must stop, not write about one it never saw. That is
    the exact failure this desk exists to prevent."""

    class _Resp:
        status_code = 503
        encoding = "utf-8"

    monkeypatch.setattr(jd.requests, "get", lambda *a, **k: _Resp())
    with pytest.raises(RuntimeError, match="503"):
        jd.fetch_changelog()


def test_an_already_told_entry_is_not_offered_again():
    told = [{"paiva": "2026-08-24", "aihe": "  decide what your AI may do  "}]
    left = jd.untold_entries(ENTRIES, told)
    assert [e["date"] for e in left] == ["2026-08-20"], "matched case-folded and whitespace-collapsed"


def test_the_dig_contract_is_checked():
    ok = {k: AINEISTO[k] for k in ("kulma", "ennen", "nyt", "kenelle", "todiste", "varmuus")}
    ok["ei_kerrota"] = ["jotain muuta"]
    assert jd.check_aineisto(ok) == []
    assert any("'ennen'" in v for v in jd.check_aineisto({**ok, "ennen": ""}))
    assert any("kenelle" in v for v in jd.check_aineisto({**ok, "kenelle": "käyttäjät"}))
    assert any("ei_kerrota" in v for v in jd.check_aineisto({**ok, "ei_kerrota": []}))
    assert any("tiivistelmä" in v for v in jd.check_aineisto({**ok, "kulma": "x" * 260}))


def _dig_reply(**over):
    fields = {
        "KULMA": AINEISTO["kulma"],
        "ENNEN": AINEISTO["ennen"],
        "NYT": AINEISTO["nyt"],
        "KENELLE": AINEISTO["kenelle"],
        "TODISTE": AINEISTO["todiste"],
        "EI_KERROTA": "- agentin nimi identiteettinä",
        "VARMUUS": AINEISTO["varmuus"],
    }
    fields.update(over)
    return "\n".join(f"<{k}>{v}</{k}>" for k, v in fields.items())


def test_the_editor_writes_the_angle_and_remembers_the_entry(monkeypatch):
    writes: list[dict] = []
    monkeypatch.setattr(jd, "fetch_changelog", lambda *a, **k: list(ENTRIES))
    monkeypatch.setattr(jd, "fetch_llms_txt", lambda *a, **k: "node text")
    monkeypatch.setattr(jd, "read_owner_key", lambda agent, key: None)
    monkeypatch.setattr(
        jd, "_aimeat_call", lambda a, tool, payload: writes.append({"tool": tool, **payload}) or {"ok": True}
    )
    monkeypatch.setattr(jd, "resolved_model", lambda llm: "m")
    monkeypatch.setattr(jd, "resolved_provider", lambda: "p")
    monkeypatch.setattr(jd, "record_deliverable_key", lambda tid, key: None)
    monkeypatch.setattr(
        jd, "get_llm", lambda **k: _StubLLM(["<VALINTA>0</VALINTA><PERUSTELU>siksi</PERUSTELU>", _dig_reply()])
    )

    out = jd.valitse_aihe("julkaisu-toimittaja", task=TASK)

    aineisto = next(w for w in writes if w["key"] == "julkaisu.2026-08-24.aineisto")["value"]
    assert aineisto["valittu"] == "Decide what your AI may do", "the title is copied from the FEED, not retyped"
    assert aineisto["paiva"] == "2026-08-24" and aineisto["lahde"].endswith("#2026-08-24")
    assert aineisto["kulma"] == AINEISTO["kulma"] and aineisto["ei_kerrota"] == ["agentin nimi identiteettinä"]
    ledger = next(w for w in writes if w["key"] == jd.KERROTTU_KEY)["value"]["kerrottu"]
    assert ledger[0]["ref"] == RUN_ID and ledger[0]["aihe"] == "Decide what your AI may do"
    assert out.startswith("OK:")


def test_the_editor_stops_when_everything_is_already_told(monkeypatch):
    writes: list = []
    monkeypatch.setattr(jd, "fetch_changelog", lambda *a, **k: list(ENTRIES))
    monkeypatch.setattr(
        jd,
        "read_owner_key",
        lambda agent, key: {"kerrottu": [{"paiva": e["date"], "aihe": e["title"]["en"]} for e in ENTRIES]},
    )
    monkeypatch.setattr(jd, "_aimeat_call", lambda a, tool, payload: writes.append(tool) or {"ok": True})
    out = jd.valitse_aihe("julkaisu-toimittaja", task=TASK)
    assert out.startswith("FAILED") and "nothing left to tell" in out
    assert "aimeat_memory_write" not in writes


def test_a_dig_that_will_not_meet_the_contract_writes_nothing(monkeypatch):
    writes: list = []
    monkeypatch.setattr(jd, "fetch_changelog", lambda *a, **k: list(ENTRIES))
    monkeypatch.setattr(jd, "fetch_llms_txt", lambda *a, **k: "")
    monkeypatch.setattr(jd, "read_owner_key", lambda agent, key: None)
    monkeypatch.setattr(jd, "_aimeat_call", lambda a, tool, payload: writes.append(tool) or {"ok": True})
    monkeypatch.setattr(
        jd,
        "get_llm",
        lambda **k: _StubLLM(["<VALINTA>0</VALINTA>", *[_dig_reply(ENNEN="") for _ in range(jd._MAX_ATTEMPTS)]]),
    )
    out = jd.valitse_aihe("julkaisu-toimittaja", task=TASK)
    assert out.startswith("FAILED") and "'ennen'" in out
    assert "aimeat_memory_write" not in writes


# ── the measurer: never a number it did not read ─────────────────────────────────────────────────
def test_only_approved_gates_are_measured(monkeypatch):
    monkeypatch.setattr(
        jd,
        "_aimeat_call",
        lambda a, tool, payload: {
            "items": [
                {
                    "key": "julkaisu.a.portti",
                    "value": {"paatos": "hyvaksy", "julkaistu": ["linkedin"]},
                    "updated_at": "x",
                },
                {"key": "julkaisu.b.portti", "value": {"paatos": "hylkaa"}, "updated_at": "x"},
                {"key": "julkaisu.c.linkedin", "value": {"text": "t"}},
            ]
        },
    )
    refs = jd.published_refs("julkaisu-mittari")
    assert [r["ref"] for r in refs] == ["a"] and refs[0]["julkaistu"] == ["linkedin"]


def test_an_attempt_is_placed_by_what_it_names_not_by_a_guess():
    assert jd._attempt_ref({"target_key": "julkaisu.p1a2b3c.linkedin"}) == "p1a2b3c"
    assert jd._attempt_ref({"ref": "p9z8y7x"}) == "p9z8y7x"
    assert jd._attempt_ref({"id": "att-1", "status": "sent"}) is None, "an unplaceable attempt is reported, not guessed"


def test_metric_names_are_read_not_assumed():
    assert jd._numbers({"impressions": 1840, "clicks": 37}) == {"nayttokerrat": 1840, "klikit": 37}
    assert jd._numbers({"nayttokerrat": 5}) == {"nayttokerrat": 5, "klikit": 0}
    assert jd._numbers(None) == {"nayttokerrat": 0, "klikit": 0}


def test_nothing_due_is_a_normal_run(monkeypatch):
    monkeypatch.setattr(jd, "published_refs", lambda a: [])
    monkeypatch.setattr(jd, "read_kerrottu", lambda a: [])
    out = jd.mittaa_julkaisut("julkaisu-mittari")
    assert out.startswith("OK: nothing due")


def test_a_run_with_no_readable_metrics_is_left_unmeasured(monkeypatch):
    """A zero is a claim. A run whose numbers could not be read is reported as unmeasured, so the
    editor never treats 'we could not read it' as 'nobody looked at it'."""
    writes: list = []
    monkeypatch.setattr(jd, "published_refs", lambda a: [{"ref": "p1", "at": "", "julkaistu": ["linkedin"]}])
    monkeypatch.setattr(jd, "read_kerrottu", lambda a: [])
    monkeypatch.setattr(jd, "fetch_attempts", lambda a: [{"id": "att-1", "status": "sent"}])
    monkeypatch.setattr(jd, "_aimeat_call", lambda a, tool, payload: writes.append(tool) or {"ok": True})
    monkeypatch.setattr(jd, "get_llm", lambda **k: _StubLLM([]))
    out = jd.mittaa_julkaisut("julkaisu-mittari")
    assert "no readable metrics" in out and "p1" in out
    assert "named no ref" in out, "an unplaceable attempt record must be reported"
    assert "aimeat_memory_write" not in writes


def test_a_measured_run_folds_into_the_ledger_the_editor_reads(monkeypatch):
    writes: list[dict] = []
    monkeypatch.setattr(jd, "published_refs", lambda a: [{"ref": "p1", "at": "", "julkaistu": ["linkedin"]}])
    monkeypatch.setattr(jd, "read_kerrottu", lambda a: [{"ref": "p1", "aihe": "Decide what your AI may do"}])
    monkeypatch.setattr(jd, "fetch_attempts", lambda a: [{"id": "att-1", "channel": "linkedin", "ref": "p1"}])
    monkeypatch.setattr(jd, "attempt_metrics", lambda a, i: {"impressions": 1840, "clicks": 37})
    monkeypatch.setattr(
        jd, "_aimeat_call", lambda a, tool, payload: writes.append({"tool": tool, **payload}) or {"ok": True}
    )
    monkeypatch.setattr(jd, "record_deliverable_key", lambda tid, key: None)
    monkeypatch.setattr(
        jd, "get_llm", lambda **k: _StubLLM(["Yhdestä ajosta ei vielä voi päätellä kumpi avaus toimii."])
    )

    out = jd.mittaa_julkaisut("julkaisu-mittari")

    mittaus = next(w for w in writes if w["key"] == "julkaisu.p1.mittaus")["value"]
    assert mittaus["mittaus"]["linkedin"] == {"nayttokerrat": 1840, "klikit": 37}
    assert mittaus["opittu"].startswith("Yhdestä ajosta")
    ledger = next(w for w in writes if w["key"] == jd.KERROTTU_KEY)["value"]["kerrottu"]
    assert ledger[0]["ref"] == "p1" and ledger[0]["mittaus"]["linkedin"]["klikit"] == 37
    assert out.startswith("OK: measured 1 run(s)")


# ── the six offers the workflow reads ────────────────────────────────────────────────────────────
DESK_AGENTS = {
    "julkaisu-toimittaja": ("valitse-aihe", "julkaisu.{ref}.aineisto"),
    "julkaisu-linkedin": ("kirjoita-linkedin", "julkaisu.{ref}.linkedin"),
    "julkaisu-x": ("kirjoita-x", "julkaisu.{ref}.x"),
    "julkaisu-video": ("kirjoita-video", "julkaisu.{ref}.video"),
    "julkaisu-kuva": ("tee-kuvat", "julkaisu.{ref}.kuvat"),
    "julkaisu-mittari": ("mittaa-julkaisut", "julkaisu.kerrottu"),
}


def test_every_desk_agent_publishes_a_workflow_compatible_offer():
    from aimeat_crewai.workflow_spec import assess_offer

    from crewaimeat.offers import offers_doc_any

    for agent, (offer_id, key) in DESK_AGENTS.items():
        offer = offers_doc_any(agent, with_samples=False)["offers"][0]
        assert offer["id"] == offer_id
        assert assess_offer(offer)["workflow_compatible"], f"{agent}: {assess_offer(offer)['missing']}"
        assert offer["deliverable"]["location"]["key"] == key
        assert len(offer["ask"]) <= 500, f"{agent}: ask is {len(offer['ask'])} chars (cap is 500)"


def test_the_writers_wait_for_a_person():
    """Every writer's input gate points at the CHOICE a person made. Nobody starts writing until a
    human has picked an angle — that is what v3 turned the chain around to guarantee."""
    from crewaimeat.offers import offers_doc_any

    for agent in ("julkaisu-linkedin", "julkaisu-x", "julkaisu-video"):
        offer = offers_doc_any(agent, with_samples=False)["offers"][0]
        assert offer["required_to_function"]["key"] == "julkaisu.{ref}.valinta"
    researcher = offers_doc_any("julkaisu-tutkija", with_samples=False)["offers"][0]
    assert researcher["required_to_function"]["key"] == "julkaisu.{ref}.tilaus", "research waits for the order"


def test_the_video_signal_counts_scenes_not_files():
    """A record that exists is not a script. The signal counts shots inside it, so a two-shot stub
    reads as output-RED instead of done."""
    from crewaimeat.offers import offers_doc_any

    sig = offers_doc_any("julkaisu-video", with_samples=False)["offers"][0]["success_signal"]
    assert sig["op"] == "count_nonempty" and sig["path"] == "kohtaukset" and sig["min"] == 6


def test_an_offer_that_half_declares_its_signals_is_rejected():
    """The node rejects a workflow whose step names an offer missing any of the three. Catching it at
    the crew's boundary names the agent and the offer instead of failing later as a save rejection."""
    from crewaimeat.offers import crew_offer

    half = {
        "id": "kirjoita-puolikas",
        "title": "t",
        "ask": "I do x; I do not do y",
        "example": "e",
        "cost": "cheap",
        "latency": "minutes",
        "repeatability": "idempotent",
        "verification": "deterministic",
        "consequences": [],
        "success_signal": {"kind": "deterministic", "op": "nonempty", "key": "julkaisu.{ref}.linkedin"},
    }
    with pytest.raises(ValueError, match="required_to_function"):
        crew_offer("julkaisu-linkedin", half)


# ── KANSI: the research, and the check the whole step lives or dies by ───────────────────────────
import crewaimeat.julkaisu_brief as jb  # noqa: E402

ALLOWED = {
    "https://ai-act-service-desk.ec.europa.eu/en/faq",
    "https://usercentrics.com/knowledge-hub/eu-ai-act-high-risk-delay-article-50-transparency-consent/",
}


def test_a_source_that_was_never_read_is_refused():
    """The load-bearing check. A researcher that can invent a citation is worse than no researcher at
    all, so a `lahde` outside the pages the search actually returned is REFUSED, not trusted."""
    assert jb.check_tausta(dict(TAUSTA), ALLOWED) == []
    invented = {
        **TAUSTA,
        "loydokset": [{**TAUSTA["loydokset"][0], "lahde": "https://example.com/made-up"}, TAUSTA["loydokset"][1]],
    }
    assert any("EI ole niiden sivujen joukossa" in v for v in jb.check_tausta(invented, ALLOWED))


def test_an_empty_search_is_a_finding_and_must_be_said():
    bad = jb.check_tausta({**TAUSTA, "ei_loytynyt": ""}, ALLOWED)
    assert any("ei_loytynyt" in v for v in bad)


def test_the_counter_argument_is_not_optional():
    assert any("vastavaite" in v for v in jb.check_tausta({**TAUSTA, "vastavaite": ""}, ALLOWED))


def test_two_findings_is_the_floor():
    bad = jb.check_tausta({**TAUSTA, "loydokset": TAUSTA["loydokset"][:1]}, ALLOWED)
    assert any("2" in v for v in bad)


def test_the_researcher_writes_nothing_when_the_web_answers_nothing(monkeypatch):
    writes: list = []
    monkeypatch.setattr(
        jb, "read_owner_key", lambda agent, key: {"merkinnat": [{"date": "2026-08-24", "title": "T", "body": "B"}]}
    )
    monkeypatch.setattr(jb, "_aimeat_call", lambda a, tool, payload: writes.append(tool) or {"ok": True})
    monkeypatch.setattr(jb, "get_llm", lambda **k: _StubLLM(["query one\nquery two"]))
    monkeypatch.setattr(jb, "web_search", lambda *a, **k: [])
    out = jb.tutki_tausta("julkaisu-tutkija", task=TASK)
    assert out.startswith("FAILED") and "open web returned nothing" in out
    assert "aimeat_memory_write" not in writes


# ── KANSI: the angles a person chooses from ──────────────────────────────────────────────────────
def _angle(nro=1, prob=70, **kw):
    a = {
        "nro": nro,
        "otsikko": f"Kulma {nro}",
        "kulma": "Yksi lause.",
        "avaus": "Ensimmainen rivi.",
        "miksi_toimii": "Koska.",
        "kenelle": "kehittajat",
        "nojaa": "Artikla 50 alkaa 2.8.2026",
        "lahteet": ["https://ai-act-service-desk.ec.europa.eu/en/faq"],
        "todennakoisyys": prob,
        "perustelu": "Siksi.",
        "ohjaaja_ele": "inspired by David Fincher",
        "riski": "Voi kuulostaa pelottelulta.",
    }
    a.update(kw)
    return a


def test_a_real_angle_set_passes():
    assert jb.check_kulmat([_angle(1, 74), _angle(2, 41), _angle(3, 58)], 3, TAUSTA) == []


def test_a_row_of_near_identical_probabilities_is_refused():
    """A row of five 80s is a tell that nothing was judged — the spread is checked, not requested."""
    bad = jb.check_kulmat([_angle(1, 80), _angle(2, 82), _angle(3, 79)], 3, TAUSTA)
    assert any("hajonta" in v for v in bad)


def test_an_angle_must_rest_on_something_real():
    bad = jb.check_kulmat([_angle(1, 70, nojaa="jokin muu juttu"), _angle(2, 30)], 2, TAUSTA)
    assert any("nojaa" in v for v in bad)
    assert jb.check_kulmat([_angle(1, 70, nojaa="changelog"), _angle(2, 30)], 2, TAUSTA) == []


def test_every_angle_field_is_required():
    for field in ("avaus", "kenelle", "riski", "ohjaaja_ele"):
        bad = jb.check_kulmat([_angle(1, 70, **{field: ""}), _angle(2, 30)], 2, TAUSTA)
        assert any(field in v for v in bad), field


def test_two_angles_may_not_share_a_name():
    bad = jb.check_kulmat([_angle(1, 70), _angle(2, 30, otsikko="Kulma 1")], 2, TAUSTA)
    assert any("nimisia" in v or "nimisiä" in v for v in bad)


def test_more_angles_appends_and_numbering_continues(monkeypatch):
    """The person is reading the first batch in the app. Replacing it would delete what they were
    looking at, and re-using 1..n would rename the angles they already discussed."""
    import json as _json

    writes: list[dict] = []

    def _read(agent, key):
        if key.endswith(".tilaus"):
            return {
                "merkinnat": [{"date": "2026-08-24", "title": "T", "body": "B"}],
                "kulmia": 2,
                "ohjaaja": {"id": "fincher", "kaytto": "inspired-by"},
                "tyyli": "asiallinen",
            }
        if key.endswith(".tausta"):
            return dict(TAUSTA)
        if key == jb.OHJAAJAT_KEY:
            return dict(OHJAAJAT)
        if key.endswith(".kulmat"):
            return {"kulmat": [_angle(1, 74), _angle(2, 41)]}
        if key.endswith(".valinta"):
            return {"vastaus": "lisaa", "lisaohje": "kokeile toimitusjohtajan nakokulmaa"}
        return None

    monkeypatch.setattr(jb, "read_owner_key", _read)
    monkeypatch.setattr(
        jb, "_aimeat_call", lambda a, tool, payload: writes.append({"tool": tool, **payload}) or {"ok": True}
    )
    monkeypatch.setattr(jb, "resolved_model", lambda llm: "m")
    monkeypatch.setattr(jb, "resolved_provider", lambda: "p")
    monkeypatch.setattr(jb, "record_deliverable_key", lambda tid, key: None)
    llm = _StubLLM([_json.dumps({"kulmat": [_angle(1, 88), _angle(2, 35)], "notes": "n"}, ensure_ascii=False)])
    monkeypatch.setattr(jb, "get_llm", lambda **k: llm)

    out = jb.tee_kulmat("julkaisu-ohjaaja", task=TASK)

    value = next(w for w in writes if w["tool"] == "aimeat_memory_write")["value"]
    assert [a["nro"] for a in value["kulmat"]] == [1, 2, 3, 4], "numbering continues from the highest existing"
    assert len(value["kulmat"]) == 4, "the batch appended, it did not replace"
    assert "toimitusjohtajan" in llm.prompts[0], "the person's new instruction reached the prompt"
    assert "appended to 2 already offered" in out


# ── KANSI: the director comes from the node, never from a copy in this repo ──────────────────────
def test_the_director_block_is_rendered_from_the_nodes_list():
    block = jb.director_block(OHJAAJAT, {"id": "fincher", "kaytto": "inspired-by"})
    assert "David Fincher" in block and "inspired by" in block


def test_each_kaytto_changes_the_instruction():
    full = jb.director_block(OHJAAJAT, {"id": "fincher", "kaytto": "full"})
    opp = jb.director_block(OHJAAJAT, {"id": "fincher", "kaytto": "opposite-of"})
    blend = jb.director_block(OHJAAJAT, {"ids": ["fincher", "gondry"], "kaytto": "blend"})
    assert "kauttaaltaan" in full
    assert "Michel Gondry" in blend and "David Fincher" in blend
    assert "PAINOT RATKAISEVAT" in blend, "several directors must not be averaged into mush"
    assert opp != full and "full" not in opp


def test_an_unknown_director_is_surfaced_not_ignored():
    """An order naming a director the node does not carry is a mistake worth seeing — quietly writing
    in no style at all would hide it."""
    with pytest.raises(LookupError, match="kubrick"):
        jb.director_block(OHJAAJAT, {"id": "kubrick", "kaytto": "full"})


def test_the_directors_list_is_not_copied_into_this_repo():
    """The person adds directors to julkaisu.ohjaajat. A hardcoded list here would be stale the first
    time they did, so no director's name may appear as data in the module."""
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "src" / "crewaimeat" / "julkaisu_brief.py").read_text(
        encoding="utf-8"
    )
    for name in ("Cunningham", "Villeneuve", "Hype Williams", "Sigismondi"):
        assert name not in src, f"{name} is hardcoded — read julkaisu.ohjaajat instead"


def test_the_new_agents_carry_the_key_rule_too():
    from pathlib import Path

    crews = Path(__file__).resolve().parent.parent / "crews"
    for name in ("tutkija", "ohjaaja"):
        src = (crews / f"julkaisu_{name}_crew.py").read_text(encoding="utf-8")
        assert "KEY_RULE" in src and "THIS RUN: you read" in src


def test_the_kansi_chain_wires_end_to_end():
    """Every step's input gate is the previous step's output key. A break here is a workflow that
    saves and then never advances."""
    from crewaimeat.offers import offers_doc_any

    chain = [
        ("julkaisu-tutkija", "julkaisu.{ref}.tilaus", "julkaisu.{ref}.tausta"),
        ("julkaisu-ohjaaja", "julkaisu.{ref}.tausta", "julkaisu.{ref}.kulmat"),
        ("julkaisu-linkedin", "julkaisu.{ref}.valinta", "julkaisu.{ref}.linkedin"),
        ("julkaisu-x", "julkaisu.{ref}.valinta", "julkaisu.{ref}.x"),
        ("julkaisu-video", "julkaisu.{ref}.valinta", "julkaisu.{ref}.video"),
        ("julkaisu-kuva", "julkaisu.{ref}.video", "julkaisu.{ref}.kuvat"),
    ]
    for agent, want_in, want_out in chain:
        offer = offers_doc_any(agent, with_samples=False)["offers"][0]
        req = offer["required_to_function"]
        assert req["key"] == want_in, f"{agent} reads {req.get('key')}, expected {want_in}"
        assert offer["deliverable"]["location"]["key"] == want_out
        assert len(offer["ask"]) <= 500


# ── KANSI v3 update: several directors, several styles, several versions ─────────────────────────
def test_several_directors_at_once_with_their_weights():
    """`ohjaajat` is a LIST and that is the point. 70/30 must read as one carrying the work and the
    other cutting across it, not as both at half power."""
    block = jb.director_block(
        OHJAAJAT,
        [
            {"id": "fincher", "kaytto": "full", "paino": 70},
            {"id": "gondry", "kaytto": "opposite-of", "paino": 30},
        ],
    )
    assert "David Fincher" in block and "Michel Gondry" in block
    assert "OSUUS 70%" in block and "OSUUS 30%" in block
    assert block.index("OSUUS 70%") < block.index("OSUUS 30%"), "the heavier hand is stated first"
    assert "KESKIARVOISTA" in block and "ohjaaja_ele" in block


def test_free_hand_is_a_real_reading():
    block = jb.director_block(OHJAAJAT, [{"id": "fincher", "kaytto": "free-hand"}])
    assert "poikkesit" in block


def test_a_director_writes_prose_not_only_pictures():
    """`teksti` and `esimerkki` are how a director writes. Three of the four writers produce prose,
    so leaving those out was leaving out the part that applies to them."""
    block = jb.director_block(OHJAAJAT, [{"id": "fincher", "kaytto": "full"}])
    assert "TEKSTI (" in block and "adjektiivia" in block
    assert "esimerkkirivi:" in block


def test_styles_are_a_list_and_all_of_them_hold():
    block = jb.style_block(OHJAAJAT, ["lyhyt", "numeroilla"])
    assert "pidä KAIKKI" in block and "Tiukka" in block and "Numeroilla" in block
    assert "Asiallinen" in jb.style_block(OHJAAJAT, "asiallinen"), "a single style still works"


def test_an_inventive_style_changes_the_source_rule_rather_than_lifting_it():
    """`villi` and `spekulaatio` ask for an idea, not a defensible claim. Inventing is allowed when
    asked for; dressing an invention as a finding never is — so `lahteet` must stay empty."""
    assert jb.invention_ordered(["lyhyt", "villi"]) == ["villi"]
    assert jb.invention_ordered(["asiallinen"]) == []

    def angle(**kw):
        a = {
            "nro": kw.get("nro", 1),
            "otsikko": f"K{kw.get('nro', 1)}",
            "kulma": "x",
            "avaus": "y",
            "miksi_toimii": "z",
            "kenelle": "kehittajat",
            "nojaa": "changelog",
            "lahteet": [],
            "todennakoisyys": 60,
            "perustelu": "s",
            "ohjaaja_ele": "e",
            "riski": "r",
        }
        a.update(kw)
        return a

    wild = angle(lahteet=["https://ai-act-service-desk.ec.europa.eu/en/faq"])
    bad = jb.check_kulmat([wild, angle(nro=2, todennakoisyys=20)], 2, TAUSTA, ["villi"])
    assert any("TYHJ" in v for v in bad), "an invented claim may not carry a source that does not support it"
    assert jb.check_kulmat([angle(), angle(nro=2, todennakoisyys=20)], 2, TAUSTA, ["villi"]) == []


def test_an_angle_resting_on_research_must_show_its_sources():
    """The app puts `lahteet` on the card so a reader can CHECK the claim instead of trusting it."""

    def angle(nro, prob, **kw):
        a = {
            "nro": nro,
            "otsikko": f"K{nro}",
            "kulma": "x",
            "avaus": "y",
            "miksi_toimii": "z",
            "kenelle": "kehittajat",
            "nojaa": "Artikla 50 alkaa 2.8.2026",
            "lahteet": ["https://ai-act-service-desk.ec.europa.eu/en/faq"],
            "todennakoisyys": prob,
            "perustelu": "s",
            "ohjaaja_ele": "e",
            "riski": "r",
        }
        a.update(kw)
        return a

    assert jb.check_kulmat([angle(1, 70), angle(2, 30)], 2, TAUSTA) == []
    empty = jb.check_kulmat([angle(1, 70, lahteet=[]), angle(2, 30)], 2, TAUSTA)
    assert any("tyhjä" in v for v in empty)
    stray = jb.check_kulmat([angle(1, 70, lahteet=["https://example.com/invented"]), angle(2, 30)], 2, TAUSTA)
    assert any("ei ole taustan" in v for v in stray)
    # resting on the changelog alone is a legitimate and visible answer
    assert jb.check_kulmat([angle(1, 70, nojaa="changelog", lahteet=[]), angle(2, 30)], 2, TAUSTA) == []


# ── the writers: reach, language, versions ───────────────────────────────────────────────────────
def test_a_slot_outside_vaikuttaa_is_written_plainly():
    """A Fincher video beside an unadorned LinkedIn post is a normal order, not a mistake."""
    order = {**VALINTA, "vaikuttaa": ["video"], "ohjaajat": [{"id": "fincher", "kaytto": "full"}]}
    directed = jp.story_block(order, TAUSTA, OHJAAJAT, channel="video")
    plain = jp.story_block(order, TAUSTA, OHJAAJAT, channel="linkedin")
    assert "David Fincher" in directed
    assert "EI OHJAAJAA" in plain
    assert "TEKSTI (näin hän kirjoittaa)" not in plain, "no directorial voice reaches an undirected slot"
    assert jp.slot_is_directed(order, "video") and not jp.slot_is_directed(order, "linkedin")
    assert jp.slot_is_directed({}, "linkedin"), "an order naming no vaikuttaa directs everything"


def test_the_language_is_ordered_not_hardcoded():
    """LinkedIn-in-Finnish was a decision nobody made on purpose."""
    assert jp.languages_for({"kielet": {"linkedin": "en"}}, "linkedin", "fi") == ["en"]
    assert jp.languages_for({"kielet": {"x": "both"}}, "x", "en") == ["fi", "en"]
    assert jp.languages_for({}, "linkedin", "fi") == ["fi"], "no order means the channel default"


def test_several_versions_land_in_versiot_and_must_actually_differ(stubbed, monkeypatch):
    llm = _StubLLM([_piece(GOOD_LINKEDIN), _piece(GOOD_LINKEDIN.replace("Yhteys", "Ikkuna"))])
    monkeypatch.setattr(jp, "get_llm", lambda **k: llm)
    monkeypatch.setattr(jp, "record_deliverable_key", lambda tid, key: None)
    monkeypatch.setattr(
        jp,
        "read_owner_key",
        lambda a, key: (
            {"versioita": 2}
            if key.endswith(".tilaus")
            else (dict(VALINTA) if key.endswith(".valinta") else (dict(TAUSTA) if key.endswith(".tausta") else None))
        ),
    )

    out = jp.write_julkaisu("julkaisu-linkedin", "linkedin", TASK)

    value = next(w for w in stubbed if w["tool"] == "aimeat_memory_write")["value"]
    assert [v["nro"] for v in value["versiot"]] == [1, 2]
    assert all(v["kieli"] == "fi" for v in value["versiot"])
    assert "ERO" in llm.prompts[1] and "avausliike" in llm.prompts[1], "version 2 is told to differ, and how"
    assert "2 versio(ta)" in out


def test_one_version_keeps_the_old_flat_shape(stubbed, monkeypatch):
    """Every existing reader and every published signal is untouched by the versions feature."""
    monkeypatch.setattr(jp, "get_llm", lambda **k: _StubLLM([_piece(GOOD_LINKEDIN)]))
    monkeypatch.setattr(jp, "record_deliverable_key", lambda tid, key: None)

    jp.write_julkaisu("julkaisu-linkedin", "linkedin", TASK)

    value = next(w for w in stubbed if w["tool"] == "aimeat_memory_write")["value"]
    assert set(value) == {"text", "notes"} and "versiot" not in value


# ── the dispatch's REAL shape (verified against task 061bf2e3, 2026-08-25) ───────────────────────
# The node sends the scope as a LIST OF RECORDS. Reading it as a mapping is what made the tool
# announce "no key and no variables in the dispatch" while the dispatch was carrying both.
REAL_SCOPE = [
    {"name": "workflow-run", "type": "text", "value": "julkaisupoyta/1678fd09", "description": "tutkija"},
    {"name": "offer", "type": "text", "value": "tutki-tausta", "description": "Search the web for background"},
    {"name": "var.ref", "type": "text", "value": "2026-08-25-ujr7", "description": "workflow variable {ref}"},
    {"name": "var.run", "type": "text", "value": "1678fd09", "description": "workflow variable {run}"},
    {"name": "var.date", "type": "text", "value": "2026-08-25", "description": "workflow variable {date}"},
    {"name": "deliverable_key", "type": "memory_key", "value": "julkaisu.2026-08-25-ujr7.tausta"},
]


def test_the_scope_list_is_read_as_records_not_as_a_mapping():
    task = {"id": "061bf2e3", "scope": REAL_SCOPE}
    entries = jp.scope_entries(task)
    assert entries["deliverable_key"] == "julkaisu.2026-08-25-ujr7.tausta"
    assert jp.scope_vars(task) == {"ref": "2026-08-25-ujr7", "run": "1678fd09", "date": "2026-08-25"}


def test_an_id_that_is_not_todays_date_survives():
    """The whole point. While the tool could not read the scope, every run's id had to be today's
    date — so a second run on the same day overwrote the first, and the app had to refuse one."""
    key, run_id, rule = jp.run_address({"scope": REAL_SCOPE}, "tausta")
    assert key == "julkaisu.2026-08-25-ujr7.tausta"
    assert run_id == "2026-08-25-ujr7" != jp.today_id()
    assert rule.startswith("saanto 1")


def test_the_log_names_the_rule_that_actually_fired():
    """The old line said "no key and no variables in the dispatch" while both were present. That
    sentence cost an hour of diagnosis on the node side, so each rule now names itself."""
    _k, _i, r1 = jp.run_address({"scope": REAL_SCOPE}, "tausta")
    _k, _i, r2 = jp.run_address({"scope": [e for e in REAL_SCOPE if e["name"] != "deliverable_key"]}, "tausta")
    _k, _i, r3 = jp.run_address({"id": "t"}, "tausta")
    assert "deliverable_key scopesta" in r1
    assert "var.ref" in r2 and "rakennettu" in r2
    assert "paivamaara" in r3
    assert r1 != r2 != r3


def test_the_input_key_belongs_to_the_same_run_as_the_output():
    """`julkaisu.{ref}.tilaus`, never `julkaisu.<today>.tilaus` — the id is read back out of the
    named key so the read and the write cannot drift apart."""
    _key, run_id, _rule = jp.run_address({"scope": REAL_SCOPE}, "tausta")
    assert jp.TILAUS_KEY.format(ref=run_id) == "julkaisu.2026-08-25-ujr7.tilaus"


def test_a_new_channels_key_is_recognised_without_editing_a_list():
    """The id is read back out of the named key by matching its last segment. That list was
    hand-kept, and `grok` was missing within one agent of it being written — which does not fail
    loudly: the key stays right and the RUN ID silently becomes today, so the agent would have read
    yesterday's shot list while writing to the correct place. The suffixes are derived now."""
    for channel in ("grok", "video", "kuvat", "tausta", "kulmat", "valinta", "tilaus", "linkedin", "x"):
        key = f"julkaisu.2026-08-25-ujr7.{channel}"
        _k, run_id, rule = jp.run_address({"scope": [{"name": "deliverable_key", "value": key}]}, channel)
        assert run_id == "2026-08-25-ujr7", f"{channel}: the id came off the calendar, not out of the key"
        assert rule.startswith("saanto 1")
    assert jp._id_of_key("julkaisu.2026-08-25.jotain-muuta") is None, "an unknown shape stays unknown"


def test_the_completion_pointer_is_not_mistaken_for_the_target():
    """A finished task's top-level `deliverableKey` is what the agent REPORTED writing. Trusting it
    would make a re-dispatch chase its own tail, so only the scope entry counts."""
    task = {"deliverableKey": "julkaisu.vanha.tausta", "scope": [{"name": "var.ref", "value": "uusi"}]}
    key, run_id, rule = jp.run_address(task, "tausta")
    assert key == "julkaisu.uusi.tausta" and run_id == "uusi" and rule.startswith("saanto 2")


def test_every_owner_read_carries_owner_scope(monkeypatch):
    """Without the flag `aimeat_memory_read` sees only the CALLER's namespace, so an agent cannot
    read anything a person or an app wrote under the owner's own GHII. Measured: NOT_FOUND on a
    tilaus one minute after the app wrote it; the same key with the flag returns it in full."""
    import crewaimeat.memory_tools as mt

    calls: list = []
    monkeypatch.setattr(
        mt, "_aimeat_call", lambda agent, tool, payload: calls.append((tool, payload)) or {"value": {"ok": 1}}
    )
    mt.read_owner_key("julkaisu-tutkija", "julkaisu.2026-08-25.tilaus")
    tool, payload = calls[0]
    assert tool == "aimeat_memory_read"
    assert payload.get("owner_scope") is True, "an owner-written key is invisible without this flag"


# ── the word "changelog" in `lahteet` (prod, 2026-08-25 18:16) ───────────────────────────────────
def _ang(nro, prob, nojaa, lahteet):
    return {
        "nro": nro,
        "otsikko": f"K{nro}",
        "kulma": "a",
        "avaus": "b",
        "miksi_toimii": "c",
        "kenelle": "kehittajat",
        "nojaa": nojaa,
        "lahteet": lahteet,
        "todennakoisyys": prob,
        "perustelu": "e",
        "ohjaaja_ele": "f",
        "riski": "g",
    }


def test_the_word_changelog_in_lahteet_does_not_throw_a_run_away():
    """What actually happened: the director produced good angles, put the word "changelog" in
    `lahteet` instead of leaving it empty, and my check burned all three attempts and wrote nothing.
    The app then showed the director as OFFLINE — it had run, and finished, and been discarded.

    A bare word is not a citation; it is the model naming where the angle stands, in the wrong
    field. The intent is not in doubt, so it is tidied, not fatal."""
    angles = [
        _ang(1, 74, "Artikla 50 alkaa 2.8.2026", ["https://ai-act-service-desk.ec.europa.eu/en/faq"]),
        _ang(2, 45, "changelog", []),
        _ang(3, 30, "changelog", ["changelog"]),
    ]
    notes = jb.normalise_kulmat(angles)
    assert any("ei-URLin" in n for n in notes), "the tidy is reported, not silent"
    assert angles[2]["lahteet"] == [], "a non-URL token is dropped so the app never shows it as a source"
    assert jb.check_kulmat(angles, 3, TAUSTA) == []


def test_an_invented_url_is_still_refused():
    """The tidy loosens nothing that matters. A URL that no page in the research carries is an
    invented citation, and that is the whole reason this check exists."""
    angles = [_ang(1, 74, "Artikla 50 alkaa 2.8.2026", ["https://invented.example/x"]), _ang(2, 30, "changelog", [])]
    jb.normalise_kulmat(angles)
    bad = jb.check_kulmat(angles, 2, TAUSTA)
    assert any("ei ole taustan" in v for v in bad)


def test_the_prompt_says_where_changelog_goes():
    """The example used to hide the rule inside a string in the JSON sample, which is how the model
    put the word in the wrong field in the first place."""
    prompt = jb._kulmat_prompt(
        {"merkinnat": [{"date": "2026-08-25", "title": "T", "body": "B"}], "kulmia": 3},
        TAUSTA,
        OHJAAJAT,
        3,
        [],
        "",
    )
    assert "JÄTÄ lahteet TYHJÄKSI" in prompt
    assert "älä kirjoita sanaa 'changelog' lähteeksi" in prompt


# ── a house rule is a note for the person, not a reason to throw the work away ───────────────────
def test_a_too_long_post_is_stored_with_the_violation_recorded(stubbed, monkeypatch):
    """Nothing here publishes — a person reads the thread at the gate and decides. "post 3 is 305
    characters" is something they fix in five seconds; discarding the whole run instead is the worse
    outcome, and it happened three times before this."""
    long_thread = "A claim that stands alone.\n\n" + ("x" * 300) + "\n\nWhat you can do next."
    monkeypatch.setattr(jp, "get_llm", lambda **k: _StubLLM([_piece(long_thread)] * jp._MAX_ATTEMPTS))
    monkeypatch.setattr(jp, "record_deliverable_key", lambda tid, key: None)

    out = jp.write_julkaisu("julkaisu-x", "x", TASK)

    write = next(w for w in stubbed if w["tool"] == "aimeat_memory_write")
    assert write["key"] == "julkaisu.2026-08-24.x", "the piece is STORED, not discarded"
    assert any("280" in v for v in write["value"]["rikkeet"]), "the violation travels with the piece"
    assert "TALON SÄÄNNÖT EIVÄT TÄYTY" in write["value"]["notes"], "the gate shows it to the person"
    assert out.startswith("OK:") and "jäi täyttymättä" in out, "the report never hides a bent rule"


def test_a_clean_piece_carries_no_violations(stubbed, monkeypatch):
    monkeypatch.setattr(jp, "get_llm", lambda **k: _StubLLM([_piece(GOOD_X)]))
    monkeypatch.setattr(jp, "record_deliverable_key", lambda tid, key: None)
    out = jp.write_julkaisu("julkaisu-x", "x", TASK)
    value = next(w for w in stubbed if w["tool"] == "aimeat_memory_write")["value"]
    assert "rikkeet" not in value and "jäi täyttymättä" not in out


def test_a_piece_nobody_can_use_is_still_fatal(stubbed, monkeypatch):
    """The one class that stays fatal: no text at all. There is nothing for a person to fix."""
    monkeypatch.setattr(jp, "get_llm", lambda **k: _StubLLM(["ei mitään lohkoa"] * jp._MAX_ATTEMPTS))
    out = jp.write_julkaisu("julkaisu-x", "x", TASK)
    assert out.startswith("FAILED") and "unusable" in out
    assert not [w for w in stubbed if w["tool"] == "aimeat_memory_write"]


def test_a_script_with_no_scenes_is_fatal_because_the_image_agent_reads_them(stubbed, monkeypatch):
    import json as _json

    empty = {"kesto_s": 0, "muoto": "9:16", "kohtaukset": [], "kuvapyynnot": [], "text": "t", "notes": "n"}
    monkeypatch.setattr(jp, "get_llm", lambda **k: _StubLLM([_json.dumps(empty)] * jp._MAX_ATTEMPTS))
    out = jp.write_julkaisu("julkaisu-video", "video", TASK)
    assert out.startswith("FAILED")
    assert not [w for w in stubbed if w["tool"] == "aimeat_memory_write"]


def test_a_short_script_is_kept_with_its_violations(stubbed, monkeypatch):
    """Six scenes instead of eight is a note, not a discard — the person can shoot it or ask again."""
    import json as _json

    short = _script(n=6)  # 30 s, under the 45 s floor
    monkeypatch.setattr(
        jp, "get_llm", lambda **k: _StubLLM([_json.dumps(short, ensure_ascii=False)] * jp._MAX_ATTEMPTS)
    )
    monkeypatch.setattr(jp, "record_deliverable_key", lambda tid, key: None)
    out = jp.write_julkaisu("julkaisu-video", "video", TASK)
    value = next(w for w in stubbed if w["tool"] == "aimeat_memory_write")["value"]
    assert len(value["kohtaukset"]) == 6 and value["rikkeet"]
    assert out.startswith("OK:")

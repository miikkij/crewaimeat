"""JULKAISUPÖYTÄ floor — the parts that must never depend on a model.

Which key each run writes, that the editor's material is required, that the house rules are actually
enforced before anything is stored, and that a number is never invented. No LLM and no network: the
write paths run with a stubbed ``_aimeat_call`` and a stubbed LLM, so every assertion is about the
pipeline's decisions rather than a model's prose.
"""

from __future__ import annotations

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


# ── the run's ref: resolved in code, never guessed ───────────────────────────────────────────────
def test_ref_comes_from_the_task_params():
    task = {"id": "t-1", "description": "kirjoita postaus", "params": {"ref": "viikko34"}}
    assert jp.resolve_ref(task, task["description"]) == "viikko34"


def test_ref_comes_from_the_aineisto_key_named_in_the_prompt():
    prompt = "Kirjoita LinkedIn-postaus avaimesta julkaisu.p1a2b3c.aineisto"
    assert jp.resolve_ref({"id": "t-1", "description": prompt}, prompt) == "p1a2b3c"


def test_no_ref_is_no_ref():
    """A run with no ref must NOT fall back to a default: writing julkaisu.<something>.linkedin on a
    guess would overwrite another run's piece. The absence has to travel to the caller."""
    assert jp.resolve_ref({"id": "t-1", "description": "kirjoita jotain"}, "kirjoita jotain") is None


def _listing(rows):
    return lambda a, tool, payload: {"items": [{"key": k, "updated_at": t} for k, t in rows]}


def test_the_fallback_takes_the_newest_aineisto(monkeypatch):
    monkeypatch.setattr(
        jp,
        "_aimeat_call",
        _listing(
            [
                ("julkaisu.vanha.aineisto", "2026-08-20T10:00:00Z"),
                ("julkaisu.uusi.aineisto", "2026-08-24T16:44:40Z"),
                ("julkaisu.uusi.linkedin", "2026-08-24T16:45:00Z"),  # not an aineisto — not a candidate
            ]
        ),
    )
    ref, why = jp.newest_aineisto_ref("julkaisu-linkedin")
    assert ref == "uusi" and "most recently written" in why


def test_the_fallback_refuses_a_tie(monkeypatch):
    """Two runs prepared in the same instant cannot be told apart, and picking one would overwrite
    the other run's piece. The step stops and says which ones collided."""
    same = "2026-08-24T16:44:40Z"
    monkeypatch.setattr(jp, "_aimeat_call", _listing([("julkaisu.a.aineisto", same), ("julkaisu.b.aineisto", same)]))
    ref, why = jp.newest_aineisto_ref("julkaisu-linkedin")
    assert ref is None and "a, b" in why


def test_a_run_with_no_ref_and_no_aineisto_writes_nothing(monkeypatch):
    calls: list = []
    monkeypatch.setattr(jp, "_aimeat_call", lambda a, tool, payload: calls.append(tool) or {"items": []})
    out = jp.write_julkaisu("julkaisu-linkedin", "linkedin", None)
    assert out.startswith("FAILED") and "editor has not run" in out
    assert "aimeat_memory_write" not in calls


# ── the editor's material is the input, and it is never invented ─────────────────────────────────
def test_a_missing_aineisto_fails_loud_and_writes_nothing(monkeypatch):
    writes: list = []
    monkeypatch.setattr(jp, "read_owner_key", lambda agent, key: None)
    monkeypatch.setattr(jp, "_aimeat_call", lambda a, tool, payload: writes.append(tool) or {"ok": True})
    out = jp.write_julkaisu("julkaisu-x", "x", "demo1")
    assert out.startswith("FAILED") and "julkaisu.demo1.aineisto" in out
    assert "aimeat_memory_write" not in writes


def test_a_half_written_aineisto_is_not_a_usable_angle(monkeypatch):
    """An aineisto with no `ennen` is the failure the editor exists to prevent. A writer must not
    quietly fill that in — that is exactly the invention this desk was rebuilt to stop."""
    monkeypatch.setattr(jp, "read_owner_key", lambda agent, key: {"kulma": "x", "nyt": "y"})
    with pytest.raises(LookupError, match="ennen"):
        jp.read_aineisto("julkaisu-x", "demo1")


# ── the two writers get the same facts through a different door ──────────────────────────────────
def test_linkedin_leads_on_the_fix_and_x_leads_on_the_before():
    """The one structural guarantee that the Finnish post and the English thread are two pieces
    rather than one text twice. 'Reads like a translation' is a judgement no check can make across
    two languages, so the divergence is built into what each writer is handed FIRST."""
    fi = jp.story_block(AINEISTO, lead="nyt")
    en = jp.story_block(AINEISTO, lead="ennen")
    assert fi.index("NYT") < fi.index("ENNEN"), "the LinkedIn writer sees the fix first"
    assert en.index("ENNEN") < en.index("NYT"), "the X writer sees the frustration first"
    assert "EI KERROTA" in fi and "GAII" in fi, "what the editor ruled out travels with the angle"


def test_the_entry_title_is_not_handed_to_the_writers():
    """`valittu` is the changelog headline. A writer given it restates the entry, which is the habit
    the editor was added to end."""
    assert AINEISTO["valittu"] not in jp.story_block(AINEISTO)


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
    monkeypatch.setattr(jp, "read_owner_key", lambda agent, key: dict(AINEISTO))
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

    out = jp.write_julkaisu("julkaisu-linkedin", "linkedin", "demo1", task_id="t-9")

    write = next(w for w in stubbed if w["tool"] == "aimeat_memory_write")
    assert write["key"] == "julkaisu.demo1.linkedin"
    assert write["visibility"] == "owner"
    assert set(write["value"]) == {"text", "notes"} and write["value"]["text"].startswith("Yhteys")
    assert recorded == [("t-9", "julkaisu.demo1.linkedin")], "the task must point at the piece, not the report"
    assert out.startswith("OK:") and "julkaisu.demo1.linkedin" in out


def test_the_script_lands_as_a_structured_shot_list(stubbed, monkeypatch):
    import json

    llm = _StubLLM([json.dumps(_script(), ensure_ascii=False)])
    monkeypatch.setattr(jp, "get_llm", lambda **k: llm)
    monkeypatch.setattr(jp, "record_deliverable_key", lambda tid, key: None)

    out = jp.write_julkaisu("julkaisu-video", "video", "demo1")

    write = next(w for w in stubbed if w["tool"] == "aimeat_memory_write")
    assert write["key"] == "julkaisu.demo1.video"
    assert len(write["value"]["kohtaukset"]) == 11 and write["value"]["muoto"] == "9:16"
    assert "kohtausta" in out


def test_a_violation_is_handed_back_and_the_rewrite_is_what_lands(stubbed, monkeypatch):
    llm = _StubLLM([_piece("Olen innoissani! " + GOOD_LINKEDIN), _piece(GOOD_LINKEDIN)])
    monkeypatch.setattr(jp, "get_llm", lambda **k: llm)
    monkeypatch.setattr(jp, "record_deliverable_key", lambda tid, key: None)

    jp.write_julkaisu("julkaisu-linkedin", "linkedin", "demo1")

    assert "innoissani" in llm.prompts[1], "the violation must be fed back into the rewrite"
    write = next(w for w in stubbed if w["tool"] == "aimeat_memory_write")
    assert not write["value"]["text"].lower().startswith("olen innoissani")


def test_a_piece_that_never_meets_the_rules_is_not_written(stubbed, monkeypatch):
    llm = _StubLLM([_piece("liian lyhyt")] * jp._MAX_ATTEMPTS)
    monkeypatch.setattr(jp, "get_llm", lambda **k: llm)

    out = jp.write_julkaisu("julkaisu-linkedin", "linkedin", "demo1")

    assert out.startswith("FAILED") and "merkkiä" in out
    assert not [w for w in stubbed if w["tool"] == "aimeat_memory_write"], (
        "a piece that breaks the house rules must not be stored — the step goes output-RED with a reason"
    )


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

    out = jp.tee_kuvat("julkaisu-kuva", "demo1")

    write = next(w for w in writes if w["tool"] == "aimeat_memory_write")
    assert write["key"] == "julkaisu.demo1.kuvat"
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
    out = jp.tee_kuvat("julkaisu-kuva", "demo1")
    assert out.startswith("FAILED") and "storage key" in out


def test_a_script_that_asks_for_no_images_is_not_a_failure_to_carry_out(monkeypatch):
    monkeypatch.setattr(jp, "read_owner_key", lambda agent, key: _script())
    monkeypatch.setattr(jp, "_aimeat_call", lambda a, tool, payload: {"ok": True})
    out = jp.tee_kuvat("julkaisu-kuva", "demo1")
    assert "asks for no images" in out and "script's decision" in out


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


def test_a_minted_ref_is_stable_for_the_same_entry():
    """Same entry -> same ref, so a re-run rewrites its own keys instead of scattering half-finished
    runs across the namespace."""
    assert jd.entry_ref(ENTRIES[0]) == jd.entry_ref(dict(ENTRIES[0]))
    assert jd.entry_ref(ENTRIES[0]) != jd.entry_ref(ENTRIES[1])
    assert jd.entry_ref(ENTRIES[0]).startswith("p") and len(jd.entry_ref(ENTRIES[0])) == 8


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

    out = jd.valitse_aihe("julkaisu-toimittaja", ref="demo1")

    aineisto = next(w for w in writes if w["key"] == "julkaisu.demo1.aineisto")["value"]
    assert aineisto["valittu"] == "Decide what your AI may do", "the title is copied from the FEED, not retyped"
    assert aineisto["paiva"] == "2026-08-24" and aineisto["lahde"].endswith("#2026-08-24")
    assert aineisto["kulma"] == AINEISTO["kulma"] and aineisto["ei_kerrota"] == ["agentin nimi identiteettinä"]
    ledger = next(w for w in writes if w["key"] == jd.KERROTTU_KEY)["value"]["kerrottu"]
    assert ledger[0]["ref"] == "demo1" and ledger[0]["aihe"] == "Decide what your AI may do"
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
    out = jd.valitse_aihe("julkaisu-toimittaja", ref="demo1")
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
    out = jd.valitse_aihe("julkaisu-toimittaja", ref="demo1")
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


def test_the_writers_wait_for_the_editor():
    """Every writer's input gate points at the editor's material — nobody starts from a summary
    somebody else pre-wrote, which is the whole point of adding the editor."""
    from crewaimeat.offers import offers_doc_any

    for agent in ("julkaisu-linkedin", "julkaisu-x", "julkaisu-video"):
        offer = offers_doc_any(agent, with_samples=False)["offers"][0]
        assert offer["required_to_function"]["key"] == "julkaisu.{ref}.aineisto"
    editor = offers_doc_any("julkaisu-toimittaja", with_samples=False)["offers"][0]
    assert editor["required_to_function"] == "none", "the editor fetches its own input"


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

"""JULKAISUPÖYTÄ floor — the parts that must never depend on a model: which key the run writes, that
the brief is required, and that the house rules are actually enforced before anything is stored.

No LLM, no network: the write path is exercised with a stubbed ``_aimeat_call`` and a stubbed LLM, so
the assertions are about the pipeline's decisions, never about a model's prose.
"""

from __future__ import annotations

import pytest

import crewaimeat.julkaisu_pipeline as jp


# ── the run's ref: resolved in code, never guessed ───────────────────────────────────────────────
def test_ref_comes_from_the_task_params():
    task = {"id": "t-1", "description": "kirjoita postaus", "params": {"ref": "viikko34"}}
    assert jp.resolve_ref(task, task["description"]) == "viikko34"


def test_ref_comes_from_the_brief_key_named_in_the_prompt():
    prompt = "Kirjoita LinkedIn-postaus avaimesta julkaisu.demo1.brief"
    assert jp.resolve_ref({"id": "t-1", "description": prompt}, prompt) == "demo1"


def test_ref_comes_from_a_bare_ref_field_in_the_text():
    prompt = "Julkaisupöytä, ref: kansi-2026-08"
    assert jp.resolve_ref({}, prompt) == "kansi-2026-08"


def test_no_ref_is_no_ref():
    """A run with no ref must NOT fall back to a default: writing julkaisu.<something>.linkedin on a
    guess would overwrite another run's piece. The absence has to travel to the caller."""
    assert jp.resolve_ref({"id": "t-1", "description": "kirjoita jotain"}, "kirjoita jotain") is None


def test_a_run_without_a_ref_and_without_briefs_writes_nothing(monkeypatch):
    calls: list = []
    monkeypatch.setattr(jp, "_aimeat_call", lambda a, tool, payload: calls.append(tool) or {"items": []})
    out = jp.write_julkaisu("julkaisu-linkedin", "linkedin", "")
    assert out.startswith("FAILED") and "no ref" in out
    assert "aimeat_memory_write" not in calls, "nothing may be written when no ref could be resolved"


def _briefs(rows):
    return lambda a, tool, payload: {"items": [{"key": k, "updated_at": t} for k, t in rows]}


def test_a_dispatch_without_a_ref_falls_back_to_the_only_brief(monkeypatch):
    monkeypatch.setattr(jp, "_aimeat_call", _briefs([("julkaisu.demo1.brief", "2026-08-24T16:44:40Z")]))
    ref, why = jp._newest_brief_ref("julkaisu-linkedin")
    assert ref == "demo1" and "only brief" in why


def test_the_fallback_takes_the_newest_brief(monkeypatch):
    monkeypatch.setattr(
        jp,
        "_aimeat_call",
        _briefs(
            [
                ("julkaisu.vanha.brief", "2026-08-20T10:00:00Z"),
                ("julkaisu.uusi.brief", "2026-08-24T16:44:40Z"),
                ("julkaisu.uusi.linkedin", "2026-08-24T16:45:00Z"),  # not a brief — must not be a candidate
            ]
        ),
    )
    ref, why = jp._newest_brief_ref("julkaisu-linkedin")
    assert ref == "uusi" and "most recently written" in why


def test_the_fallback_refuses_a_tie(monkeypatch):
    """Two briefs written in the same instant cannot be told apart, and picking one would overwrite
    the other run's piece. The run stops and says which ones collided."""
    same = "2026-08-24T16:44:40Z"
    monkeypatch.setattr(jp, "_aimeat_call", _briefs([("julkaisu.a.brief", same), ("julkaisu.b.brief", same)]))
    ref, why = jp._newest_brief_ref("julkaisu-linkedin")
    assert ref is None and "a, b" in why


def test_an_inferred_ref_is_written_into_the_report(monkeypatch):
    writes: list[dict] = []

    def _call(agent, tool, payload):
        if tool == "aimeat_memory_list":
            return {"items": [{"key": "julkaisu.demo1.brief", "updated_at": "2026-08-24T16:44:40Z"}]}
        writes.append({"tool": tool, **payload})
        return {"ok": True}

    monkeypatch.setattr(jp, "_aimeat_call", _call)
    monkeypatch.setattr(jp, "read_owner_key", lambda agent, key: {"aihe": "Hyväksymisikkuna"})
    monkeypatch.setattr(jp, "get_llm", lambda **k: _StubLLM([_piece(GOOD_LINKEDIN)]))
    monkeypatch.setattr(jp, "resolved_model", lambda llm: "m")
    monkeypatch.setattr(jp, "resolved_provider", lambda: "p")
    monkeypatch.setattr(jp, "record_deliverable_key", lambda tid, key: None)

    out = jp.write_julkaisu("julkaisu-linkedin", "linkedin", "")

    assert "was not in the dispatch" in out and "demo1" in out, "an inferred ref must be visible, not quiet"
    assert next(w for w in writes if w["tool"] == "aimeat_memory_write")["key"] == "julkaisu.demo1.linkedin"


# ── the brief is the input, and it is never invented ─────────────────────────────────────────────
def test_a_missing_brief_fails_loud_and_writes_nothing(monkeypatch):
    writes: list = []
    monkeypatch.setattr(jp, "read_owner_key", lambda agent, key: None)
    monkeypatch.setattr(jp, "_aimeat_call", lambda a, tool, payload: writes.append(tool) or {"ok": True})
    out = jp.write_julkaisu("julkaisu-x", "x", "demo1")
    assert out.startswith("FAILED") and "julkaisu.demo1.brief" in out
    assert "aimeat_memory_write" not in writes


def test_a_brief_stored_as_json_text_still_reads(monkeypatch):
    monkeypatch.setattr(jp, "read_owner_key", lambda agent, key: '{"aihe": "Uusi hyväksymisikkuna"}')
    assert jp.read_brief("julkaisu-x", "demo1")["aihe"] == "Uusi hyväksymisikkuna"


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


GOOD_X = "A connection that finishes before you decide is a decision made for you.\n\nThe window asks first.\n\nOne choice, once."


def test_x_rules():
    assert jp.check_x(GOOD_X) == []
    assert any("3 to 6" in v for v in jp.check_x("only one post"))
    assert any("280" in v for v in jp.check_x(GOOD_X.replace("The window asks first.", "x" * 300)))
    assert any("thread" in v for v in jp.check_x("🧵 here we go\n\nsecond\n\nthird"))
    assert any("emoji" in v for v in jp.check_x("A claim.\n\n👉 a bullet\n\nthird post."))
    assert any("engagement" in v for v in jp.check_x(GOOD_X.replace("One choice, once.", "Follow me for more.")))


GOOD_VIDEO = (
    "Yhteys ei ole valmis ennen kuin olet päättänyt mitä tekoälysi saa tehdä sinun datallasi. "
    "[ruutu: hyväksymisikkuna auki, oikeusvalinnat näkyvissä]\n"
    "Ikkuna kysyy sen heti kun yhdistät palvelun tiliisi: pidä nykyiset, vain luku, vakio tai "
    "täydet oikeudet. [ruutu: kursori liikkuu vaihtoehtojen yli ja pysähtyy vakioon]\n"
    "Voit myös rastittaa itse ne oikeudet jotka haluat antaa, yksi kerrallaan ja ilman kiirettä, "
    "ennen kuin mitään on tapahtunut. [ruutu: rastit menevät päälle yksi kerrallaan]\n"
    "Ennen yhteys syntyi niillä oikeuksilla jotka agentilla sattui olemaan, ja niiden muuttaminen "
    "vaati agenttisivun löytämisen ja koko yhteyden purkamisen alusta asti. "
    "[ruutu: vanha agenttisivu, yhteys puretaan]\n"
    "Juuri siihen kohtaan uusi käyttäjä pysähtyi, eikä palannut takaisin samana iltana. "
    "[ruutu: keskeytynyt asennus, puolivalmis yhteys]\n"
    "Nyt valinta tehdään kerran, ennen kuin yhteys valmistuu. "
    "[ruutu: valmis yhteys, valitut oikeudet listattuna]"
)


def test_video_rules():
    assert jp.check_video(GOOD_VIDEO) == []
    assert any("kuvaustieto" in v for v in jp.check_video(GOOD_VIDEO + "\nPelkkä repliikki ilman ruutua."))
    stocked = GOOD_VIDEO.replace("[ruutu: vanha agenttisivu, yhteys puretaan]", "[stock: kädet näppäimistöllä]")
    assert any("kuvituskuva" in v for v in jp.check_video(stocked))
    logoed = GOOD_VIDEO.replace("[ruutu: hyväksymisikkuna auki, oikeusvalinnat näkyvissä]", "[ruutu: logo]")
    assert any("logo" in v for v in jp.check_video(logoed))
    assert any("sanaa" in v for v in jp.check_video("Yksi rivi. [ruutu: a]\nToinen. [ruutu: b]\nKolmas. [ruutu: c]"))


# ── the write: the exact key, the object shape, and the task's deliverable_key ───────────────────
class _StubLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts: list[str] = []

    def call(self, messages):
        self.prompts.append(messages[0]["content"])
        return self.replies.pop(0) if self.replies else self.replies_exhausted()

    def replies_exhausted(self):  # pragma: no cover - a test that gets here is mis-written
        raise AssertionError("the pipeline asked for more attempts than the test provided")


def _piece(text: str, notes: str = "jätin pois päivämäärän") -> str:
    return f"<TEKSTI>\n{text}\n</TEKSTI>\n<HUOMIOT>\n{notes}\n</HUOMIOT>"


@pytest.fixture
def stubbed(monkeypatch):
    writes: list[dict] = []
    monkeypatch.setattr(
        jp, "read_owner_key", lambda agent, key: {"aihe": "Hyväksymisikkuna", "kohdeyleiso": "kehittäjät"}
    )
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


def test_a_failed_write_is_reported_as_a_failure(monkeypatch):
    monkeypatch.setattr(jp, "read_owner_key", lambda agent, key: {"aihe": "x"})
    monkeypatch.setattr(jp, "_aimeat_call", lambda *a, **k: None)  # tunnel drop: the write did not land
    monkeypatch.setattr(jp, "get_llm", lambda **k: _StubLLM([_piece(GOOD_LINKEDIN)]))
    monkeypatch.setattr(jp, "resolved_model", lambda llm: "m")
    monkeypatch.setattr(jp, "resolved_provider", lambda: "p")
    out = jp.write_julkaisu("julkaisu-linkedin", "linkedin", "demo1")
    assert out.startswith("FAILED") and "did not land" in out


def test_a_reply_without_the_text_block_is_not_stored_as_the_piece():
    assert jp.parse_piece("Sure! Here is your post: ...") == ("", "")
    assert jp.parse_piece(_piece("hei", "huomio")) == ("hei", "huomio")


# ── the offers the workflow reads ────────────────────────────────────────────────────────────────
def test_each_writer_publishes_a_workflow_compatible_offer_keyed_on_ref():
    from aimeat_crewai.workflow_spec import assess_offer

    from crewaimeat.offers import offers_doc_any

    for channel, spec in jp.CHANNELS.items():
        offer = offers_doc_any(spec["agent"], with_samples=False)["offers"][0]
        assert assess_offer(offer)["workflow_compatible"], f"{spec['agent']}: {assess_offer(offer)['missing']}"
        assert offer["deliverable"]["location"]["key"] == f"julkaisu.{{ref}}.{channel}"
        assert offer["success_signal"]["key"] == f"julkaisu.{{ref}}.{channel}"
        assert offer["required_to_function"]["key"] == "julkaisu.{ref}.brief"


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

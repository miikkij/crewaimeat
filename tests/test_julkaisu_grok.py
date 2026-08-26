"""julkaisu-grok floor — the parts a model is not allowed to decide.

Four things break the app if they drift, and they are asserted here rather than requested in a
prompt: the clip id is the shot numbers and covers every shot once, a screen recording is recorded
and never carries a prompt, the length is one Imagine offers and is the smallest that carries, and
no prompt asks for readable text or calls the camera `locked`.

No LLM and no network: `_aimeat_call` and `get_llm` are stubbed, so every assertion is about the
module's own decisions.
"""

from __future__ import annotations

import json

import pytest

import crewaimeat.julkaisu_grok as jg

# ── a run that looks like the real one (julkaisu.2026-08-25) ─────────────────────────────────────
# 1-4 and 7-9 are screen recordings; 5 and 10 are wide shots with a generated first frame; 6 is a
# close-up with no image of its own.
SHOTS = [
    {
        "nro": 1,
        "kesto_s": 4,
        "kuvakoko": "ruutukaappaus",
        "kuvassa": "osoiterivi",
        "liike": "still",
        "puhe": "",
        "ruututeksti": "The browser stopped asking.",
        "aani": "vaimennettu tausta",
    },
    {
        "nro": 2,
        "kesto_s": 5,
        "kuvakoko": "ruutukaappaus",
        "kuvassa": "valikko auki",
        "liike": "still",
        "puhe": "",
        "ruututeksti": "",
        "aani": "vaimennettu tausta",
    },
    {
        "nro": 3,
        "kesto_s": 4,
        "kuvakoko": "ruutukaappaus",
        "kuvassa": "harmaa valinta",
        "liike": "still",
        "puhe": "",
        "ruututeksti": "",
        "aani": "isku leikkauksessa",
    },
    {
        "nro": 4,
        "kesto_s": 5,
        "kuvakoko": "ruutukaappaus",
        "kuvassa": "asetussivu",
        "liike": "still",
        "puhe": "",
        "ruututeksti": "",
        "aani": "vaimennettu tausta",
    },
    {
        "nro": 5,
        "kesto_s": 6,
        "kuvakoko": "laaja",
        "kuvassa": "tyopoyta hamarassa",
        "liike": "hidas zoom sisaan",
        "puhe": "Pimeys on se mita selain jatti tekematta.",
        "ruututeksti": "",
        "aani": "puhe",
    },
    {
        "nro": 6,
        "kesto_s": 5,
        "kuvakoko": "lahikuva",
        "kuvassa": "kadet nappaimistolla",
        "liike": "still",
        "puhe": "",
        "ruututeksti": "",
        "aani": "vaimennettu tausta",
    },
    {
        "nro": 7,
        "kesto_s": 5,
        "kuvakoko": "ruutukaappaus",
        "kuvassa": "lupaikkuna",
        "liike": "still",
        "puhe": "",
        "ruututeksti": "",
        "aani": "vaimennettu tausta",
    },
    {
        "nro": 8,
        "kesto_s": 4,
        "kuvakoko": "ruutukaappaus",
        "kuvassa": "hyvaksynta",
        "liike": "still",
        "puhe": "",
        "ruututeksti": "",
        "aani": "isku leikkauksessa",
    },
    {
        "nro": 9,
        "kesto_s": 5,
        "kuvakoko": "ruutukaappaus",
        "kuvassa": "valmis yhteys",
        "liike": "still",
        "puhe": "",
        "ruututeksti": "",
        "aani": "vaimennettu tausta",
    },
    {
        "nro": 10,
        "kesto_s": 6,
        "kuvakoko": "laaja",
        "kuvassa": "ikkuna aamulla",
        "liike": "hidas zoom sisaan",
        "puhe": "Nyt se kysyy ensin.",
        "ruututeksti": "",
        "aani": "puhe",
    },
]
VIDEO = {"kesto_s": 49, "muoto": "9:16", "kohtaukset": SHOTS, "kuvapyynnot": [{"nro": 5}, {"nro": 10}]}
KUVAT = {
    "kuvat": [
        {"nro": 5, "url": "https://aimeat.io/v1/pub/x/images/a.jpg", "storage_key": "images/a.jpg"},
        {"nro": 10, "url": "https://aimeat.io/v1/pub/x/images/b.jpg", "storage_key": "images/b.jpg"},
    ]
}
VALINTA = {"kulma": {"kulma": "Selain lakkasi kysymasta", "avaus": "Yksitoista kuukautta", "kenelle": "kehittajat"}}
TILAUS = {"tyylit": ["havainto"], "kielet": ["fi"]}
TASK = {"id": "t-1", "scope": [{"name": "deliverable_key", "type": "text", "value": "julkaisu.2026-08-25.grok"}]}


class _StubLLM:
    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts: list[str] = []

    def call(self, messages):
        self.prompts.append(messages[0]["content"])
        if not self.replies:
            raise AssertionError("more attempts were asked for than the test provided")
        return self.replies.pop(0)


def _clip(planned: dict) -> dict:
    """A clip written the way the contract wants it, from the plan the code already made."""
    out = dict(planned)
    out.pop("_shots", None)
    out.update(
        {
            "aani": True,
            "tunnelma": "havainto",
            "kielto": False,
            "miksi": "Lyhin joka kantaa.",
            "ruututeksti_jalkikateen": [],
        }
    )
    if planned["tyyppi"] == "nauhoita":
        # A recording is filmed off a real screen: no prompt, and nothing to select in Imagine.
        out["nauhoitusohje"] = "Avaa selain ja pida osoiterivi nakyvissa kolme sekuntia."
        return out
    spoken = " ".join(f'"{s["puhe"]}"' for s in planned["_shots"] if str(s.get("puhe") or "").strip())
    out["prompt"] = (
        "The shadow creeps across the desk as the glow dims. Camera not moving. "
        f"Look: near-dark, one screen as the only light. Sound: narration, low: {spoken} no music. "
        "Keep the subject and the framing unchanged."
    )
    out["imagine"] = {
        "tila": "image-to-video" if planned.get("kuva_url") else "text-to-video",
        "kesto": f"{planned['grok_kesto_s']}s",
        "tarkkuus": "720p",
        "kuvasuhde": "9:16",
        "aani": "paalla",
        "liite": "-",
    }
    return out


def _reply(clips: list[dict]) -> str:
    return json.dumps(
        {"asetukset": {"tarkkuus": "720p", "kuvasuhde": "9:16", "ketjutus": False}, "klipit": clips}, ensure_ascii=False
    )


@pytest.fixture
def stubbed(monkeypatch):
    writes: list[dict] = []

    def _read(agent, key):
        return {"video": VIDEO, "kuvat": KUVAT}.get(key.rsplit(".", 1)[-1]) or (
            VALINTA if key.endswith(".valinta") else TILAUS if key.endswith(".tilaus") else None
        )

    monkeypatch.setattr(jg, "read_owner_key", _read)
    monkeypatch.setattr(
        jg, "_aimeat_call", lambda a, tool, payload: writes.append({"tool": tool, **payload}) or {"ok": True}
    )
    monkeypatch.setattr(jg, "record_deliverable_key", lambda tid, key: None)
    monkeypatch.setattr(jg, "resolved_model", lambda llm: "test-model")
    monkeypatch.setattr(jg, "resolved_provider", lambda: "test-provider")
    return writes


def _good_reply() -> str:
    return _reply([_clip(p) for p in jg.plan_clips(SHOTS, KUVAT)])


# ── 1. the id is the shot numbers, and the clips cover every shot exactly once ────────────────────
def test_the_clip_id_is_its_shot_numbers_joined_by_a_hyphen():
    assert jg.clip_id([5]) == "5" and jg.clip_id([5, 6]) == "5-6"


def test_the_clips_cover_every_shot_exactly_once():
    clips = jg.plan_clips(SHOTS, KUVAT)
    covered = [n for c in clips for n in c["kohtaukset"]]
    assert covered == sorted(covered) == [s["nro"] for s in SHOTS]
    assert all(c["id"] == jg.clip_id(c["kohtaukset"]) for c in clips)


def test_a_model_that_renames_a_clip_does_not_move_it(stubbed, monkeypatch):
    """The app joins the person's uploaded video on the id, so the model's id is discarded and the
    plan's own id is written back — a re-run cannot rename a clip somebody already filled."""
    clips = [_clip(p) for p in jg.plan_clips(SHOTS, KUVAT)]
    clips[0]["id"], clips[0]["kohtaukset"] = "kohtaus-yksi", [99]
    monkeypatch.setattr(jg, "get_llm", lambda **k: _StubLLM([_reply(clips), _good_reply()]))

    jg.tee_grok("julkaisu-grok", task=TASK)

    written = next(w for w in stubbed if w["tool"] == "aimeat_memory_write")["value"]["klipit"]
    assert [c["id"] for c in written] == [c["id"] for c in jg.plan_clips(SHOTS, KUVAT)]


# ── 2. a screen recording is recorded, never generated ───────────────────────────────────────────
def test_a_screen_recording_is_recorded_and_never_shares_a_clip_with_a_generated_shot():
    clips = jg.plan_clips(SHOTS, KUVAT)
    by_nro = {s["nro"]: s for s in SHOTS}
    for c in clips:
        kinds = {by_nro[n]["kuvakoko"] == jg.SCREEN_RECORDING for n in c["kohtaukset"]}
        assert len(kinds) == 1, f"clip {c['id']} mixes recorded and generated"
        assert c["tyyppi"] == ("nauhoita" if kinds.pop() else "generoi")


def test_a_prompt_written_for_a_recording_is_thrown_away(stubbed, monkeypatch):
    clips = []
    for p in jg.plan_clips(SHOTS, KUVAT):
        c = _clip(p)
        if p["tyyppi"] == "nauhoita":
            c["prompt"] = "A browser window fades in."  # the model tried to generate the UI
        clips.append(c)
    monkeypatch.setattr(jg, "get_llm", lambda **k: _StubLLM([_reply(clips)]))

    out = jg.tee_grok("julkaisu-grok", task=TASK)

    written = next(w for w in stubbed if w["tool"] == "aimeat_memory_write")["value"]["klipit"]
    rec = [c for c in written if c["tyyppi"] == "nauhoita"]
    assert rec and all("prompt" not in c for c in rec)
    assert all(c["nauhoitusohje"].strip() for c in rec)
    assert out.startswith("OK:")


def test_a_recording_with_no_instruction_is_fatal(stubbed, monkeypatch):
    """Nothing to paste and nothing to shoot — there is no half of this a person can use."""
    clips = []
    for p in jg.plan_clips(SHOTS, KUVAT):
        c = _clip(p)
        if p["tyyppi"] == "nauhoita":
            c["nauhoitusohje"] = ""
        clips.append(c)
    monkeypatch.setattr(jg, "get_llm", lambda **k: _StubLLM([_reply(clips)] * jg._MAX_ATTEMPTS))

    out = jg.tee_grok("julkaisu-grok", task=TASK)

    assert out.startswith("FAILED") and "julkaisu.2026-08-25.grok" in out
    assert not [w for w in stubbed if w["tool"] == "aimeat_memory_write"]


# ── 3. the length is one Imagine offers, and it is the smallest that carries ─────────────────────
@pytest.mark.parametrize("content,want", [(3, 6), (6, 6), (7, 10), (10, 10), (12, 10), (14, 15)])
def test_the_length_is_the_shortest_that_carries_not_the_ceiling(content, want):
    """Physics and audio hold best at 5-8 s and drift toward 15 s, so 12 s of content is offered as
    10 rather than rounded up. The old spec's default of 15 is exactly the error."""
    assert jg.suggest_duration(content) == want


def test_every_planned_length_is_one_imagine_offers():
    assert all(c["grok_kesto_s"] in jg.GROK_DURATIONS for c in jg.plan_clips(SHOTS, KUVAT))


# ── 4. the prompt rules the research settled ─────────────────────────────────────────────────────
def _one_generated(prompt: str) -> list[str]:
    plan = jg.plan_clips(SHOTS, KUVAT)
    clips = [_clip(p) for p in plan]
    gen = next(c for c in clips if c["tyyppi"] == "generoi")
    gen["prompt"] = prompt
    return jg.check_clips(clips, SHOTS)


def test_locked_and_stable_are_refused_because_they_do_not_lock():
    for phrase in ("locked static shot", "stable camera", "steady shot"):
        bad = _one_generated(f"The shadow creeps in. {phrase}. Look: dark. Sound: room tone, no music.")
        assert any("liikkeeksi" in b for b in bad), phrase


def test_camera_not_moving_is_accepted():
    assert not any(
        "liikkeeksi" in b
        for b in _one_generated("The shadow creeps in. Camera not moving. Look: dark. Sound: room tone, no music.")
    )


def test_asking_for_readable_text_is_refused():
    bad = _one_generated('The shadow creeps in. On-screen text: "Install". Sound: room tone, no music.')
    assert any("luettavaa teksti" in b for b in bad)


def test_the_shot_lists_burned_in_text_never_reaches_a_prompt(stubbed, monkeypatch):
    monkeypatch.setattr(jg, "get_llm", lambda **k: _StubLLM([_good_reply()]))
    jg.tee_grok("julkaisu-grok", task=TASK)
    written = next(w for w in stubbed if w["tool"] == "aimeat_memory_write")["value"]["klipit"]
    assert not any("The browser stopped asking" in str(c.get("prompt") or "") for c in written)
    assert all("ruututeksti_jalkikateen" in c for c in written), "it has its own field for the editor"


def test_a_prompt_that_opens_with_style_is_refused_because_the_model_is_sequential():
    bad = _one_generated("Cinematic, moody. The shadow creeps in. Sound: room tone, no music.")
    assert any("teko ensimm" in b for b in bad)


def test_speech_requires_a_sound_block_and_no_music():
    bad = _one_generated("The shadow creeps in. Camera not moving. Look: dark.")
    assert any("Sound:" in b for b in bad)
    bad = _one_generated("The shadow creeps in. Camera not moving. Sound: narration, warm.")
    assert any("no music" in b for b in bad)


# ── the address and the write ────────────────────────────────────────────────────────────────────
def test_the_clips_land_at_the_key_the_run_was_given(stubbed, monkeypatch):
    monkeypatch.setattr(jg, "get_llm", lambda **k: _StubLLM([_good_reply()]))
    recorded: list = []
    monkeypatch.setattr(jg, "record_deliverable_key", lambda tid, key: recorded.append((tid, key)))

    out = jg.tee_grok("julkaisu-grok", task=TASK, task_id="t-1")

    write = next(w for w in stubbed if w["tool"] == "aimeat_memory_write")
    assert write["key"] == "julkaisu.2026-08-25.grok" and write["visibility"] == "owner"
    assert recorded == [("t-1", "julkaisu.2026-08-25.grok")]
    assert out.startswith("OK:") and "10 klippi" not in out  # 10 shots become fewer clips
    assert set(write["value"]) == {"asetukset", "klipit"}


def test_every_owner_read_carries_owner_scope(monkeypatch):
    """Without owner_scope the agent cannot see what a person or the app wrote (commit 8ad9144)."""
    seen: list[dict] = []
    monkeypatch.setattr(
        "crewaimeat.memory_tools._aimeat_call",
        lambda agent, tool, payload: seen.append(payload) or {"value": json.dumps(VIDEO)},
    )
    jg._read("julkaisu-grok", "julkaisu.2026-08-25.video")
    assert seen and all(p.get("owner_scope") is True for p in seen)


def test_no_shot_list_is_fatal_and_writes_nothing(stubbed, monkeypatch):
    monkeypatch.setattr(jg, "read_owner_key", lambda agent, key: {})
    monkeypatch.setattr(jg, "get_llm", lambda **k: _StubLLM([]))
    out = jg.tee_grok("julkaisu-grok", task=TASK)
    assert out.startswith("FAILED") and "does not invent shots" in out
    assert not [w for w in stubbed if w["tool"] == "aimeat_memory_write"]


def test_a_bent_prompt_rule_is_stored_with_the_work_not_thrown_away(stubbed, monkeypatch):
    """Nothing here publishes: a person reads the clip before pasting it. A prompt that says
    'steady shot' is something they fix in five seconds; discarding the run is the worse outcome."""
    clips = [_clip(p) for p in jg.plan_clips(SHOTS, KUVAT)]
    gen = next(c for c in clips if c["tyyppi"] == "generoi")
    gen["prompt"] = "The shadow creeps in. Steady shot. Look: dark. Sound: room tone, no music."
    monkeypatch.setattr(jg, "get_llm", lambda **k: _StubLLM([_reply(clips)] * jg._MAX_ATTEMPTS))

    out = jg.tee_grok("julkaisu-grok", task=TASK)

    value = next(w for w in stubbed if w["tool"] == "aimeat_memory_write")["value"]
    assert value["rikkeet"] and any("liikkeeksi" in r for r in value["rikkeet"])
    assert out.startswith("OK:") and "jäi täyttymättä" in out


def test_the_second_attempt_is_told_what_was_wrong(stubbed, monkeypatch):
    clips = [_clip(p) for p in jg.plan_clips(SHOTS, KUVAT)]
    next(c for c in clips if c["tyyppi"] == "generoi")["prompt"] = "Locked static shot of a desk."
    llm = _StubLLM([_reply(clips), _good_reply()])
    monkeypatch.setattr(jg, "get_llm", lambda **k: llm)

    jg.tee_grok("julkaisu-grok", task=TASK)

    assert len(llm.prompts) == 2 and "Korjaa nämä" in llm.prompts[1]


def test_the_image_is_the_first_frame_and_the_mode_says_so(stubbed, monkeypatch):
    monkeypatch.setattr(jg, "get_llm", lambda **k: _StubLLM([_good_reply()]))
    jg.tee_grok("julkaisu-grok", task=TASK)
    written = next(w for w in stubbed if w["tool"] == "aimeat_memory_write")["value"]["klipit"]
    with_image = [c for c in written if c.get("kuva_url")]
    assert {c["id"] for c in with_image} == {"5-6", "10"}, "shot 6 rides on 5's first frame"
    assert all(c["kuva"] == "ensimmainen_ruutu" for c in with_image)
    assert all(c["imagine"]["tila"] == "image-to-video" for c in with_image)
    assert all(not c.get("kuva_url") and c["kuva"] == "ei" for c in written if c["tyyppi"] == "nauhoita")


# -- what the first live run against julkaisu.2026-08-25 exposed -------------------------------
def test_the_spoken_line_must_be_the_scripts_own_words():
    """On the first live run the model translated two English lines into Finnish. The voice is
    generated in the same pass as the picture, so a wrong line is not fixable in the edit -- the
    person would have to run the clip again."""
    bad = _one_generated(
        "The shadow creeps in. Camera not moving. Sound: narration: "
        '"Darkness is what the browser left undone." no music.'
    )
    assert any("sanatarkasti" in b for b in bad)
    quoted = _one_generated(
        "The shadow creeps in. Camera not moving. Sound: narration: "
        '"Pimeys on se mita selain jatti tekematta." no music.'
    )
    assert not any("sanatarkasti" in b for b in quoted)


def test_a_recording_has_no_imagine_settings(stubbed, monkeypatch):
    """`imagine` is what a person CLICKS in Imagine. A recording is filmed off a real screen, and
    the live run filled its mode with 'reference-to-video' -- an instruction to do the one thing the
    shot list forbids, generate the product's own UI."""
    clips = []
    for p in jg.plan_clips(SHOTS, KUVAT):
        c = _clip(p)
        if p["tyyppi"] == "nauhoita":
            c["imagine"] = {"tila": "reference-to-video", "kesto": "10s", "tarkkuus": "720p"}
        clips.append(c)
    monkeypatch.setattr(jg, "get_llm", lambda **k: _StubLLM([_reply(clips)]))

    jg.tee_grok("julkaisu-grok", task=TASK)

    written = next(w for w in stubbed if w["tool"] == "aimeat_memory_write")["value"]["klipit"]
    assert all("imagine" not in c for c in written if c["tyyppi"] == "nauhoita")
    assert all(c["imagine"]["tila"] in jg.MODES for c in written if c["tyyppi"] == "generoi")


def test_a_cut_may_open_the_prompt_when_the_cut_is_the_action():
    """The camera is not the action -- except on a shot whose own `liike` IS the cut, where it is
    exactly what happens first. The rule was right about the danger and wrong about that shot."""
    cut_shots = [dict(s, liike="leikkaus") for s in SHOTS]
    clips = [_clip(p) for p in jg.plan_clips(cut_shots, KUVAT)]
    for c in clips:
        if c["tyyppi"] == "generoi":
            c["prompt"] = "Camera cuts to the dark desk. Sound: room tone, no music."
    assert not any("teko ensimm" in b for b in jg.check_clips(clips, cut_shots))
    # the same opening on a still shot is still the old error
    assert any("teko ensimm" in b for b in _one_generated("Camera cuts to the dark desk. Sound: room tone."))

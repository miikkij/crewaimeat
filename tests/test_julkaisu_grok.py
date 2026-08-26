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
    """Serves the two passes. Pass 1 asks for the whole structure; pass 2 asks one clip for its two
    long versions, so the stub answers by WHAT WAS ASKED rather than by call order — otherwise every
    test would have to count calls (2 generated clips = 2 extra) to stay green."""

    def __init__(self, replies, long_reply=None, short_reply=None):
        self.replies = list(replies)
        self.long_reply, self.short_reply = long_reply, short_reply
        self.prompts: list[str] = []
        self.short_prompts: list[str] = []
        self.long_prompts: list[str] = []

    def call(self, messages):
        text = messages[0]["content"]
        if '{"lyhyt"' in text:  # pass 2's response contract
            self.short_prompts.append(text)
            return self.short_reply if self.short_reply is not None else _short_reply(text)
        if '{"laaja"' in text:  # pass 3's
            self.long_prompts.append(text)
            return self.long_reply if self.long_reply is not None else _long_reply(text)
        self.prompts.append(text)
        if not self.replies:
            raise AssertionError("more attempts were asked for than the test provided")
        return self.replies.pop(0)


PRESETS = [
    {
        "id": "hiljainen-osoiterivi",
        "nimi": "Hiljainen osoiterivi",
        "look": "near-dark room, one monitor as the only light, cold blue-grey on the desk edge",
        "aani": "room tone of an empty flat, a fan behind the wall, no music",
        "miksi": "Valittu kulma on hiljaisuus, joten pimeys on tama jutun materiaali eika tehoste.",
    }
]

# Filler that adds what the FRAME DOES NOT SHOW, which is the rule the long versions exist for.
_UNSEEN = (
    "The desk surface holds a thin film of dust that never lifts. The air in the room is still and "
    "cool against the back of the hands. Behind the wall a fan turns at a constant speed. "
)


def _long_body(spoken: str, target: int) -> str:
    head = (
        "The shadow creeps across the desk as the glow dims. Camera not moving. "
        f"Look: near-dark, one screen as the only light. Sound: narration, low: {spoken} no music. "
    )
    body = head + _UNSEEN * (1 + (target - len(head)) // len(_UNSEEN))
    return body[: target - 40].rsplit(" ", 1)[0] + ". Keep the subject and the framing unchanged."


def _spoken_of(text: str) -> str:
    """The lines pass 2 was told to quote, pulled back out of its own prompt."""
    return " ".join(f'"{s["puhe"]}"' for s in SHOTS if s["puhe"] and f"puhe: '{s['puhe']}'" in text)


def _short_reply(ask: str) -> str:
    spoken = _spoken_of(ask)
    return json.dumps(
        {
            "lyhyt": "The shadow creeps across the desk as the glow dims. Camera not moving. Room tone, no music.",
            "keskiko": (
                "The shadow creeps across the desk as the glow dims. Camera not moving. "
                f"Look: near-dark, one screen as the only light. Sound: narration, low: {spoken} no music. "
                "Keep the subject and the framing unchanged."
            ),
        },
        ensure_ascii=False,
    )


def _long_reply(ask: str) -> str:
    spoken = _spoken_of(ask)
    return json.dumps({"laaja": _long_body(spoken, 1200), "massiivinen": _long_body(spoken, 3000)}, ensure_ascii=False)


def _clip(planned: dict, with_prompts: bool = False) -> dict:
    """A clip the way the contract wants it. Pass 1 writes NO prompts — those are asked per clip
    afterwards — so `with_prompts` is for the tests that check prompt rules directly."""
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
    out["preset"] = PRESETS[0]["id"]
    out["imagine"] = {
        "tila": "image-to-video" if planned.get("kuva_url") else "text-to-video",
        "kesto": f"{planned['grok_kesto_s']}s",
        "tarkkuus": "720p",
        "kuvasuhde": "9:16",
        "aani": "paalla",
        "liite": "-",
    }
    if not with_prompts:
        return out
    out["promptit"] = {
        "lyhyt": "The shadow creeps across the desk as the glow dims. Camera not moving. Room tone, no music.",
        "keskiko": (
            "The shadow creeps across the desk as the glow dims. Camera not moving. "
            f"Look: near-dark, one screen as the only light. Sound: narration, low: {spoken} no music. "
            "Keep the subject and the framing unchanged."
        ),
    }
    return out


def _reply(clips: list[dict], presets: list | None = None) -> str:
    return json.dumps(
        {
            "asetukset": {"tarkkuus": "720p", "kuvasuhde": "9:16", "ketjutus": False},
            "presetit": PRESETS if presets is None else presets,
            "klipit": clips,
        },
        ensure_ascii=False,
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
            c["promptit"] = {"keskiko": "A browser window fades in."}  # it tried to generate the UI
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
def _one_generated(prompt: str, size: str = "keskiko") -> list[str]:
    """One generated clip's prompt at ONE size, checked against the rules that apply to that size."""
    clips = [_clip(p, with_prompts=True) for p in jg.plan_clips(SHOTS, KUVAT)]
    gen = next(c for c in clips if c["tyyppi"] == "generoi")
    gen["promptit"] = {size: prompt}
    return jg.check_clips(clips, SHOTS, sizes=(size,))


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
    assert not any("The browser stopped asking" in t for c in written for t in jg.prompts_of(c).values())
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
    assert set(write["value"]) == {"asetukset", "presetit", "klipit"}
    gen = [c for c in write["value"]["klipit"] if c["tyyppi"] == "generoi"]
    assert all(set(jg.prompts_of(c)) == set(jg.PROMPT_SIZES) for c in gen), "all four versions, every clip"


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
    bent = json.dumps(
        {
            "lyhyt": "The shadow creeps in. Camera not moving. Room tone under it, no music at all.",
            "keskiko": "The shadow creeps in. Steady shot. Look: dark. Sound: room tone, no music.",
        },
        ensure_ascii=False,
    )
    monkeypatch.setattr(jg, "get_llm", lambda **k: _StubLLM([_good_reply()] * jg._MAX_ATTEMPTS, short_reply=bent))

    out = jg.tee_grok("julkaisu-grok", task=TASK)

    value = next(w for w in stubbed if w["tool"] == "aimeat_memory_write")["value"]
    assert value["rikkeet"] and any("liikkeeksi" in r for r in value["rikkeet"])
    assert out.startswith("OK:") and "jäi täyttymättä" in out


def test_the_second_attempt_is_told_what_was_wrong(stubbed, monkeypatch):
    clips = [_clip(p) for p in jg.plan_clips(SHOTS, KUVAT)]
    next(c for c in clips if c["tyyppi"] == "generoi")["tunnelma"] = "ei-tallainen"
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
    clips = [_clip(p, with_prompts=True) for p in jg.plan_clips(cut_shots, KUVAT)]
    for c in clips:
        if c["tyyppi"] == "generoi":
            c["promptit"] = {"keskiko": "Camera cuts to the dark desk. Sound: room tone, no music."}
    assert not any("teko ensimm" in b for b in jg.check_clips(clips, cut_shots, sizes=("keskiko",)))
    # the same opening on a still shot is still the old error
    assert any("teko ensimm" in b for b in _one_generated("Camera cuts to the dark desk. Sound: room tone."))


# -- the experiment: four prompts per clip, and presets made for THIS job ------------------------
def test_the_four_versions_are_genuinely_different_orders_of_magnitude(stubbed, monkeypatch):
    """The point is not four candidates to pick from. A person runs the same shot on all four and
    watches where extra text starts helping and where it starts drowning -- and four near-identical
    prompts measure nothing at all."""
    monkeypatch.setattr(jg, "get_llm", lambda **k: _StubLLM([_good_reply()]))

    jg.tee_grok("julkaisu-grok", task=TASK)

    written = next(w for w in stubbed if w["tool"] == "aimeat_memory_write")["value"]["klipit"]
    for c in [c for c in written if c["tyyppi"] == "generoi"]:
        n = {size: len(text) for size, text in jg.prompts_of(c).items()}
        assert len(jg.prompts_of(c)["lyhyt"].split()) <= 25
        assert n["laaja"] > 1000, n
        assert n["massiivinen"] > 2500, n
        assert n["massiivinen"] > 2 * n["laaja"] - n["laaja"], "each step is its own size, not a nudge"
        assert n["lyhyt"] < n["keskiko"] < n["laaja"] < n["massiivinen"], n


def test_a_short_prompt_is_not_asked_to_carry_the_spoken_line():
    """8-25 words carry motion, one camera and the sound. Demanding the dialogue there too would be
    the rule being right about the danger and wrong about the size -- it does not fit."""
    bad = _one_generated("The shadow creeps in. Camera not moving. Room tone, no music.", size="lyhyt")
    assert not any("sanatarkasti" in b for b in bad)
    assert not any("Sound:" in b for b in bad)
    # the same omission in the medium version IS the error
    assert any("sanatarkasti" in b for b in _one_generated("The shadow creeps in. Camera not moving. Look: dark."))


def test_a_long_version_that_is_actually_short_is_refused():
    clips = [_clip(p, with_prompts=True) for p in jg.plan_clips(SHOTS, KUVAT)]
    gen = next(c for c in clips if c["tyyppi"] == "generoi")
    gen["promptit"]["laaja"] = "The shadow creeps in. Camera not moving. Sound: room tone, no music."
    gen["promptit"]["massiivinen"] = _long_body('"Pimeys on se mita selain jatti tekematta."', 1100)
    bad = jg.check_clips(clips, SHOTS)
    assert any("laaja" in b and "vähintään 1000" in b for b in bad)
    assert any("massiivinen" in b and "vähintään 2500" in b for b in bad)


def test_a_preset_off_a_generic_shelf_is_refused():
    """'This job's preset is this job's place.' A name that would fit any job is the tell."""
    clips = [_clip(p) for p in jg.plan_clips(SHOTS, KUVAT)]
    generic = [{**PRESETS[0], "id": "pimea", "nimi": "Pimeä"}]
    for c in clips:
        if c["tyyppi"] == "generoi":
            c["preset"] = "pimea"
    bad = jg.check_presets(generic, clips)
    assert any("geneerinen nimi" in b for b in bad)
    named = [dict(c, preset=PRESETS[0]["id"]) for c in clips]
    assert jg.check_presets(PRESETS, named) == [], "a preset written from this job's own material passes"


def test_a_clip_pointing_at_a_preset_nobody_wrote_is_caught():
    clips = [dict(c, preset="ei-ole") for c in (_clip(p) for p in jg.plan_clips(SHOTS, KUVAT))]
    assert any("ei ole 'presetit'-listassa" in b for b in jg.check_presets(PRESETS, clips))


def test_presets_are_optional():
    clips = [_clip(p) for p in jg.plan_clips(SHOTS, KUVAT)]
    for c in clips:
        c.pop("preset", None)
    assert jg.check_presets([], clips) == [] and jg.check_presets(None, clips) == []


def test_a_recording_carries_no_preset_and_no_prompts(stubbed, monkeypatch):
    monkeypatch.setattr(jg, "get_llm", lambda **k: _StubLLM([_good_reply()]))
    jg.tee_grok("julkaisu-grok", task=TASK)
    written = next(w for w in stubbed if w["tool"] == "aimeat_memory_write")["value"]["klipit"]
    rec = [c for c in written if c["tyyppi"] == "nauhoita"]
    assert rec and all("promptit" not in c and "preset" not in c for c in rec)


def test_the_long_versions_are_asked_for_one_clip_at_a_time(stubbed, monkeypatch):
    """~3000 characters each, and one response carrying every clip's is where a model truncates --
    which would cost the whole run instead of one clip."""
    llm = _StubLLM([_good_reply()])
    monkeypatch.setattr(jg, "get_llm", lambda **k: llm)

    jg.tee_grok("julkaisu-grok", task=TASK)

    assert len(llm.prompts) == 1, "one structural call"
    assert len(llm.long_prompts) == 2, "one per GENERATED clip, and none for the recordings"
    assert all("5-6" in p or "10" in p for p in llm.long_prompts)


def test_a_clip_whose_long_versions_never_land_keeps_its_short_ones(stubbed, monkeypatch):
    """Pass 2 failing is a note, not a discard: the person still has two prompts to paste."""
    llm = _StubLLM([_good_reply()], long_reply="not json at all")
    monkeypatch.setattr(jg, "get_llm", lambda **k: llm)

    out = jg.tee_grok("julkaisu-grok", task=TASK)

    value = next(w for w in stubbed if w["tool"] == "aimeat_memory_write")["value"]
    gen = [c for c in value["klipit"] if c["tyyppi"] == "generoi"]
    assert all(set(jg.prompts_of(c)) == {"lyhyt", "keskiko"} for c in gen)
    assert out.startswith("OK:") and value["rikkeet"], "stored, with the gap recorded"
    assert any("puuttuu" in r for r in value["rikkeet"])


def test_a_flaky_last_attempt_cannot_destroy_a_good_earlier_one(stubbed, monkeypatch):
    """Measured on the first live run of the four-prompt shape: attempt 2 produced a complete
    structure that was one word over on one prompt, attempt 3 came back as prose, and the whole run
    failed with nothing written. A retry must never end worse than its best attempt."""
    clips = [_clip(p) for p in jg.plan_clips(SHOTS, KUVAT)]
    next(c for c in clips if c["tyyppi"] == "generoi")["tunnelma"] = "ei-tallainen"
    llm = _StubLLM(["ei mitään JSONia", _reply(clips), "taas pelkkää proosaa"])
    monkeypatch.setattr(jg, "get_llm", lambda **k: llm)

    out = jg.tee_grok("julkaisu-grok", task=TASK)

    value = next(w for w in stubbed if w["tool"] == "aimeat_memory_write")["value"]
    assert out.startswith("OK:"), out
    assert len(value["klipit"]) == 6, "the structure from attempt 2 survived attempt 3"
    assert any("tunnelma" in r for r in value["rikkeet"]), "and the fault travels as a note"


def test_the_best_attempt_wins_even_when_a_later_one_parses(stubbed, monkeypatch):
    """Not just 'JSON beats prose': a parseable but WORSE answer must not replace a better one."""
    good = [_clip(p) for p in jg.plan_clips(SHOTS, KUVAT)]
    worse = [dict(c) for c in good]
    for c in worse:
        if c["tyyppi"] == "generoi":
            c["tunnelma"] = "ei-sallittu"
            c["miksi"] = ""
    llm = _StubLLM([_reply(good), _reply(worse)])
    monkeypatch.setattr(jg, "get_llm", lambda **k: llm)

    jg.tee_grok("julkaisu-grok", task=TASK)

    value = next(w for w in stubbed if w["tool"] == "aimeat_memory_write")["value"]
    assert all(c["tunnelma"] in jg.TUNNELMAT for c in value["klipit"])
    assert len(llm.prompts) == 1, "a clean first attempt is not retried at all"


def test_a_flawed_prompt_still_beats_an_empty_one(stubbed, monkeypatch):
    """'Keep the best attempt' has an edge: an EMPTY version violates once (it is missing) exactly
    like a written one that bends a style rule. Comparing violation counts alone would keep nothing
    over prose the person could fix in five seconds, so what is written wins first."""
    flawed = json.dumps(
        {"lyhyt": "Steady shot of the desk in the dark.", "keskiko": "Steady shot of the desk."},
        ensure_ascii=False,
    )
    monkeypatch.setattr(jg, "get_llm", lambda **k: _StubLLM([_good_reply()] * jg._MAX_ATTEMPTS, short_reply=flawed))

    out = jg.tee_grok("julkaisu-grok", task=TASK)

    value = next(w for w in stubbed if w["tool"] == "aimeat_memory_write")["value"]
    gen = [c for c in value["klipit"] if c["tyyppi"] == "generoi"]
    assert all(jg.prompts_of(c).get("lyhyt", "").strip() for c in gen), "the flawed text was KEPT"
    assert out.startswith("OK:") and any("liikkeeksi" in r for r in value["rikkeet"])

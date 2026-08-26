"""KANSI — `julkaisu-grok`: the shot list turned into Grok Imagine clips a person can paste.

It invents no story. The shot list already exists (`julkaisu.{ref}.video`); this turns it into
**clips and the settings to select in Imagine**, so nobody has to infer the settings from the prompt.
The app can do the same as a rule-transform and keeps that as a fallback; what an agent adds is where
the rule cannot reach — camera and sound as prose, a character that stays recognisable across clips,
and each clip's own settings WITH the reason for them.

Written against a real research pass on how Grok Imagine is actually prompted
(`docs/internal/2026-08-26-kansi-grok-skriptaaja.md`, background in `…-vertailu.md`). An earlier
version of the spec carried four errors that the research overturned; all four are enforced HERE, in
code, so they cannot be written back by a model having a bad day:

  1. `locked static shot` DOES NOT lock the camera — `stable`/`steady`/`locked` all read as smooth
     motion and the camera drifts. The phrase that locks is `Camera not moving.`
  2. NEVER ask for readable text in frame. Text in a frame is a documented artefact; the shot list's
     `ruututeksti` goes to `ruututeksti_jalkikateen` for the editor, never into the prompt.
  3. `Sound:` (not `AUDIO:`), and `no music` whenever there is speech — otherwise the model scores it.
  4. The default length is NOT 15 s. Physics and audio hold best at 5–8 s; 12–15 s drifts. Pick the
     shortest that carries.

Order inside a prompt is chronological, because the model is sequential: **a climax in the last
sentence does not get time to happen.** Action first, camera immediately after.

Every generated clip gets FOUR prompts, not one — `lyhyt`, `keskiko`, `laaja`, `massiivinen`, from
8 words to ~3000 characters. They are not candidates to pick the best of: a person runs the same
shot on all four in Imagine and watches where the extra text starts helping and where it starts
drowning the shot. Nobody has that line measured, so it is run rather than argued about. The long
versions add what the frame does NOT show — material, air, layers of sound, what must not move —
because repeating what is visible would make 3000 characters the same sentence four times, and the
experiment would measure nothing. `presetit` are the run's own look, designed from its order, angle
and background; a preset that would fit any job was not made for this one.

Two disagreements in the research are deliberately NOT settled here. Negations (one guide calls them
useless, two write them every time) and one-camera-move vs 2–3 beats: the agent takes the documented
default, and says in `miksi` when it departs, so a person can run the same shot both ways.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

from aimeat_crewai.provenance import HumanInvolvement, Level, Method, declare

from crewaimeat.aimeat_crew import _aimeat_call, record_deliverable_key
from crewaimeat.julkaisu_pipeline import (
    PIECE_KEY,
    TILAUS_KEY,
    VALINTA_KEY,
    parse_json_object,
    run_address,
)
from crewaimeat.llm import get_llm, resolved_model, resolved_provider
from crewaimeat.memory_tools import read_owner_key
from crewaimeat.prose_style import FINNISH_NATIVE_STYLE

_MAX_ATTEMPTS = 3

# A shot framed as a screen recording is ALWAYS recorded, never generated: no model draws a real
# product UI correctly, and a wrong one is worse than none.
SCREEN_RECORDING = "ruutukaappaus"

# What Imagine actually offers. `grok_kesto_s` is one of these — the smallest that carries the
# content, never rounded up to the ceiling.
GROK_DURATIONS = (6, 10, 15)
TUNNELMAT = ("", "havainto", "pimea", "kirkas", "karu", "eleginen", "ulkona")
MODES = ("text-to-video", "image-to-video", "reference-to-video")
KUVA_MODES = ("ensimmainen_ruutu", "referenssi", "ei")
REFERENCE_MAX_RES = "720p"  # reference-to-video is capped there; 1080p is T2V/I2V only

# Clip grouping: two shots may share a clip when they are one continuous move, and 12 s of content is
# already at the edge of where Imagine holds together.
_CLIP_MAX_SHOTS = 2
_CLIP_MAX_SECONDS = 12

# FOUR prompts for every generated clip, and this is the point of the job — not four candidates to
# pick the best of, but an EXPERIMENT. A person runs the same shot on all four in Imagine and sees
# with their own eyes where the extra text starts helping and where it starts drowning the shot.
# Nobody has that line measured; every guide guesses it. So it is not reasoned about, it is run.
PROMPT_SIZES = ("lyhyt", "keskiko", "laaja", "massiivinen")
# `lyhyt` is what I2V wants off a strong still; `keskiko` is the T2V recommendation; the two long
# ones are deliberately past every published recommendation.
_WORDS = {"lyhyt": (8, 25), "keskiko": (20, 60)}
_CHARS = {"laaja": (1000, 1500), "massiivinen": (2500, 3800)}
_IMAGINE_MAX_CHARS = 4096  # the hard ceiling; `massiivinen` stays clear of it on purpose
# 8-25 words cannot carry a spoken line AND a Sound: block — asking for them there would be the
# rule being right about the danger and wrong about the size.
_TOO_SHORT_FOR_SPEECH = ("lyhyt",)
_LONG_SIZES = ("laaja", "massiivinen")

# A preset is designed FOR THIS JOB from its order, angle and background — "this job's preset is
# this job's place". A name off a generic shelf is the tell that it was not.
_GENERIC_PRESETS = frozenset(
    {
        "pimea",
        "pimeä",
        "kirkas",
        "karu",
        "havainto",
        "eleginen",
        "ulkona",
        "dark",
        "bright",
        "moody",
        "cinematic",
        "neutral",
        "default",
        "oletus",
        "perus",
        "standard",
        "yleinen",
    }
)


# ── what it reads ────────────────────────────────────────────────────────────────────────────────
def _read(agent_name: str, key: str) -> dict:
    """One owner-memory read. `read_owner_key` passes owner_scope=True (commit 8ad9144) — without it
    an agent cannot see what a person or the app wrote under the owner's own GHII."""
    value = read_owner_key(agent_name, key)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def read_inputs(agent_name: str, ref: str) -> tuple[dict, dict, dict, dict]:
    """(video, kuvat, valinta, tilaus). Raises when there is no shot list — that is the one input
    this agent cannot work without, and it does not invent shots."""
    video = _read(agent_name, PIECE_KEY.format(ref=ref, channel="video"))
    shots = video.get("kohtaukset")
    if not isinstance(shots, list) or not shots:
        raise LookupError(
            f"shot list '{PIECE_KEY.format(ref=ref, channel='video')}' is missing or has no scenes — "
            "the script step has not run. Nothing was written; this agent does not invent shots."
        )
    return (
        video,
        _read(agent_name, PIECE_KEY.format(ref=ref, channel="kuvat")),
        _read(agent_name, VALINTA_KEY.format(ref=ref)),
        _read(agent_name, TILAUS_KEY.format(ref=ref)),
    )


# ── clip planning: CODE decides the grouping, so the ids cannot drift ────────────────────────────
def clip_id(shot_numbers: list[int]) -> str:
    """Shots 5 and 6 -> "5-6". Not a matter of taste: the app matches an uploaded video AND the
    clip's own settings on this id, so a different id means the person's upload finds no place."""
    return "-".join(str(n) for n in shot_numbers)


def plan_clips(shots: list[dict], kuvat: dict) -> list[dict]:
    """Group the shots into clips. Recorded and generated never share a clip.

    Grouping is done here rather than by the model for one reason: the id is an index the app joins
    on. A model that merges two shots differently on a re-run renames a clip the person has already
    uploaded a video for.
    """
    by_shot = {}
    for k in (kuvat.get("kuvat") or []) if isinstance(kuvat.get("kuvat"), list) else []:
        if isinstance(k, dict) and k.get("nro") is not None:
            by_shot[int(k["nro"])] = k

    clips: list[dict] = []
    current: list[dict] = []

    def _flush() -> None:
        if not current:
            return
        nums = [int(s["nro"]) for s in current]
        total = sum(int(s.get("kesto_s") or 0) for s in current)
        recorded = str(current[0].get("kuvakoko") or "").strip().casefold() == SCREEN_RECORDING
        image = next((by_shot[n] for n in nums if n in by_shot), None)
        clips.append(
            {
                "id": clip_id(nums),
                "kohtaukset": nums,
                "tyyppi": "nauhoita" if recorded else "generoi",
                "kesto_s": total,
                "grok_kesto_s": suggest_duration(total),
                "kuva": "ensimmainen_ruutu" if (image and not recorded) else "ei",
                "kuva_url": (image or {}).get("url", ""),
                "_shots": current[:],
            }
        )
        current.clear()

    for shot in shots:
        if not isinstance(shot, dict) or shot.get("nro") is None:
            continue
        recorded = str(shot.get("kuvakoko") or "").strip().casefold() == SCREEN_RECORDING
        if current:
            same_kind = (str(current[0].get("kuvakoko") or "").strip().casefold() == SCREEN_RECORDING) == recorded
            running = sum(int(s.get("kesto_s") or 0) for s in current)
            fits = len(current) < _CLIP_MAX_SHOTS and running + int(shot.get("kesto_s") or 0) <= _CLIP_MAX_SECONDS
            # A shot that has its own image starts a new clip: that image is its first frame.
            starts_own = int(shot["nro"]) in by_shot
            if not (same_kind and fits and not starts_own):
                _flush()
        current.append(shot)
    _flush()
    return clips


def suggest_duration(content_seconds: int) -> int:
    """The shortest Imagine length that carries the content.

    Biased DOWN on purpose. The tested claim is that physics and audio rhythm hold best at 5–8 s and
    drift toward 12–15 s, so 12 s of content is offered as 10 — a hair tighter — rather than rounded
    up to the ceiling. The old spec's reflex default of 15 s is exactly the error this avoids, and
    the clip keeps its own `kesto_s` beside this, so the difference is visible rather than silent.
    """
    if content_seconds <= 6:
        return 6
    return 10 if content_seconds <= 13 else 15


# ── the contract, checked in code ────────────────────────────────────────────────────────────────
# Phrases that DO NOT lock the camera even though they sound like they do.
_FAKE_LOCK = re.compile(r"\b(locked|stable camera|steady shot|static shot)\b", re.I)
_LOCK_PHRASE = "camera not moving"
# Asking for readable text in frame is a documented artefact generator.
_ASKS_FOR_TEXT = re.compile(r"\b(on-?screen text|text reads?|caption|subtitle|title card|the words?\s+[\"'])", re.I)
_STYLE_OPENERS = re.compile(r"^\s*(look|sound|cinematic|epic|style)\b[:,]?", re.I)
# The camera is not the action — EXCEPT on a shot whose own movement is the cut, where the cut IS
# what happens first. Flagging that would be right about the rule and wrong about the shot.
_CAMERA_OPENER = re.compile(r"^\s*(the\s+)?camera\b", re.I)
_CUT = "leikkaus"


def _first_sentence(prompt: str) -> str:
    return re.split(r"(?<=[.!?])\s", str(prompt).strip(), maxsplit=1)[0] if prompt else ""


def _opens_with_style(prompt: str, cut_is_the_action: bool) -> bool:
    """True when the prompt opens with style or camera instead of with what happens."""
    first = _first_sentence(prompt)
    if _STYLE_OPENERS.match(first):
        return True
    return bool(_CAMERA_OPENER.match(first)) and not cut_is_the_action


def _said(line: str) -> str:
    """A spoken line reduced to what must survive into the prompt."""
    return re.sub(r"[\s\"'“”‘’]+", " ", str(line or "")).strip().casefold()


def prompts_of(clip: dict) -> dict[str, str]:
    """A clip's prompts as {size: text}, tolerating a single `prompt` string from an older shape."""
    p = clip.get("promptit")
    if isinstance(p, dict):
        return {k: str(v or "") for k, v in p.items() if k in PROMPT_SIZES}
    one = str(clip.get("prompt") or "").strip()
    return {"keskiko": one} if one else {}


def check_presets(presets: Any, clips: list) -> list[str]:
    """The presets are this job's own look, not a shelf. Optional: none at all is fine."""
    bad: list[str] = []
    if presets is None or presets == []:
        return bad
    if not isinstance(presets, list):
        return [f"'presetit' pitää olla lista, ei {type(presets).__name__}."]
    ids: list[str] = []
    for p in presets:
        if not isinstance(p, dict):
            bad.append("preset ei ole olio.")
            continue
        pid = str(p.get("id") or "").strip()
        if not pid:
            bad.append(f"presetiltä {p.get('nimi')!r} puuttuu 'id'.")
        ids.append(pid)
        for field in ("nimi", "look", "aani", "miksi"):
            if not str(p.get(field) or "").strip():
                bad.append(f"preset {pid or '?'}: '{field}' on tyhjä.")
        # "Ei kiinteitä nimiä kuten 'Pimeä'": a preset that would fit any job was not made for this one.
        for field in ("id", "nimi"):
            if str(p.get(field) or "").strip().casefold() in _GENERIC_PRESETS:
                bad.append(
                    f"preset {pid or '?'}: {field} on {p.get(field)!r} — geneerinen nimi kelpaisi mihin "
                    "tahansa työhön. Presetin pitää olla tämän työn oma paikka."
                )
    dupes = sorted({i for i in ids if ids.count(i) > 1 and i})
    if dupes:
        bad.append(f"preset-id toistuu: {dupes} — klippi viittaa niihin, joten niiden pitää olla eri.")
    known = {i for i in ids if i}
    for c in clips if isinstance(clips, list) else []:
        ref = str((c or {}).get("preset") or "").strip() if isinstance(c, dict) else ""
        if ref and ref not in known:
            bad.append(f"klippi {c.get('id')}: preset {ref!r} ei ole 'presetit'-listassa.")
    return bad


def _check_prompt_size(cid: str, size: str, text: str) -> list[str]:
    """Is this version actually its own size? Four near-identical prompts are not the experiment."""
    bad: list[str] = []
    n_words, n_chars = len(text.split()), len(text)
    if size in _WORDS:
        lo, hi = _WORDS[size]
        if not (lo <= n_words <= hi):
            bad.append(f"klippi {cid}: promptit.{size} on {n_words} sanaa — pitää olla {lo}–{hi}.")
    if size in _CHARS:
        lo, hi = _CHARS[size]
        if n_chars < lo:
            bad.append(
                f"klippi {cid}: promptit.{size} on {n_chars} merkkiä, pitää olla vähintään {lo}. "
                "Lisää sitä mitä kuvassa EI näy — materiaali, ilma, äänen kerrokset, mikä pysyy "
                "paikallaan — älä toista sitä mikä näkyy."
            )
        elif n_chars > hi:
            bad.append(f"klippi {cid}: promptit.{size} on {n_chars} merkkiä — yli tavoitteen {hi}.")
    if n_chars >= _IMAGINE_MAX_CHARS:
        bad.append(f"klippi {cid}: promptit.{size} on {n_chars} merkkiä — Imaginen kova raja on {_IMAGINE_MAX_CHARS}.")
    return bad


def check_clips(clips: list, shots: list[dict], sizes: tuple[str, ...] = PROMPT_SIZES) -> list[str]:
    """The four things that break the app, plus the prompt rules the research settled.

    `sizes` is which prompt versions must be present — the long two are written in a second pass, so
    the first pass is checked against the short two rather than told it is missing what nobody has
    written yet.
    """
    bad: list[str] = []
    if not isinstance(clips, list) or not clips:
        return ["klippejä ei tullut yhtään."]

    # 1. ids are shot numbers joined by a hyphen, and cover every shot exactly once.
    want = [int(s["nro"]) for s in shots if isinstance(s, dict) and s.get("nro") is not None]
    seen: list[int] = []
    for c in clips:
        if not isinstance(c, dict):
            bad.append("klippi ei ole olio.")
            continue
        nums = [int(n) for n in (c.get("kohtaukset") or []) if str(n).strip().lstrip("-").isdigit()]
        if c.get("id") != clip_id(nums):
            bad.append(f"klipin id on {c.get('id')!r} mutta kohtaukset ovat {nums} — id on numerot yhdysviivalla.")
        seen += nums
    if sorted(seen) != sorted(want):
        missing, extra = sorted(set(want) - set(seen)), sorted(set(seen) - set(want))
        bad.append(
            f"klipit eivät kata kohtauksia kertaalleen — puuttuu {missing or '-'}, ylimääräisiä {extra or '-'}"
            + (f", toistuu {sorted(n for n in set(seen) if seen.count(n) > 1)}" if len(seen) != len(set(seen)) else "")
        )

    by_nro = {int(s["nro"]): s for s in shots if isinstance(s, dict) and s.get("nro") is not None}
    for c in clips:
        if not isinstance(c, dict):
            continue
        cid = c.get("id")
        nums = [int(n) for n in (c.get("kohtaukset") or []) if str(n).strip().lstrip("-").isdigit()]
        recorded_shots = [
            n for n in nums if str(by_nro.get(n, {}).get("kuvakoko") or "").casefold() == SCREEN_RECORDING
        ]

        # 2. a screen recording is recorded, never generated — and the two never share a clip.
        if recorded_shots and len(recorded_shots) != len(nums):
            bad.append(f"klippi {cid}: nauhoitettavaa ja generoitavaa samassa klipissä ({nums}).")
        if recorded_shots:
            if c.get("tyyppi") != "nauhoita":
                bad.append(f"klippi {cid}: ruutukaappaus on aina tyyppi 'nauhoita', ei {c.get('tyyppi')!r}.")
            if not str(c.get("nauhoitusohje") or "").strip():
                bad.append(f"klippi {cid}: nauhoitettavalta puuttuu 'nauhoitusohje'.")
            if prompts_of(c):
                bad.append(f"klippi {cid}: nauhoitettavalle ei kirjoiteta promptia — se kuvataan, ei generoida.")
        elif c.get("tyyppi") != "generoi":
            bad.append(f"klippi {cid}: tyyppi on {c.get('tyyppi')!r}, pitäisi olla 'generoi'.")
        else:
            # One line per missing size, not one listing them: every message about a version is then
            # addressed `promptit.<size>`, which is what lets pass 2 be handed exactly its own two.
            for size in [s for s in sizes if not prompts_of(c).get(s, "").strip()]:
                bad.append(
                    f"klippi {cid}: promptit.{size} puuttuu — jokaisesta generoitavasta klipistä "
                    f"kirjoitetaan kaikki neljä versiota ({', '.join(PROMPT_SIZES)})."
                )

        # 3. the length is one Imagine offers, and not the ceiling by reflex.
        if c.get("grok_kesto_s") not in GROK_DURATIONS:
            bad.append(f"klippi {cid}: grok_kesto_s on {c.get('grok_kesto_s')!r} — sallitut {GROK_DURATIONS}.")

        # 4. the prompt rules the research settled — every version, because a person runs every version.
        cuts_only = all(str(by_nro.get(n, {}).get("liike") or "").casefold() == _CUT for n in nums)
        stills_only = all(str(by_nro.get(n, {}).get("liike") or "").casefold() == "still" for n in nums)
        speaks = any(str(by_nro.get(n, {}).get("puhe") or "").strip() for n in nums)
        for size, prompt in sorted(prompts_of(c).items(), key=lambda kv: PROMPT_SIZES.index(kv[0])):
            if not prompt.strip():
                continue
            where = f"klippi {cid} / {size}"
            bad += _check_prompt_size(str(cid), size, prompt)
            if _FAKE_LOCK.search(prompt):
                bad.append(
                    f"{where}: prompti sanoo 'locked/stable/steady' kamerasta — ne luetaan liikkeeksi. "
                    f"Kirjoita '{_LOCK_PHRASE.capitalize()}.'"
                )
            if _ASKS_FOR_TEXT.search(prompt):
                bad.append(f"{where}: prompti pyytää luettavaa tekstiä ruutuun — se on artefakti, ei ohje.")
            for n in nums:
                rt = str(by_nro.get(n, {}).get("ruututeksti") or "").strip()
                if rt and rt.casefold() in prompt.casefold():
                    bad.append(
                        f"{where}: ruututeksti {rt!r} on promptissa — se kuuluu ruututeksti_jalkikateen-kenttään."
                    )
            if _opens_with_style(prompt, cuts_only):
                bad.append(
                    f"{where}: prompti alkaa tyylillä tai kameralla eikä teolla — teko ensimmäiseen "
                    f"lauseeseen: {_first_sentence(prompt)!r}"
                )
            if stills_only and _LOCK_PHRASE not in prompt.casefold():
                bad.append(f"{where}: staattinen kohtaus ilman '{_LOCK_PHRASE.capitalize()}.' — kamera ajautuu.")
            # 8-25 words carry motion, one camera and the sound — not a spoken line. Everything longer
            # carries the line, and it is the SCRIPT's line: a model that translates or paraphrases it
            # makes a clip that says something the person never wrote, and the voice is generated in
            # the same pass, so there is no fixing it in the edit.
            if size in _TOO_SHORT_FOR_SPEECH:
                continue
            for n in nums:
                said = _said(by_nro.get(n, {}).get("puhe"))
                if said and said not in _said(prompt):
                    bad.append(
                        f"{where}: kohtauksen {n} puhe ei ole promptissa sellaisenaan — lainaa se "
                        f'sanatarkasti äläkä käännä: "{str(by_nro[n]["puhe"]).strip()}"'
                    )
            if "sound:" not in prompt.casefold() and any(
                str(by_nro.get(n, {}).get("aani") or "").casefold() == "puhe" for n in nums
            ):
                bad.append(f"{where}: puheellisessa klipissä ei ole 'Sound:'-lohkoa.")
            if speaks and "no music" not in prompt.casefold():
                bad.append(f"{where}: puhetta on mutta 'no music' puuttuu — malli lisää muuten scoren omin päin.")

        # settings the app reads straight off the clip
        if c.get("tunnelma") not in TUNNELMAT:
            bad.append(f"klippi {cid}: tunnelma on {c.get('tunnelma')!r} — sallitut {TUNNELMAT}.")
        if not isinstance(c.get("aani"), bool):
            bad.append(f"klippi {cid}: 'aani' on tosi/epätosi, ei {c.get('aani')!r}.")
        if not isinstance(c.get("kielto"), bool):
            bad.append(f"klippi {cid}: 'kielto' on tosi/epätosi, ei {c.get('kielto')!r}.")
        if c.get("kuva") not in KUVA_MODES:
            bad.append(f"klippi {cid}: 'kuva' on {c.get('kuva')!r} — sallitut {KUVA_MODES}.")
        if not str(c.get("miksi") or "").strip():
            bad.append(f"klippi {cid}: 'miksi' on tyhjä — ihminen lukee sen kun päättää ajaako uusiksi.")

        # `imagine` is what a person CLICKS in Imagine, so it belongs to the clips that are run
        # there. A recording is filmed off a real screen; on the first live run the model filled its
        # settings with 'reference-to-video', which is not merely useless but an instruction to do
        # the one thing the shot list forbids — generate the product's UI.
        im = c.get("imagine")
        if recorded_shots:
            if im is not None:
                bad.append(f"klippi {cid}: nauhoitettavaa ei ajeta Imaginessa, joten sillä ei ole 'imagine'-asetuksia.")
            continue
        if not isinstance(im, dict):
            bad.append(f"klippi {cid}: 'imagine' puuttuu — ihmisen ei pidä johtaa asetuksia promptista.")
            continue
        if im.get("tila") not in MODES:
            bad.append(f"klippi {cid}: imagine.tila on {im.get('tila')!r} — sallitut {MODES}.")
        if im.get("tila") == "reference-to-video" and str(im.get("tarkkuus") or "") not in ("480p", REFERENCE_MAX_RES):
            bad.append(f"klippi {cid}: referenssitilan katto on {REFERENCE_MAX_RES}, ei {im.get('tarkkuus')!r}.")
        if str(im.get("kesto") or "").rstrip("s") != str(c.get("grok_kesto_s")):
            bad.append(
                f"klippi {cid}: imagine.kesto {im.get('kesto')!r} ei vastaa grok_kesto_s {c.get('grok_kesto_s')!r}."
            )
    return bad


# ── the prompt ───────────────────────────────────────────────────────────────────────────────────
def _shots_block(clips: list[dict]) -> str:
    rows = []
    for c in clips:
        head = (
            f"KLIPPI {c['id']} — tyyppi {c['tyyppi']}, sisältöä {c['kesto_s']} s, "
            f"ehdotettu grok_kesto_s {c['grok_kesto_s']}"
            + (f", KUVA aloitusruuduksi: {c['kuva_url']}" if c.get("kuva_url") else "")
        )
        rows.append(head)
        for s in c["_shots"]:
            rows.append(
                f"  kohtaus {s.get('nro')}: [{s.get('kuvakoko')}] {s.get('kuvassa')}\n"
                f"     liike: {s.get('liike')} | ääni: {s.get('aani')} | puhe: {s.get('puhe')!r}"
                + (
                    f" | ruututeksti (EI promptiin): {s.get('ruututeksti')!r}"
                    if str(s.get("ruututeksti") or "").strip()
                    else ""
                )
            )
    return "\n".join(rows)


def build_prompt(clips: list[dict], valinta: dict, tilaus: dict, video: dict) -> str:
    """PASS 1: the run's shape — presets and each clip's settings. NO prompts.

    The prompts are asked per clip afterwards, because the answer and the model's THINKING share one
    token budget. Measured on z-ai/glm-5.3-flash: one uncapped call spent 10 034 of 14 693 completion
    tokens on reasoning, so under our 16 384 cap a whole-run answer had ~1 800 tokens left and stopped
    mid-string at 6 025 characters — valid JSON that simply ended. Asking for less per call is the fix
    that does not depend on guessing how much a model will think.
    """
    angle = (valinta.get("kulma") or {}) if isinstance(valinta.get("kulma"), dict) else {}
    aspect = str(video.get("muoto") or "9:16")
    return (
        "Olet Grok Imagine -skriptaaja. Kohtausluettelo on JO olemassa etkä keksi tarinaa: käännät "
        "sen klipeiksi ja asetuksiksi, jotka ihminen vie Imagineen sellaisenaan.\n\n"
        "TÄSSÄ VAIHEESSA ET KIRJOITA YHTÄKÄÄN PROMPTIA. Ne tilataan klippi kerrallaan erikseen. Nyt "
        "päätät presetit ja kunkin klipin asetukset.\n\n"
        "PITUUS: grok_kesto_s on 6, 10 tai 15 — PIENIN joka kantaa sisällön. Fysiikka ja äänen rytmi "
        "pitävät parhaiten 5–8 sekunnissa; 12–15 s ajautuu. Älä pyöristä kattoon.\n\n"
        "PRESETIT suunnittelet TÄLLE TYÖLLE tilauksen tyyleistä, valitusta kulmasta ja taustasta. "
        "Ei kiinteitä nimiä kuten 'Pimeä': tämän työn preset on tämän työn paikka, ja jos preset "
        "kelpaisi mihin tahansa työhön, sitä ei ole tehty tästä. Kenttä 'look' on valo ja materiaali, "
        "'aani' äänimaisema, 'miksi' yksi lause siitä miksi tämä työ näyttää tältä. Klippi viittaa "
        "yhteen kentällä 'preset', ja useampi klippi SAA käyttää samaa — se on niiden tarkoitus.\n\n"
        "RUUTUTEKSTI ei mene promptiin vaan kenttään 'ruututeksti_jalkikateen' editoria varten: "
        "luettava teksti ruudussa on dokumentoitu artefakti, ei ohje.\n\n"
        "'kielto' on oletuksena false, eli positiivinen nimeäminen ('Keep the subject, the framing "
        "and the horizon unchanged.'). Jos poikkeat, perustele 'miksi'-kentässä.\n\n"
        "NAUHOITETTAVAT (tyyppi 'nauhoita') ovat ruutukaappauksia: kirjoita niille 'nauhoitusohje' — "
        "mitä ruudulla tehdään, missä järjestyksessä ja millä tahdilla. Niitä ei ajeta Imaginessa "
        "lainkaan, joten ÄLÄ kirjoita niille 'imagine'-lohkoa äläkä presettiä.\n\n"
        f"KULMA: {angle.get('kulma') or '-'}\n"
        f"AVAUS: {angle.get('avaus') or '-'}\n"
        f"KENELLE: {angle.get('kenelle') or '-'}\n"
        f"TYYLIT: {tilaus.get('tyylit') or tilaus.get('tyyli') or '-'}\n"
        f"KUVASUHDE: {aspect}\n\n"
        "KLIPIT (ryhmittely on jo tehty, ÄLÄ muuta id:itä äläkä kohtausnumeroita):\n"
        + _shots_block(clips)
        + FINNISH_NATIVE_STYLE
        + "\n\nVASTAUKSEN MUOTO — pelkkä JSON-olio. Palauta KAIKKI klipit, samoilla id:illä:\n"
        '{"asetukset": {"tarkkuus": "720p", "kuvasuhde": "' + aspect + '", "ketjutus": false},\n'
        ' "presetit": [{"id": "tyonoma-tunnus", "nimi": "Työn oma nimi",\n'
        '   "look": "valo ja materiaali englanniksi, yksi tiivis lause",\n'
        '   "aani": "äänimaisema englanniksi", "miksi": "yksi lause suomeksi: miksi tämä työ näyttää tältä"}],\n'
        ' "klipit": [{"id": "5-6", "kohtaukset": [5,6], "tyyppi": "generoi", "kesto_s": 12,\n'
        '   "grok_kesto_s": 10, "aani": true, "tunnelma": "pimea", "kielto": false,\n'
        '   "kuva": "ensimmainen_ruutu", "kuva_url": "…", "preset": "tyonoma-tunnus",\n'
        '   "imagine": {"tila": "image-to-video", "kesto": "10s", "tarkkuus": "720p",\n'
        '     "kuvasuhde": "' + aspect + '", "aani": "paalla", "liite": "mitä kuvalle tehdään ja miksi"},\n'
        '   "ruututeksti_jalkikateen": ["…"], "miksi": "yksi lause"}]}\n'
        f"tunnelma: {' | '.join(t or '(tyhjä)' for t in TUNNELMAT)}   tila: {' | '.join(MODES)}"
    )


_PROMPT_RULES = (
    "SÄÄNNÖT, jotka on testattu — nämä tarkistetaan koneellisesti:\n"
    f"- Teko ENSIMMÄISESSÄ lauseessa. Malli on peräkkäinen: viimeisen lauseen huippukohta ei ehdi "
    "tapahtua. Järjestys on aikajärjestys: [teko] [yksi kameraliike] [Look: valo ja materiaali] "
    "[Sound: konkreettinen] [mikä ei muutu].\n"
    f"- Staattinen kohtaus: kirjoita '{_LOCK_PHRASE.capitalize()}.' ÄLÄ 'locked', 'stable camera' "
    "tai 'steady shot' — ne luetaan sulavaksi liikkeeksi ja kamera ajautuu.\n"
    "- ÄLÄ pyydä luettavaa tekstiä ruutuun. Ruututeksti EI mene promptiin.\n"
    "- Ääni: 'Sound:' (ei 'AUDIO:'), konkreettinen materiaaliääni ('rain on glass'), ei 'city "
    "sounds'. Kun puhetta on, lisää 'no music' — malli lisää muuten scoren omin päin.\n"
    "- Puhe lainausmerkeissä ja sävy perään, ei referoituna: referoitu puhe tuottaa muminaa. "
    "Repliikki on kohtauksen 'puhe' SANATARKASTI — ääni syntyy samassa ajossa kuin kuva, joten "
    "väärä repliikki ei ole korjattavissa editissä.\n"
    "- Yksi nimetty kameraliike per klippi.\n"
    "- Intensiteettisanat toimivat ('fully', 'with tremendous force', 'then faster'), ja ilmasanat "
    "luetaan liikkeeksi ('wind whipping', 'heat shimmer').\n"
    "- Ihmiset kuvassa: leveämpi kuva ja hitaampi liike. Tiukka lähikuva + nopea liike sotkee kasvot.\n"
)


def _clip_brief(clip: dict, preset: dict | None, angle: dict) -> str:
    """The one clip a prompt call is about: its shots, its look, the angle it serves."""
    shots = "\n".join(
        f"  kohtaus {s.get('nro')}: [{s.get('kuvakoko')}] {s.get('kuvassa')}\n"
        f"     liike: {s.get('liike')} | ääni: {s.get('aani')} | puhe: {s.get('puhe')!r}"
        for s in clip.get("_shots") or []
    )
    return (
        f"KOHTAUKSET:\n{shots}\n\n"
        + (
            f"PRESET '{preset.get('nimi')}' — look: {preset.get('look')} | ääni: {preset.get('aani')}\n\n"
            if preset
            else ""
        )
        + (f"KULMA: {angle.get('kulma') or '-'}\nKENELLE: {angle.get('kenelle') or '-'}\n\n" if angle else "")
        + (
            "KUVA ON ALOITUSRUUTUNA, joten ÄLÄ kuvaile sitä uudestaan.\n\n"
            if clip.get("kuva") == "ensimmainen_ruutu"
            else ""
        )
    )


def build_short_prompt(clip: dict, preset: dict | None, angle: dict) -> str:
    """PASS 2: the two SHORT versions of ONE clip."""
    lo_s, hi_s = _WORDS["lyhyt"]
    lo_k, hi_k = _WORDS["keskiko"]
    return (
        f"Kirjoitat KLIPIN {clip['id']} kaksi LYHYTTÄ promptiversiota Grok Imagineen. Kohtaus on jo "
        "kirjoitettu etkä keksi tarinaa — käännät sen.\n\n"
        + _clip_brief(clip, preset, angle)
        + _PROMPT_RULES
        + "\nNELJÄ VERSIOTA, JOISTA NYT KAKSI: ne eivät ole vaihtoehtoja joista valitaan paras, vaan "
        "koe. Ihminen ajaa saman kohtauksen kaikilla neljällä ja katsoo missä kohtaa lisäteksti alkaa "
        "auttaa ja missä se alkaa hukuttaa.\n"
        f"- 'lyhyt': {lo_s}–{hi_s} SANAA. VAIN se mitä kuva ei kerro: liike, yksi kamera, ääni. "
        "EI repliikkiä — se ei mahdu tähän mittaan.\n"
        f"- 'keskiko': {lo_k}–{hi_k} SANAA, 2–3 virkettä. Subjekti, teko, kamera, valo, 'Sound:' ja "
        "repliikki sanatarkasti.\n\n"
        "VASTAA pelkällä JSON-oliolla:\n"
        '{"lyhyt": "…", "keskiko": "…"}'
    )


def build_long_prompt(clip: dict, preset: dict | None, angle: dict, aspect: str) -> str:
    """PASS 3: the two long versions of ONE clip.

    Asked per clip rather than for the whole run at once, because `massiivinen` is ~3000 characters
    and a single response carrying several of them is where a model runs out of budget — and a
    truncated JSON costs the whole run, not one clip. The short versions are handed back in so the
    long ones stay the SAME shot rather than four independent inventions.
    """
    have = prompts_of(clip)
    lo_l, hi_l = _CHARS["laaja"]
    lo_m, hi_m = _CHARS["massiivinen"]
    return (
        f"Kirjoitat KLIPIN {clip['id']} kaksi PITKÄÄ promptiversiota Grok Imagineen. Lyhyet versiot "
        "ovat jo olemassa, ja pitkät ovat SAMA kohtaus — ei uutta tarinaa, ei uutta kohtausta.\n\n"
        f"LYHYT (on jo): {have.get('lyhyt', '-')}\n"
        f"KESKIKO (on jo): {have.get('keskiko', '-')}\n\n"
        + _clip_brief(clip, preset, angle)
        + "MITÄ PITUUS TARKOITTAA — tämä on koko tehtävä:\n"
        f"- 'laaja': {lo_l}–{hi_l} MERKKIÄ. Yli minkään suosituksen, tarkoituksella. Materiaalit, "
        "valon suunta ja laatu, mitä taustalla tapahtuu, äänen kerrokset, mikä ei saa muuttua.\n"
        f"- 'massiivinen': noin {lo_m}–{hi_m} MERKKIÄ. Niin paljon kuin kohtauksesta on sanottavaa. "
        f"Kova raja on {_IMAGINE_MAX_CHARS}; älä mene siihen kiinni.\n\n"
        "SÄÄNTÖ JOKA TEKEE PITUUDESTA MERKITYKSELLISEN: pitkissä versioissa lisätään sitä mitä "
        "kuvassa EI näy — materiaali, ilma, äänen kerrokset, mikä pysyy paikallaan — EI toistoa "
        "siitä mikä näkyy. Muuten 3000 merkkiä on vain sama lause neljästi, eikä koe mittaa mitään.\n\n"
        "Samat säännöt kuin lyhyissä: teko ENSIMMÄISESSÄ lauseessa, "
        f"'{_LOCK_PHRASE.capitalize()}.' staattiseen (ei 'locked'/'stable'/'steady'), 'Sound:'-lohko, "
        "'no music' kun puhetta on, repliikki SANATARKASTI kohtauksen 'puhe'-kentästä, eikä koskaan "
        "pyyntöä luettavasta tekstistä ruutuun.\n\n"
        "VASTAA pelkällä JSON-oliolla:\n"
        '{"laaja": "…", "massiivinen": "…"}'
    )


def check_prompt_sizes(clip: dict, shots: list[dict], sizes: tuple[str, ...]) -> list[str]:
    """Only what THESE versions of this clip are answerable for.

    Scoped deliberately: a prompt call must not be handed a complaint about a setting it was never
    asked to write, or it would rewrite prose to answer a note about `imagine.tila` and never
    converge. Matched on the message's own markers (`klippi X / laaja:`, `promptit.laaja`) rather
    than the bare word, because `laaja` is also a SHOT SIZE in the script.
    """
    marks = tuple(m for s in sizes for m in (f"/ {s}", f"promptit.{s}"))
    return [v for v in check_clips([clip], shots) if any(m in v for m in marks)]


def _written(prompts: dict, sizes: tuple[str, ...]) -> int:
    """How many of the asked-for versions actually carry text."""
    return sum(1 for s in sizes if str(prompts.get(s) or "").strip())


def _more_written(trial: dict, current: dict, sizes: tuple[str, ...]) -> bool:
    return _written(trial, sizes) > _written(current, sizes)


# ── the run ──────────────────────────────────────────────────────────────────────────────────────
def _merge(planned: dict, written: dict) -> dict:
    """The model's words on top of the plan's facts. Grouping, ids and the image never move."""
    out = {k: v for k, v in written.items() if k not in ("id", "kohtaukset", "tyyppi", "kesto_s", "kuva_url")}
    out.update(
        {
            "id": planned["id"],
            "kohtaukset": planned["kohtaukset"],
            "tyyppi": planned["tyyppi"],
            "kesto_s": planned["kesto_s"],
        }
    )
    if planned.get("kuva_url"):
        out["kuva_url"] = planned["kuva_url"]
    # Normalise an older single `prompt` into the four-version shape, so one field carries the prompts.
    got = prompts_of(out)
    out.pop("prompt", None)
    out["promptit"] = {s: got[s] for s in PROMPT_SIZES if got.get(s, "").strip()}
    if planned["tyyppi"] == "nauhoita":
        out.pop("promptit", None)  # a recording is filmed, never generated
        out.pop("imagine", None)  # …and never run in Imagine, so it has nothing to select there
        out.pop("preset", None)  # …and its look is the real screen's, not one we designed
        out["kuva"], out["kuva_url"] = "ei", ""
    return out


def tee_grok(agent_name: str, task: dict | None = None, task_id: str | None = None) -> str:
    """Turn this run's shot list into Grok Imagine clips. Returns a report.

    The address comes from `run_address` — the dispatch's `deliverable_key` used verbatim (rule 1),
    else the run's variables, else today's date. Never constructed here.
    """
    key, ref, rule = run_address(task, "grok")
    addr = f" Address: {rule}."
    print(f"[{agent_name}] grok -> {key} ({rule})", file=sys.stderr)
    try:
        video, kuvat, valinta, tilaus = read_inputs(agent_name, ref)
    except LookupError as exc:
        print(f"[{agent_name}] {exc}", file=sys.stderr)
        return f"FAILED: {exc}{addr}"

    shots = [s for s in video["kohtaukset"] if isinstance(s, dict) and s.get("nro") is not None]
    planned = plan_clips(shots, kuvat)
    print(
        f"[{agent_name}] {len(shots)} kohtausta -> {len(planned)} klippiä "
        f"({sum(1 for c in planned if c['tyyppi'] == 'nauhoita')} nauhoita / "
        f"{sum(1 for c in planned if c['tyyppi'] == 'generoi')} generoi)",
        file=sys.stderr,
    )

    llm = get_llm(for_tool_use=False, temperature=0.6, agent_name=agent_name)

    # PASS 1: the grouping is already decided, so this is settings and presets — and NO prompts.
    # Every prompt is asked per clip below, because the answer and the model's thinking share one
    # token budget: a whole-run answer stopped mid-string at 6025 characters (see build_prompt).
    base = build_prompt(planned, valinta, tilaus, video)
    prompt, clips, presets, violations = base, None, [], ["(no attempt ran)"]
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        doc = parse_json_object(llm.call([{"role": "user", "content": prompt}]))
        if doc is None:
            got_clips, got_presets, tried = None, [], ["vastaus ei ollut JSON-olio."]
        else:
            written = {str(c.get("id")): c for c in (doc.get("klipit") or []) if isinstance(c, dict)}
            got_clips = [_merge(p, written.get(p["id"], {})) for p in planned]
            got_presets = doc.get("presetit") if isinstance(doc.get("presetit"), list) else []
            tried = check_clips(got_clips, shots, sizes=()) + check_presets(got_presets, got_clips)
        print(
            f"[{agent_name}] grok pass 1 attempt {attempt}/{_MAX_ATTEMPTS}: "
            + ("OK" if got_clips is not None and not tried else "; ".join(tried[:4])),
            file=sys.stderr,
        )
        # A RETRY MUST NEVER END WORSE THAN ITS BEST ATTEMPT. On the first live run of the four-prompt
        # shape, attempt 2 produced a complete structure that was one word over on one prompt, and
        # attempt 3 came back as prose — which overwrote it with None and failed the whole run. Work
        # that was already in hand cannot be destroyed by a later flaky answer.
        if got_clips is not None and (clips is None or len(tried) < len(violations)):
            clips, presets, violations = got_clips, got_presets, tried
        if clips is not None and not violations:
            break
        prompt = base + "\n\nKorjaa nämä ja kirjoita koko JSON uudestaan:\n" + "\n".join(f"- {v}" for v in tried)
    if clips is None:
        return f"FAILED: the clips could not be read as JSON in any of {_MAX_ATTEMPTS} attempts. Nothing was written to {key}.{addr}"

    # PASSES 2 and 3: the prompts, one clip and one pair of sizes at a time. `massiivinen` alone is
    # ~3000 characters, and one response carrying every clip's would run out of budget again — here a
    # failed call costs one pair on one clip, and everything already written stays.
    by_preset = {str(p.get("id")): p for p in presets if isinstance(p, dict)}
    angle = (valinta.get("kulma") or {}) if isinstance(valinta.get("kulma"), dict) else {}
    aspect = str(video.get("muoto") or "9:16")
    plan_by_id = {p["id"]: p for p in planned}
    short = tuple(s for s in PROMPT_SIZES if s not in _LONG_SIZES)
    for clip in [c for c in clips if c["tyyppi"] == "generoi"]:
        with_shots = {**clip, "_shots": plan_by_id[clip["id"]]["_shots"]}
        preset = by_preset.get(str(clip.get("preset") or ""))
        for pass_no, sizes in ((2, short), (3, _LONG_SIZES)):
            # The long call is built AFTER the short one lands, so it can quote it back — the long
            # versions are the same shot at more length, not a fresh invention.
            with_shots["promptit"] = clip.get("promptit", {})
            asked = (
                build_short_prompt(with_shots, preset, angle)
                if sizes == short
                else build_long_prompt(with_shots, preset, angle, aspect)
            )
            ask, best = asked, check_prompt_sizes(clip, shots, sizes)
            for attempt in range(1, _MAX_ATTEMPTS + 1):
                doc = parse_json_object(llm.call([{"role": "user", "content": ask}])) or {}
                got = {s: str(doc.get(s) or "") for s in sizes if str(doc.get(s) or "").strip()}
                trial = {**clip, "promptit": {**clip.get("promptit", {}), **got}}
                missed = check_prompt_sizes(trial, shots, sizes)
                sizes_txt = " ".join(f"{s}={len(got.get(s, ''))}" for s in sizes)
                print(
                    f"[{agent_name}] grok pass {pass_no} {clip['id']} attempt {attempt}/{_MAX_ATTEMPTS} "
                    f"({sizes_txt} merkkiä): " + ("OK" if not missed else "; ".join(missed[:2])),
                    file=sys.stderr,
                )
                # Same rule as pass 1: take the new answer only when it is actually better. But
                # "better" is not just a smaller violation count — an EMPTY slot violates once (the
                # version is missing) exactly like a written one that bends a style rule, so a strict
                # count comparison would keep nothing over prose a person could fix in five seconds.
                # More versions written wins first; only then does the violation count decide.
                if _more_written(trial["promptit"], clip.get("promptit", {}), sizes) or (
                    _written(trial["promptit"], sizes) == _written(clip.get("promptit", {}), sizes)
                    and len(missed) < len(best)
                ):
                    clip["promptit"], best = trial["promptit"], missed
                if not best:
                    break
                ask = asked + "\n\nKorjaa nämä ja kirjoita molemmat uudestaan:\n" + "\n".join(f"- {m}" for m in missed)

    violations = check_clips(clips, shots) + check_presets(presets, clips)

    # A prompt-rule violation is a note for the person, not a reason to discard the work (d03cce3) —
    # nothing here publishes. A clip with nothing to paste IS unusable, and that still fails.
    unusable = [
        c["id"]
        for c in clips
        if (c["tyyppi"] == "generoi" and not any(t.strip() for t in prompts_of(c).values()))
        or (c["tyyppi"] == "nauhoita" and not str(c.get("nauhoitusohje") or "").strip())
    ]
    if unusable:
        return (
            f"FAILED: clip(s) {', '.join(unusable)} have nothing for a person to paste or shoot after "
            f"{_MAX_ATTEMPTS} attempts — " + "; ".join(violations[:3]) + f". Nothing was written to {key}.{addr}"
        )

    value: dict[str, Any] = {
        "asetukset": {
            "tarkkuus": REFERENCE_MAX_RES
            if any((c.get("imagine") or {}).get("tila") == "reference-to-video" for c in clips)
            else "720p",
            "kuvasuhde": aspect,
            "ketjutus": any(c.get("kuva") == "ei" and c["tyyppi"] == "generoi" for c in clips),
        },
        "presetit": [p for p in presets if isinstance(p, dict)],
        "klipit": clips,
    }
    if violations:
        value["rikkeet"] = list(violations)
        print(f"[{agent_name}] stored WITH {len(violations)} rule(s) unmet", file=sys.stderr)

    written_ok = _aimeat_call(
        agent_name,
        "aimeat_memory_write",
        {
            "key": key,
            "value": value,
            "visibility": "owner",
            "tags": ["julkaisupoyta", "grok", f"ref:{ref}"],
            "ai_provenance": declare(
                Level.SYNTHESIZED,
                method=Method.SYNTHESIZED,
                human_involvement=HumanInvolvement.NONE,
                model=resolved_model(llm),
                provider=resolved_provider(),
                notes="KANSI: Grok Imagine clips prepared from an existing shot list; nothing published.",
            ),
        },
    )
    if written_ok is None:
        return f"FAILED to write '{key}' (tunnel/transport) — the clips did not land.{addr}"
    record_deliverable_key(task_id, key)
    rec = sum(1 for c in clips if c["tyyppi"] == "nauhoita")
    gen = [c for c in clips if c["tyyppi"] == "generoi"]
    bent = f" {len(violations)} sääntöä jäi täyttymättä — merkitty kohtaan 'rikkeet'." if violations else ""
    # The person reads this to decide whether to run the experiment, so it says what they get: how
    # many prompts landed, and the preset names — which are the part only they can judge as "made
    # for this job".
    sizes = ", ".join(
        f"{s} {min((len(prompts_of(c).get(s, '')) for c in gen), default=0)}–"
        f"{max((len(prompts_of(c).get(s, '')) for c in gen), default=0)} merkkiä"
        for s in PROMPT_SIZES
    )
    names = ", ".join(str(p.get("nimi") or p.get("id")) for p in presets if isinstance(p, dict)) or "ei yhtään"
    return (
        f"OK: {len(clips)} klippiä ({rec} nauhoita / {len(gen)} generoi), "
        f"{sum(len(prompts_of(c)) for c in gen)} promptia -> {key}. "
        f"Pituudet: {sizes}. Presetit: {names}. Ei julkaise mitään, valmistelee.{bent}{addr}"
    )


def make_grok_tools(agent_name: str, task: dict | None = None, prompt: str | None = None) -> list:
    """The scriptwriter's ONE tool. The address and the clip grouping are resolved in code."""
    from crewai.tools import tool

    task_id = (task or {}).get("id")

    @tool("tee_grok")
    def tee_grok_tool() -> str:
        """Turn this run's finished shot list into Grok Imagine clips with the settings to select,
        and store them at the key THIS RUN WAS GIVEN. Takes no arguments. Call it EXACTLY ONCE and
        report what it returns verbatim, including any FAILED line."""
        return tee_grok(agent_name, task=task, task_id=task_id)

    tee_grok_tool.cache_function = lambda *_a, **_k: False
    return [tee_grok_tool]

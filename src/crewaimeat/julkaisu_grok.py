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


def check_clips(clips: list, shots: list[dict]) -> list[str]:
    """The four things that break the app, plus the prompt rules the research settled."""
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
            if str(c.get("prompt") or "").strip():
                bad.append(f"klippi {cid}: nauhoitettavalle ei kirjoiteta promptia — se kuvataan, ei generoida.")
        elif c.get("tyyppi") != "generoi":
            bad.append(f"klippi {cid}: tyyppi on {c.get('tyyppi')!r}, pitäisi olla 'generoi'.")
        elif not str(c.get("prompt") or "").strip():
            bad.append(f"klippi {cid}: generoitavalta puuttuu 'prompt'.")

        # 3. the length is one Imagine offers, and not the ceiling by reflex.
        if c.get("grok_kesto_s") not in GROK_DURATIONS:
            bad.append(f"klippi {cid}: grok_kesto_s on {c.get('grok_kesto_s')!r} — sallitut {GROK_DURATIONS}.")

        # 4. the prompt rules the research settled.
        prompt = str(c.get("prompt") or "")
        if prompt:
            if _FAKE_LOCK.search(prompt):
                bad.append(
                    f"klippi {cid}: prompti sanoo 'locked/stable/steady' kamerasta — ne luetaan liikkeeksi. "
                    f"Kirjoita '{_LOCK_PHRASE.capitalize()}.'"
                )
            if _ASKS_FOR_TEXT.search(prompt):
                bad.append(f"klippi {cid}: prompti pyytää luettavaa tekstiä ruutuun — se on artefakti, ei ohje.")
            for n in nums:
                rt = str(by_nro.get(n, {}).get("ruututeksti") or "").strip()
                if rt and rt.casefold() in prompt.casefold():
                    bad.append(
                        f"klippi {cid}: ruututeksti {rt!r} on promptissa — se kuuluu kenttään ruututeksti_jalkikateen."
                    )
            if _opens_with_style(
                prompt, all(str(by_nro.get(n, {}).get("liike") or "").casefold() == _CUT for n in nums)
            ):
                bad.append(
                    f"klippi {cid}: prompti alkaa tyylillä tai kameralla eikä teolla — teko ensimmäiseen "
                    f"lauseeseen: {_first_sentence(prompt)!r}"
                )
            # The spoken line is the script's, word for word. A model that translates or paraphrases it
            # produces a clip that says something the person never wrote — and the voice is generated
            # in the same pass, so there is no fixing it in the edit.
            for n in nums:
                said = _said(by_nro.get(n, {}).get("puhe"))
                if said and said not in _said(prompt):
                    bad.append(
                        f"klippi {cid}: kohtauksen {n} puhe ei ole promptissa sellaisenaan — lainaa se "
                        f'sanatarkasti äläkä käännä: "{str(by_nro[n]["puhe"]).strip()}"'
                    )
            if "sound:" not in prompt.casefold() and any(
                str(by_nro.get(n, {}).get("aani") or "").casefold() == "puhe" for n in nums
            ):
                bad.append(f"klippi {cid}: puheellisessa klipissä ei ole 'Sound:'-lohkoa.")
            if (
                any(str(by_nro.get(n, {}).get("puhe") or "").strip() for n in nums)
                and "no music" not in prompt.casefold()
            ):
                bad.append(f"klippi {cid}: puhetta on mutta 'no music' puuttuu — malli lisää muuten scoren omin päin.")
            if all(str(by_nro.get(n, {}).get("liike") or "").casefold() == "still" for n in nums) and (
                _LOCK_PHRASE not in prompt.casefold()
            ):
                bad.append(f"klippi {cid}: staattinen kohtaus ilman '{_LOCK_PHRASE.capitalize()}.' — kamera ajautuu.")

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
    angle = (valinta.get("kulma") or {}) if isinstance(valinta.get("kulma"), dict) else {}
    aspect = str(video.get("muoto") or "9:16")
    return (
        "Olet Grok Imagine -skriptaaja. Kohtausluettelo on JO olemassa etkä keksi tarinaa: käännät "
        "sen klipeiksi ja asetuksiksi, jotka ihminen vie Imagineen sellaisenaan.\n\n"
        "PROMPTIN JÄRJESTYS ON AIKAJÄRJESTYS, koska malli on peräkkäinen — viimeisen lauseen "
        "huippukohta ei ehdi tapahtua:\n"
        "  [teko, mitä tapahtuu ensin] [yksi kameraliike] [Look: valo ja materiaali] "
        "[Sound: konkreettinen] [mikä ei muutu]\n\n"
        "SÄÄNNÖT, jotka on testattu — nämä tarkistetaan koneellisesti:\n"
        f"- Staattinen kohtaus: kirjoita '{_LOCK_PHRASE.capitalize()}.' ÄLÄ 'locked', 'stable camera' "
        "tai 'steady shot' — ne luetaan sulavaksi liikkeeksi ja kamera ajautuu.\n"
        "- ÄLÄ pyydä luettavaa tekstiä ruutuun. Ruututeksti EI mene promptiin; se menee kenttään "
        "'ruututeksti_jalkikateen' editoria varten.\n"
        "- Ääni: 'Sound:' (ei 'AUDIO:'), konkreettinen materiaaliääni ('rain on glass'), ei 'city "
        "sounds'. Kun puhetta on, lisää 'no music' — malli lisää muuten scoren omin päin.\n"
        "- Puhe lainausmerkeissä ja sävy perään, ei referoituna: referoitu puhe tuottaa muminaa.\n"
        "- Puhuttu repliikki on kohtauksen 'puhe' SANATARKASTI. Älä käännä sitä äläkä muotoile "
        "uusiksi: ääni syntyy samassa ajossa, joten väärä repliikki ei ole korjattavissa editissä.\n"
        "- Yksi nimetty kameraliike per klippi. Jos kokeilet 2–3 beatia pilkuilla, sano se 'miksi'-kentässä.\n"
        "- Intensiteettisanat toimivat ('fully', 'with tremendous force', 'then faster'), ja ilmasanat "
        "luetaan liikkeeksi ('wind whipping', 'heat shimmer').\n"
        "- Kuvaa EI kuvailla uudestaan kun se on aloitusruutuna.\n"
        "- Ihmiset kuvassa: leveämpi kuva ja hitaampi liike. Tiukka lähikuva + nopea liike sotkee kasvot.\n"
        "- Oletus 'kielto': false, eli positiivinen nimeäminen ('Keep the subject, the framing and the "
        "horizon unchanged.'). Jos poikkeat, perustele 'miksi'-kentässä.\n\n"
        "PITUUS: grok_kesto_s on 6, 10 tai 15 — PIENIN joka kantaa sisällön. Fysiikka ja äänen rytmi "
        "pitävät parhaiten 5–8 sekunnissa; 12–15 s ajautuu. Älä pyöristä kattoon.\n\n"
        "NAUHOITETTAVAT (tyyppi 'nauhoita') ovat ruutukaappauksia: kirjoita niille 'nauhoitusohje' — "
        "mitä ruudulla tehdään, missä järjestyksessä ja millä tahdilla — ÄLÄ promptia. Niitä ei ajeta "
        "Imaginessa lainkaan, joten ÄLÄ kirjoita niille 'imagine'-lohkoa.\n\n"
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
        ' "klipit": [{"id": "5-6", "kohtaukset": [5,6], "tyyppi": "generoi", "kesto_s": 12,\n'
        '   "grok_kesto_s": 10, "aani": true, "tunnelma": "pimea", "kielto": false,\n'
        '   "kuva": "ensimmainen_ruutu", "kuva_url": "…",\n'
        '   "imagine": {"tila": "image-to-video", "kesto": "10s", "tarkkuus": "720p",\n'
        '     "kuvasuhde": "' + aspect + '", "aani": "paalla", "liite": "mitä kuvalle tehdään ja miksi"},\n'
        '   "prompt": "teko ensin. Camera not moving. Look: … Sound: … no music. Keep the subject unchanged.",\n'
        '   "ruututeksti_jalkikateen": ["…"], "miksi": "yksi lause"}]}\n'
        f"tunnelma: {' | '.join(t or '(tyhjä)' for t in TUNNELMAT)}   tila: {' | '.join(MODES)}"
    )


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
    if planned["tyyppi"] == "nauhoita":
        out.pop("prompt", None)  # a recording is filmed, never generated
        out.pop("imagine", None)  # …and never run in Imagine, so it has nothing to select there
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
    base = build_prompt(planned, valinta, tilaus, video)
    prompt, clips, violations = base, None, ["(no attempt ran)"]
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        doc = parse_json_object(llm.call([{"role": "user", "content": prompt}]))
        if doc is None:
            clips, violations = None, ["vastaus ei ollut JSON-olio."]
        else:
            written = {str(c.get("id")): c for c in (doc.get("klipit") or []) if isinstance(c, dict)}
            clips = [_merge(p, written.get(p["id"], {})) for p in planned]
            violations = check_clips(clips, shots)
        print(
            f"[{agent_name}] grok attempt {attempt}/{_MAX_ATTEMPTS}: "
            + ("OK" if not violations else "; ".join(violations[:4])),
            file=sys.stderr,
        )
        if not violations:
            break
        prompt = base + "\n\nKorjaa nämä ja kirjoita koko JSON uudestaan:\n" + "\n".join(f"- {v}" for v in violations)
    if clips is None:
        return f"FAILED: the clips could not be read as JSON after {_MAX_ATTEMPTS} attempts. Nothing was written to {key}.{addr}"

    # A prompt-rule violation is a note for the person, not a reason to discard the work (d03cce3) —
    # nothing here publishes. A clip with nothing to paste IS unusable, and that still fails.
    unusable = [
        c["id"]
        for c in clips
        if (c["tyyppi"] == "generoi" and not str(c.get("prompt") or "").strip())
        or (c["tyyppi"] == "nauhoita" and not str(c.get("nauhoitusohje") or "").strip())
    ]
    if unusable:
        return (
            f"FAILED: clip(s) {', '.join(unusable)} have nothing for a person to paste or shoot after "
            f"{_MAX_ATTEMPTS} attempts — " + "; ".join(violations[:3]) + f". Nothing was written to {key}.{addr}"
        )

    aspect = str(video.get("muoto") or "9:16")
    value: dict[str, Any] = {
        "asetukset": {
            "tarkkuus": REFERENCE_MAX_RES
            if any((c.get("imagine") or {}).get("tila") == "reference-to-video" for c in clips)
            else "720p",
            "kuvasuhde": aspect,
            "ketjutus": any(c.get("kuva") == "ei" and c["tyyppi"] == "generoi" for c in clips),
        },
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
    bent = f" {len(violations)} sääntöä jäi täyttymättä — merkitty kohtaan 'rikkeet'." if violations else ""
    return (
        f"OK: {len(clips)} klippiä ({rec} nauhoita / {len(clips) - rec} generoi) -> {key}. "
        f"Ei julkaise mitään, valmistelee.{bent}{addr}"
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

"""JULKAISUPÖYTÄ — the four agents that turn one angle into publishable material.

The editor (`crewaimeat.julkaisu_desk`) decides what is worth telling and digs out why, and writes
``julkaisu.{ref}.aineisto``. Everything here reads THAT — never a pre-written summary, and never the
changelog entry itself:

  * **julkaisu-linkedin** → ``julkaisu.{ref}.linkedin`` = ``{text, notes}``. Finnish, 600–1200
    chars, leads with the FIX (nyt) and what it is worth to the reader.
  * **julkaisu-x** → ``julkaisu.{ref}.x`` = ``{text, notes}``. English, 3–6 posts, and it leads with
    the BEFORE state (ennen) — the frustration — so the two never read as one text in two languages.
  * **julkaisu-video** → ``julkaisu.{ref}.video``, a real shot list: ``kohtaukset`` with framing,
    movement, spoken line, burned-in text and sound, plus ``kuvapyynnot`` for the shots that cannot
    be a screen recording.
  * **julkaisu-kuva** → ``julkaisu.{ref}.kuvat``. Takes the script's ``kuvapyynnot``, generates one
    image each, uploads them public, and records BOTH the URL and the storage key — the app attaches
    by key, and a URL alone cannot be attached. No model runs here at all: the prompts were already
    written by the script.

None of them posts anything or contacts anyone. The pieces land in owner memory and a person decides
at the workflow's gate.

The loop is code, the model only chooses words: the run's `ref` is resolved in code (never typed by
a model), the aineisto is required, and every piece is CHECKED against its channel's house rules
before it is stored. A violation is handed back for a rewrite; after `_MAX_ATTEMPTS` nothing is
written and the run names the rule that failed, so a step that cannot meet the angle goes output-RED
with a reason instead of shipping something weaker than the brief.
"""

from __future__ import annotations

import datetime
import json
import re
import sys
from typing import Any
from zoneinfo import ZoneInfo

from aimeat_crewai.provenance import HumanInvolvement, Level, Method, declare

from crewaimeat.aimeat_crew import _aimeat_call, record_deliverable_key, reset_deliverable_key
from crewaimeat.llm import get_llm, resolved_model, resolved_provider
from crewaimeat.memory_tools import read_owner_key
from crewaimeat.prose_style import FINNISH_NATIVE_STYLE

# The workflow variable. Every key this chain touches is julkaisu.<ref>.<something>; `{ref}` stays
# LITERAL in the published offers (the engine substitutes it per run) and concrete here.
#
# THE CANONICAL KEY TEMPLATES live here, in the lowest module of the chain, and everything else
# imports them. `julkaisu_brief` reads from this module (run_address, KEY_RULE), so defining a
# second copy of a key template over there is how the two halves would quietly disagree about where
# a run lives.
_REF_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
PIECE_KEY = "julkaisu.{ref}.{channel}"
KUVAT_KEY = "julkaisu.{ref}.kuvat"
TILAUS_KEY = "julkaisu.{ref}.tilaus"
TAUSTA_KEY = "julkaisu.{ref}.tausta"
KULMAT_KEY = "julkaisu.{ref}.kulmat"
VALINTA_KEY = "julkaisu.{ref}.valinta"
OHJAAJAT_KEY = "julkaisu.ohjaajat"

# The chosen angle's fields a writer works from.
STORY_FIELDS = ("kulma", "avaus", "miksi_toimii", "kenelle", "nojaa", "ohjaaja_ele", "riski")

# The two answers the angle gate can carry. The app has a "Lisää kulmia" button, so `lisaa` is a
# normal answer meaning "another batch, I have not chosen yet" — and a writer must REFUSE it rather
# than pick an angle on the person's behalf. That refusal is the whole point of turning the chain
# around: the person directs.
VALITTU, LISAA = "valittu", "lisaa"

# THE RULE, in every julkaisu agent's prompt, word for word. The code already resolves the address
# (`run_address`) and the tools take no key, so the model cannot mistype one — but it is stated here
# too because the failure it prevents is one a model talks itself into: with no key in sight it
# generates a plausible id (p69c3e53, p6605be9, p55ff4e1 on three prod runs), writes good work there,
# and the engine records the step as having produced nothing.
KEY_RULE = (
    "KEY RULE — NEVER generate, invent or randomise the id in a memory key. You are told the key. "
    "Read it.\n"
    "  1. FIRST look in the task's scope for a field named `deliverable_key`. If it is there, that "
    "is your output key, complete and final. Write there, character for character. Do not add to "
    "it, do not prefix it, do not 'improve' it.\n"
    "  2. If `deliverable_key` is absent, the scope also carries the run's variables as `var.<name>` "
    "(e.g. var.date = 2026-08-24). Build the key from those, using the template in your work "
    "description.\n"
    "  3. If neither is present, the id is TODAY'S DATE in YYYY-MM-DD form — nothing else.\n"
    "The same three rules apply to the key you READ your input from. If you cannot determine the "
    "key by these rules, FAIL the task and say so. Do not write to a key you made up: that looks "
    "like success to you and like a dead step to everyone else.\n\n"
)

# One line of the same rule for an agent's backstory, where the standing habits live.
KEY_RULE_BACKSTORY = (
    "You never invent the id in a memory key: it comes from the task's scope (`deliverable_key`, "
    "else its `var.<name>` variables, else today's date in YYYY-MM-DD), and the tool has already "
    "resolved it. If you ever find yourself composing an id, stop and fail instead — an invented "
    "key looks like success to you and like a dead step to everyone else. "
)

_MAX_ATTEMPTS = 3  # first write + two rewrites against the violations we hand back
# `versioita` x `kielet` is a product, and every piece is a paid generation. 3 versions in both
# languages would be 6; this is the ceiling, and the report says when it bit.
_MAX_VERSIONS = 4


def _opening_of(made: dict) -> str:
    """The first line of a produced version — what the next version must not repeat."""
    if made.get("text"):
        return str(made["text"]).strip().splitlines()[0][:120]
    shots = made.get("kohtaukset") or []
    return str((shots[0] or {}).get("puhe", ""))[:120] if shots else "(ei avausta)"


_TEXT_TAG = re.compile(r"<TEKSTI>(.*?)</TEKSTI>", re.S | re.I)
_NOTES_TAG = re.compile(r"<HUOMIOT>(.*?)</HUOMIOT>", re.S | re.I)

_OUTPUT_CONTRACT = (
    "\n\nVASTAUKSEN MUOTO — täsmälleen nämä kaksi lohkoa, ei mitään muuta:\n"
    "<TEKSTI>\n(valmis teksti sellaisenaan, ei otsikkoa 'teksti', ei lainausmerkkejä ympärillä)\n</TEKSTI>\n"
    "<HUOMIOT>\n(1–4 lyhyttä riviä: mitä jätit pois aineistosta ja miksi)\n</HUOMIOT>"
)


# ── input ────────────────────────────────────────────────────────────────────────────────────────
def _walk(value: Any):
    """Every (key, value) pair inside a nested task record."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield k, v
            yield from _walk(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk(v)


def scope_deliverable_key(task: dict | None) -> str | None:
    """The output key the dispatch NAMED, or None.

    RULE 1, and it outranks everything else: if the task's scope carries `deliverable_key`, that is
    the address, complete and final — written character for character, never prefixed, extended or
    "improved". The engine checks the key IT knows; anything else is a dead step.
    """
    for k, v in _walk(task or {}):
        if str(k).lower() in ("deliverable_key", "deliverablekey") and isinstance(v, str) and v.strip():
            return v.strip()
    return None


def scope_vars(task: dict | None) -> dict[str, str]:
    """The run's variables, as the scope carries them: `var.<name>` fields (RULE 2).

    Also accepts a `vars` / `params` object, because the same values reach a task both ways depending
    on how the run was started, and a variable the engine did send is not worth losing to a shape.
    """
    out: dict[str, str] = {}
    for k, v in _walk(task or {}):
        key = str(k)
        if key.lower().startswith("var.") and isinstance(v, (str, int, float)) and str(v).strip():
            out.setdefault(key[4:].lower(), str(v).strip())
        elif key.lower() in ("vars", "params", "variables") and isinstance(v, dict):
            for name, value in v.items():
                if isinstance(value, (str, int, float)) and str(value).strip():
                    out.setdefault(str(name).lower().removeprefix("var."), str(value).strip())
    return out


def today_id() -> str:
    """RULE 3: today's date, YYYY-MM-DD, Europe/Helsinki — the fleet's editorial day.

    Not a random id, not a hash of anything: a value the engine, the app and a person all compute
    the same way without being told. That is the whole point — an id nobody else can derive is an id
    nobody else can find.
    """
    return datetime.datetime.now(ZoneInfo("Europe/Helsinki")).strftime("%Y-%m-%d")


# The variable names that carry the run's id, most specific first. `ref` is what the offers template;
# `date` is what the engine sends for a dated run, and rule 3 lands on the same shape.
_ID_VARS = ("ref", "id", "date", "paiva", "edition_date")


def resolve_id(task: dict | None) -> tuple[str, str]:
    """The run's id by RULES 2 and 3 — (id, which rule). Never generated, never randomised.

    A single unnamed variable is taken as the id too: a run that sent exactly one value cannot have
    meant a different one, and refusing it would fall to the date and write to the wrong address.
    """
    variables = scope_vars(task)
    for name in _ID_VARS:
        if variables.get(name):
            return variables[name], f"rule 2: the run's var.{name}"
    if len(variables) == 1:
        name, value = next(iter(variables.items()))
        return value, f"rule 2: the run's only variable, var.{name}"
    return today_id(), "rule 3: no key and no variables in the dispatch, so the id is today's date"


def _id_of_key(key: str) -> str | None:
    """The id inside a `julkaisu.<id>.<channel>` key, or None when it is shaped differently."""
    m = re.match(r"^julkaisu\.(.+)\.(?:aineisto|linkedin|x|video|kuvat|portti|mittaus)$", key or "", re.I)
    return m.group(1) if m else None


def run_address(task: dict | None, channel: str) -> tuple[str, str, str]:
    """(output key, run id, which rule) — the ONE place any julkaisu agent learns where to write.

    The defect this replaces: with no key in the dispatch the agents generated one (p69c3e53,
    p6605be9, p55ff4e1 on three prod runs), wrote a perfectly good result there, and the engine —
    looking at the key it knows — recorded the step as having produced nothing. The work was done
    and thrown away, three times. An invented id looks like success from the inside and like a dead
    step from everywhere else, which is exactly why it must never be invented.

    Rule 1 wins outright: a named `deliverable_key` is used verbatim, and the run id is read back
    OUT of it so the input key belongs to the same run.
    """
    named = scope_deliverable_key(task)
    if named:
        return named, (_id_of_key(named) or resolve_id(task)[0]), "rule 1: the dispatch named deliverable_key"
    run_id, rule = resolve_id(task)
    return PIECE_KEY.format(ref=run_id, channel=channel), run_id, rule


def _read_json_key(agent_name: str, key: str) -> dict | None:
    value = read_owner_key(agent_name, key)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def read_valinta(agent_name: str, ref: str) -> dict:
    """The angle THE PERSON chose. Raises when nobody has chosen yet.

    Two answers can sit at this gate, because the app has a "Lisää kulmia" button: `valittu` (here
    is the angle) and `lisaa` (offer me another batch, I have not decided). A writer that treated
    `lisaa` as permission to pick would be choosing on the person's behalf — which is the exact thing
    v3 turned the chain around to stop. So it refuses, loudly, and says what the gate actually says.
    """
    key = VALINTA_KEY.format(ref=ref)
    value = _read_json_key(agent_name, key)
    if value is None:
        raise LookupError(
            f"choice '{key}' is missing — nobody has picked an angle yet. Nothing was written; this "
            "agent does not choose the angle."
        )
    answer = str(value.get("vastaus") or "").strip().casefold()
    if answer == LISAA:
        raise LookupError(
            f"the gate at '{key}' answered '{LISAA}' — the person asked for more angles instead of "
            "choosing one. Nothing was written; the angle director runs again and a person picks."
        )
    angle = value.get("kulma")
    if not isinstance(angle, dict) or not str(angle.get("kulma") or "").strip():
        raise LookupError(
            f"choice '{key}' carries no chosen angle (vastaus={answer or 'missing'!r}) — it is not a "
            "usable brief, and this agent does not fill one in. Nothing was written."
        )
    return value


def read_tausta(agent_name: str, ref: str) -> dict:
    """This run's research. Missing research is survivable — a chosen angle is still a brief — but it
    is reported, because a piece written without the sourced background is a weaker piece."""
    return _read_json_key(agent_name, TAUSTA_KEY.format(ref=ref)) or {}


def _picked_block(valinta: dict, kulmat: list[dict]) -> str:
    """The other angles the person ticked (`poimitut`) — MATERIAL, never a second subject."""
    wanted = {int(n) for n in (valinta.get("poimitut") or []) if str(n).strip().isdigit()}
    rows = [
        f"  #{a.get('nro')} {a.get('otsikko')}: {a.get('kulma')}"
        for a in kulmat
        if isinstance(a, dict) and int(a.get("nro") or 0) in wanted
    ]
    if not rows:
        return ""
    return (
        "\n\nPOIMITUT KULMAT — tilaaja haluaa näistä paloja MUKAAN. Käytä niitä aineistona: yksi "
        "lause, yksi kuva, yksi luku. Ne EIVÄT ole toinen aihe; valittu kulma on yhä tarina.\n" + "\n".join(rows)
    )


def slot_is_directed(valinta: dict, channel: str) -> bool:
    """Does the direction reach THIS slot? (`vaikuttaa` in the order.)

    A slot missing from that list is written plainly, in the ordered style, with no directorial voice
    at all — a Fincher video beside an unadorned LinkedIn post is a normal order, not a mistake. An
    order naming no `vaikuttaa` directs everything, which is the older behaviour.
    """
    reach = valinta.get("vaikuttaa")
    if reach is None:
        return True
    return channel in {str(x).strip().casefold() for x in (reach or []) if isinstance(x, str)}


def languages_for(valinta: dict, channel: str, default: str) -> list[str]:
    """Which language(s) this channel is written in — `fi`, `en`, or `both`.

    This used to be hardcoded per writer (LinkedIn Finnish, X English), which is a decision nobody
    made on purpose. `both` produces the piece twice, once in each language, as two versions.
    """
    kielet = valinta.get("kielet")
    want = str((kielet or {}).get(channel) or "").strip().casefold() if isinstance(kielet, dict) else ""
    if want == "both":
        return ["fi", "en"]
    return [want] if want in ("fi", "en") else [default]


def story_block(
    valinta: dict,
    tausta: dict,
    ohjaajat: dict,
    kulmat: list[dict] | None = None,
    *,
    lead: str = "avaus",
    channel: str = "",
) -> str:
    """The brief a writer works from: the chosen angle, the research behind it, and the direction.

    `lead` keeps the Finnish post and the English thread two pieces rather than one text twice —
    LinkedIn opens on the angle's written first line, X opens on the tension (the counter-argument
    and the angle's own risk) and lands on the same claim. Same story, different door in.
    """
    angle = valinta.get("kulma") or {}
    labels = {
        "kulma": "KULMA (se yksi lause jonka lukija toistaisi)",
        "avaus": "AVAUS (ohjaajan kirjoittama ensimmäinen rivi — käytä sitä tai sen henkeä)",
        "miksi_toimii": "MIKSI TOIMII",
        "kenelle": "KENELLE (kuka tarkalleen)",
        "nojaa": "NOJAA (mihin tämä nojaa)",
        "ohjaaja_ele": "OHJAAJAN ELE (tämä näkyy tekstissä)",
        "riski": "RISKI (mitä tässä voi mennä pieleen)",
    }
    order = ["kulma", lead, *[f for f in STORY_FIELDS if f not in ("kulma", lead)]]
    rows = [
        f"{labels.get(f, f.upper())}: {str(angle.get(f)).strip()}"
        for f in dict.fromkeys(order)
        if str(angle.get(f) or "").strip()
    ]

    findings = [f for f in (tausta.get("loydokset") or []) if isinstance(f, dict)]
    if findings:
        rows.append(
            "\nTAUSTA — käytä näitä, ja jos siteeraat lukua tai väitettä, se on näistä:\n"
            + "\n".join(f"  - {f.get('vaite')} [{f.get('lahde')}]" for f in findings)
        )
    comps = [v for v in (tausta.get("vertailu") or []) if isinstance(v, dict)]
    if comps:
        rows.append("VERTAILU: " + "; ".join(f"{v.get('kuka')} — {v.get('mita_tekee')}" for v in comps))
    for field, label in (
        ("ajankohtaisuus", "AJANKOHTAISUUS"),
        ("vastavaite", "VASTAVÄITE (älä ohita tätä)"),
        ("ei_loytynyt", "EI LÖYTYNYT (älä väitä näitä)"),
    ):
        if str(tausta.get(field) or "").strip():
            rows.append(f"{label}: {tausta[field]}")
    if not findings:
        rows.append("TAUSTA: tutkimusta ei ollut saatavilla — pysy siinä mitä kulma sanoo, älä keksi lukuja.")

    block = "\n".join(rows) + _picked_block(valinta, kulmat or [])
    # The director shapes the WRITING too, not only the video. A Fincher LinkedIn post is not the
    # same post as a Gondry one: rhythm, sentence length and what is left unsaid all carry.
    from crewaimeat.julkaisu_brief import director_block, style_block

    styles = style_block(ohjaajat, valinta.get("tyylit") or valinta.get("tyyli"))
    if channel and not slot_is_directed(valinta, channel):
        # Ordered plain: the direction reaches other slots, not this one.
        block += (
            "\n\nEI OHJAAJAA TÄHÄN OSAAN. Tilaus rajasi ohjauksen muihin paloihin, joten kirjoita "
            "tämä suoraan ja koruttomasti tilatussa tyylissä. Älä lainaa kenenkään kuvakieltä.\n\n" + styles
        )
    else:
        block += "\n\n" + director_block(ohjaajat, valinta.get("ohjaajat") or valinta.get("ohjaaja")) + "\n\n" + styles
        block += (
            "\n\nOHJAAJA KOSKEE MYÖS KIRJOITTAMISTA: rytmi, lauseen pituus, mitä jätetään sanomatta. "
            "Kentässä TEKSTI lukee miten hän kirjoittaa — se on tämän palan tärkein rivi. "
            "Älä kuvaile ohjaajaa, kirjoita hänen rytmissään."
        )
    if str(valinta.get("lisaohje") or "").strip():
        block += f"\n\nLISÄOHJE TILAAJALTA (tämä voittaa talon oletukset): {valinta['lisaohje']}"
    return block


# ── house rules, checked in code ─────────────────────────────────────────────────────────────────
_HASHTAG = re.compile(r"(?<![\w#])#[\wåäöÅÄÖ][\wåäöÅÄÖ-]*")
# Leading pictographs / bullet glyphs — an emoji bullet list, which the X thread must not use.
_EMOJI_BULLET = re.compile(
    r"^[ \t]*[•▪▶►\u2190-\u21FF\u2300-\u23FF\u25A0-\u27BF\u2B00-\u2BFF\uFE0F\U0001F000-\U0001FAFF]",
    re.M,
)
# A NAMEABLE thing: a capitalised word or a number. Used only to catch an excluded specific leaking
# into a piece — never to judge prose.
_NAMED = re.compile(r"\b[A-ZÅÄÖ][\wåäö/-]{3,}\b|\b\d[\d.,]*\s*(?:%|€|kk|min|s|h)?\b")


def excluded_leak(text: str, brief: dict) -> list[str]:
    """Violations for material the editor put in `ei_kerrota` that turned up in the piece anyway.

    Deliberately NARROW. It compares only NAMEABLE things — capitalised names and numbers — and only
    ones that appear in an excluded item and NOT anywhere in the angle itself. One such NAME is
    already the tell: a word the editor ruled out, that the angle never mentions, cannot have reached
    the piece any other way. Bare numbers are weaker (a date or a version can coincide), so those
    need two. A looser rule would block good prose for sharing an ordinary word with an excluded
    sentence, and a check that cries wolf gets switched off.
    """
    allowed = set()
    for field in (*STORY_FIELDS, "varmuus"):
        allowed |= set(_NAMED.findall(str(brief.get(field) or "")))
    out = []
    for item in brief.get("ei_kerrota") or []:
        tokens = {t.strip() for t in _NAMED.findall(str(item)) if t.strip()} - allowed
        hits = sorted(t for t in tokens if t and t in text)
        named = [h for h in hits if h[:1].isalpha()]
        if named or len(hits) >= 2:
            out.append(f"teksti käyttää poissuljettua aihetta ({', '.join(hits[:3])}) — se on ei_kerrota-listalla.")
    return out


def check_linkedin(text: str, brief: dict | None = None) -> list[str]:
    """House rules for the Finnish LinkedIn post. Violations in Finnish — they are fed straight back
    into a Finnish prompt, where an English instruction would invite translationese."""
    bad: list[str] = []
    body = text.strip()
    n = len(body)
    if not 600 <= n <= 1200:
        bad.append(f"pituus on {n} merkkiä — sen pitää olla 600–1200 merkkiä.")
    tags = _HASHTAG.findall(body)
    if len(tags) > 2:
        bad.append(f"aihetunnisteita on {len(tags)} ({' '.join(tags[:5])}) — enintään kaksi, mieluiten ei yhtään.")
    first = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
    low = body.lower()
    if low.startswith("olen innoissani") or low.startswith("olen todella innoissani"):
        bad.append("aloitus on 'olen innoissani' — aloita lukijan hyödystä.")
    if "tässä muutama ajatus" in low:
        bad.append("teksti sisältää 'tässä muutama ajatus' — poista se.")
    if first.endswith("?"):
        bad.append("ensimmäinen rivi on kysymys — ensimmäisen kappaleen pitää kertoa lukijan konkreettinen hyöty.")
    if brief:
        bad += excluded_leak(body, brief)
    return bad


_X_FOLLOW_BAIT = (
    "follow me",
    "follow for more",
    "hit follow",
    "give me a follow",
    "like and",
    "like & ",
    "retweet",
    "rt if",
    "smash that",
    "subscribe for",
)


def x_posts(text: str) -> list[str]:
    """The thread's posts — separated by a blank line, which is the `text` field's contract."""
    return [p.strip() for p in re.split(r"\n\s*\n", (text or "").strip()) if p.strip()]


def check_x(text: str, brief: dict | None = None) -> list[str]:
    """House rules for the English X thread. Violations in English — the prompt is English."""
    bad: list[str] = []
    posts = x_posts(text)
    if not 3 <= len(posts) <= 6:
        bad.append(f"the thread has {len(posts)} post(s) — it must have 3 to 6, separated by a blank line.")
    for i, p in enumerate(posts, 1):
        if len(p) > 280:
            bad.append(f"post {i} is {len(p)} characters — every post must be under 280.")
    if posts:
        first = posts[0].lower()
        if "🧵" in text or re.search(r"\ba thread\b|\bthread:\s|^thread\b", first, re.I):
            bad.append("post 1 announces a thread ('🧵' / 'a thread:') — it must stand alone as a claim.")
        last = posts[-1].lower()
        for bait in _X_FOLLOW_BAIT:
            if bait in last:
                bad.append(f"the last post asks for engagement ('{bait.strip()}') — say what a reader can do next.")
                break
    if _EMOJI_BULLET.search(text or ""):
        bad.append("a line starts with an emoji/bullet glyph — no emoji bullets anywhere in the thread.")
    if brief:
        bad += excluded_leak(text, brief)
    return bad


# ── the video script: a shot list, not prose with brackets glued on ──────────────────────────────
KUVAKOKO = ("ruutukaappaus", "lahikuva", "puolikuva", "laaja")
LIIKE = ("still", "hidas zoom sisaan", "pan oikealle", "leikkaus")
AANI = ("puhe", "vaimennettu tausta", "isku leikkauksessa")
_SHOT_MAX_S = 6
_TOTAL_S = (45, 75)
_SHOTS_MIN = 6  # the offer's success_signal floor; the duration rule pushes a real script to ~8–12
_RUUTUTEKSTI_MAX_WORDS = 6
_STOCK = ("stock", "arkistokuv", "kuvituskuv", "geneerinen", "b-roll", "kuvapankki")


def parse_json_object(raw: str) -> dict | None:
    """The first JSON object in a model reply, fences and preamble tolerated. None when there is none
    — the caller retries rather than storing something that is really an apology."""
    text = raw if isinstance(raw, str) else str(raw)
    text = re.sub(r"^\s*```(?:json)?|```\s*$", "", text.strip(), flags=re.M)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        out = json.loads(text[start : end + 1])
    except ValueError:
        return None
    return out if isinstance(out, dict) else None


def check_video(doc: dict) -> list[str]:
    """The shot list's contract, checked field by field. Violations in Finnish."""
    bad: list[str] = []
    shots = doc.get("kohtaukset")
    if not isinstance(shots, list) or len(shots) < _SHOTS_MIN:
        bad.append(
            f"kohtauksia on {len(shots) if isinstance(shots, list) else 0} — niitä pitää olla vähintään {_SHOTS_MIN} (45–75 s enintään {_SHOT_MAX_S} s kuvina tarkoittaa noin 8–12)."
        )
        shots = shots if isinstance(shots, list) else []
    if str(doc.get("muoto") or "").strip() != "9:16":
        bad.append("muoto ei ole '9:16' — tämä on pystyvideo.")
    total = 0
    for i, s in enumerate(shots, 1):
        if not isinstance(s, dict):
            bad.append(f"kohtaus {i} ei ole olio.")
            continue
        where = f"kohtaus {i}"
        if s.get("nro") != i:
            bad.append(f"{where}: 'nro' on {s.get('nro')!r} — kohtaukset numeroidaan 1..n järjestyksessä.")
        kesto = s.get("kesto_s")
        if not isinstance(kesto, (int, float)) or kesto <= 0:
            bad.append(f"{where}: 'kesto_s' puuttuu tai ei ole luku.")
        elif kesto > _SHOT_MAX_S:
            bad.append(f"{where}: kesto {kesto} s — yksikään kuva ei saa olla yli {_SHOT_MAX_S} s.")
        else:
            total += kesto
        if s.get("kuvakoko") not in KUVAKOKO:
            bad.append(f"{where}: 'kuvakoko' on {s.get('kuvakoko')!r} — valitse {' | '.join(KUVAKOKO)}.")
        if s.get("liike") not in LIIKE:
            bad.append(f"{where}: 'liike' on {s.get('liike')!r} — valitse {' | '.join(LIIKE)}.")
        if s.get("aani") not in AANI:
            bad.append(f"{where}: 'aani' on {s.get('aani')!r} — valitse {' | '.join(AANI)}.")
        kuvassa = str(s.get("kuvassa") or "").strip()
        if not kuvassa:
            bad.append(f"{where}: 'kuvassa' on tyhjä — nimeä tarkalleen mitä ruudussa näkyy.")
        for token in _STOCK:
            if token in kuvassa.casefold():
                bad.append(f"{where}: 'kuvassa' on kuvituskuvaohje ('{token}') — näytä jotain mikä on olemassa.")
                break
        if not str(s.get("puhe") or "").strip():
            bad.append(f"{where}: 'puhe' on tyhjä.")
        rt = str(s.get("ruututeksti") or "").strip()
        if len(rt.split()) > _RUUTUTEKSTI_MAX_WORDS:
            bad.append(f"{where}: ruututeksti on {len(rt.split())} sanaa — enintään {_RUUTUTEKSTI_MAX_WORDS}.")
    if shots and not _TOTAL_S[0] <= total <= _TOTAL_S[1]:
        bad.append(f"kokonaiskesto on {total} s — sen pitää olla {_TOTAL_S[0]}–{_TOTAL_S[1]} s.")
    kesto_s = doc.get("kesto_s")
    if shots and isinstance(kesto_s, (int, float)) and abs(kesto_s - total) > 1:
        bad.append(f"'kesto_s' on {kesto_s} mutta kohtausten summa on {total} — niiden pitää täsmätä.")
    if shots:
        first = shots[0] if isinstance(shots[0], dict) else {}
        opening = f"{first.get('kuvassa', '')} {first.get('ruututeksti', '')}".casefold()
        if "logo" in opening:
            bad.append("ensimmäisessä kohtauksessa on logo — kolme ensimmäistä sekuntia kuuluvat väitteelle.")
    reqs = doc.get("kuvapyynnot")
    if not isinstance(reqs, list):
        bad.append("'kuvapyynnot' puuttuu — anna lista (tyhjä lista jos jokainen kuva on ruutukaappaus).")
    else:
        by_nro = {s.get("nro"): s for s in shots if isinstance(s, dict)}
        for r in reqs:
            if not isinstance(r, dict) or not str(r.get("prompt") or "").strip():
                bad.append("kuvapyynnössä ei ole 'prompt'-tekstiä.")
                continue
            shot = by_nro.get(r.get("nro"))
            if shot is None:
                bad.append(f"kuvapyyntö viittaa kohtaukseen {r.get('nro')!r}, jota ei ole.")
            elif shot.get("kuvakoko") == "ruutukaappaus":
                bad.append(f"kohtaus {r.get('nro')} on ruutukaappaus — sille ei pyydetä generoitua kuvaa.")
    if not str(doc.get("text") or "").strip():
        bad.append("'text' on tyhjä — sama käsikirjoitus luettavana tekstinä puuttuu.")
    if not str(doc.get("notes") or "").strip():
        bad.append("'notes' on tyhjä — kerro mitä jätit pois ja miksi.")
    return bad


# ── prompts ──────────────────────────────────────────────────────────────────────────────────────
def _linkedin_prompt(b: dict) -> str:
    return (
        "Olet suomalainen kirjoittaja AIMEATin julkaisupöydässä. IHMINEN on jo valinnut kulman ja "
        "ohjaajan. Sinä kirjoitat sen kulman — et valitse toista.\n\n"
        "TALON SÄÄNNÖT:\n"
        "- Kirjoita VALITUSTA KULMASTA. Avaus on jo kirjoitettu sinulle: käytä sitä tai sen henkeä.\n"
        "- Pituus 600–1200 merkkiä.\n"
        "- Lukijan konkreettinen hyöty ensimmäisessä kappaleessa, ei kolmannessa.\n"
        "- Ei aihetunnistekasaa: korkeintaan kaksi, mieluiten ei yhtään.\n"
        "- Ei aloitusta 'olen innoissani', ei fraasia 'tässä muutama ajatus', ei retorista kysymystä "
        "ensimmäiseksi riviksi.\n"
        "- Väitä vain sitä mitä kulma tai tausta sanoo. Jos siteeraat lukua, se on taustan lähteistä. "
        "EI LÖYTYNYT kertoo mitä ei varmistettu — älä esitä sitä varmana.\n\n"
        "BRIIFFI:\n" + b["block"] + FINNISH_NATIVE_STYLE + _OUTPUT_CONTRACT
    )


def _x_prompt(b: dict) -> str:
    return (
        "You write X threads for AIMEAT. A PERSON has already chosen the angle and the director; you "
        "write that angle, you do not pick another.\n\n"
        "YOUR DOOR IN IS THE TENSION. Open on what is uncomfortable — the counter-argument, or the "
        "risk the angle itself names — and let the chosen claim arrive as the turn. The Finnish "
        "LinkedIn post for this same angle opens on its written first line; if your thread reads like "
        "that post translated, the run failed even though both files exist.\n\n"
        "HOUSE RULES:\n"
        "- 3 to 6 posts. Separate every post with a BLANK LINE. Each post under 280 characters.\n"
        "- Post 1 has to stand alone as a claim worth reading — no '🧵', no 'a thread:', no announcement.\n"
        "- No emoji bullets anywhere.\n"
        "- The last post says what a reader can do next. Do not ask for a follow, a like or a retweet.\n"
        "- Claim only what the angle or the research says. A number you quote comes from a source in "
        "the research. EI LÖYTYNYT is what could NOT be verified — do not state it as fact.\n\n"
        "BRIEF (in Finnish — the thread is in English):\n" + b["block"] + _OUTPUT_CONTRACT
    )


def _video_prompt(b: dict) -> str:
    return (
        "Olet käsikirjoittaja AIMEATin julkaisupöydässä. Kirjoita YKSI pystyvideon (9:16, 45–75 s) "
        "KUVALUETTELO. Et kirjoita proosaa: kirjoitat kohtauksia, jotka joku voi kuvata.\n\n"
        "SÄÄNNÖT:\n"
        f"- {_SHOTS_MIN}–14 kohtausta, yksikään ei yli {_SHOT_MAX_S} sekuntia. Yhteiskesto {_TOTAL_S[0]}–{_TOTAL_S[1]} s, "
        "ja 'kesto_s' on kohtausten summa.\n"
        "- Kolme ensimmäistä sekuntia kantavat väitteen, eivät logoa.\n"
        "- 'kuvassa' nimeää jotain mikä ON OLEMASSA: oikea näkymä tuotteesta, oikea luku, oikea "
        "valikko. Ei kuvituskuvaa, ei arkistokuvaohjeita.\n"
        f"- 'ruututeksti' enintään {_RUUTUTEKSTI_MAX_WORDS} sanaa.\n"
        "- 'kuvapyynnot' VAIN niille kohtauksille joita ei voi kuvata ruutukaappauksena. Kirjoita niiden "
        "prompt englanniksi ja kuvaile mitä kuvassa on, älä tuotenimiä.\n"
        "- Lopeta siihen minkä katsoja toistaisi kollegalleen.\n"
        "- Väitä vain sitä mitä kulma tai tausta sanoo; EI LÖYTYNYT -asioita ei esitetä varmana.\n"
        "- OHJAAJA koskee kuvaa, rytmiä, väriä ja ääntä. Tämä on se kohta jossa hän näkyy eniten.\n\n"
        "BRIIFFI:\n" + b["block"] + FINNISH_NATIVE_STYLE + "\n\n"
        "VASTAUKSEN MUOTO — pelkkä JSON-olio, ei mitään sen ympärille:\n"
        "{\n"
        '  "kesto_s": 58,\n'
        '  "muoto": "9:16",\n'
        '  "kohtaukset": [\n'
        '    {"nro": 1, "kesto_s": 3, "kuvakoko": "' + KUVAKOKO[0] + '", "kuvassa": "tarkalleen mitä ruudussa on",\n'
        '     "liike": "' + LIIKE[0] + '", "puhe": "puhuttu repliikki", "ruututeksti": "enintään kuusi sanaa",\n'
        '     "aani": "' + AANI[0] + '"}\n'
        "  ],\n"
        '  "kuvapyynnot": [{"nro": 4, "prompt": "image prompt in English"}],\n'
        '  "text": "sama käsikirjoitus luettavana tekstinä",\n'
        '  "notes": "mitä jätit pois ja miksi"\n'
        "}\n"
        f"kuvakoko: {' | '.join(KUVAKOKO)}   liike: {' | '.join(LIIKE)}   aani: {' | '.join(AANI)}"
    )


CHANNELS: dict[str, dict] = {
    "linkedin": {
        "agent": "julkaisu-linkedin",
        "lead": "avaus",
        "lang": "fi",
        "what": "LinkedIn-postaus (suomi, 600–1200 merkkiä)",
        "prompt": _linkedin_prompt,
        "check": check_linkedin,
        "temperature": 0.6,
        "retry_lead": "Korjaa nämä ja kirjoita postaus uudestaan kokonaan:",
        "structured": False,
    },
    "x": {
        "agent": "julkaisu-x",
        "lead": "riski",
        "lang": "en",
        "what": "X thread (English, 3–6 posts)",
        "prompt": _x_prompt,
        "check": check_x,
        "temperature": 0.7,
        "retry_lead": "Fix these and write the whole thread again:",
        "structured": False,
    },
    "video": {
        "agent": "julkaisu-video",
        "lead": "avaus",
        "lang": "fi",
        "what": "pystyvideon kuvaluettelo (suomi, 9:16, 45–75 s)",
        "prompt": _video_prompt,
        "check": check_video,
        "temperature": 0.6,
        "retry_lead": "Korjaa nämä ja kirjoita koko JSON uudestaan:",
        "structured": True,
    },
}


def parse_piece(raw: str) -> tuple[str, str]:
    """(text, notes) out of the model's two tagged blocks. Missing tags -> ('', '') so the caller
    retries instead of storing a piece that is really an apology or a stray preamble."""
    raw = raw if isinstance(raw, str) else str(raw)
    t = _TEXT_TAG.search(raw)
    n = _NOTES_TAG.search(raw)
    if not t:
        return "", ""
    return t.group(1).strip(), (n.group(1).strip() if n else "")


# ── the run ──────────────────────────────────────────────────────────────────────────────────────
def write_julkaisu(agent_name: str, channel: str, task: dict | None = None, task_id: str | None = None) -> str:
    """Write ONE checked piece, from the angle A PERSON chose, to the key this run was GIVEN.

    The address comes from `run_address` — the dispatch's `deliverable_key`, else its variables, else
    today's date. Never a generated id: that is the defect this whole path was rewritten around.

    The brief is `julkaisu.<id>.valinta` (the chosen angle, the director, the style, the picked
    extras) plus `julkaisu.<id>.tausta` (the sourced research). Nothing is posted and nobody is
    contacted — the piece lands in owner memory for the approval gate. A gate that has not chosen, a
    model that will not meet the house rules, or a failed write all return a FAILED report and write
    nothing, so the step's success_signal reads the same absence.
    """
    spec = CHANNELS.get(channel)
    if spec is None:
        raise KeyError(f"unknown channel {channel!r} (known: {', '.join(sorted(CHANNELS))})")
    key, run_id, rule = run_address(task, channel)
    inferred = f" Address: {rule}."
    print(f"[{agent_name}] {channel} -> {key} ({rule})", file=sys.stderr)
    try:
        from crewaimeat.julkaisu_brief import read_ohjaajat

        valinta = read_valinta(agent_name, run_id)
        tausta = read_tausta(agent_name, run_id)
        ohjaajat = read_ohjaajat(agent_name)
        kulmat = (_read_json_key(agent_name, KULMAT_KEY.format(ref=run_id)) or {}).get("kulmat") or []
        # The order carries `versioita`, `kielet`, `vaikuttaa` and `tyylit`; the choice carries the
        # angle and may repeat any of them. The choice wins where it speaks, the order fills the rest
        # — so the app can put those fields in either place and neither is silently ignored.
        order = {**(_read_json_key(agent_name, TILAUS_KEY.format(ref=run_id)) or {}), **valinta}
        block = story_block(order, tausta, ohjaajat, kulmat, lead=spec["lead"], channel=channel)
    except LookupError as exc:
        print(f"[{agent_name}] {exc}", file=sys.stderr)
        return f"FAILED: {exc}{inferred}"
    if not tausta:
        print(f"[{agent_name}] {channel}: no research for this run — writing from the angle alone", file=sys.stderr)

    # What the leak check treats as off-limits: the research's own "could not find". A writer that
    # states a named thing the researcher explicitly failed to verify has invented it.
    brief = {
        "kulma": str((valinta.get("kulma") or {}).get("kulma") or ""),
        "ei_kerrota": [tausta.get("ei_loytynyt")] if tausta.get("ei_loytynyt") else [],
        "block": block,
    }
    llm = get_llm(for_tool_use=False, temperature=spec["temperature"], agent_name=agent_name)
    base = spec["prompt"](brief)
    structured = spec["structured"]

    def _one(base_prompt: str) -> tuple[dict | None, list[str]]:
        """One version, retried against its own violations. (value, violations)."""
        prompt, value, previous, violations = base_prompt, None, "", ["(no attempt ran)"]
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            out = llm.call([{"role": "user", "content": prompt}])
            if structured:
                doc = parse_json_object(out)
                if doc is None:
                    value, previous, violations = None, str(out)[:1500], ["vastaus ei ollut JSON-olio."]
                else:
                    value, previous, violations = doc, json.dumps(doc, ensure_ascii=False)[:2000], spec["check"](doc)
            else:
                text, notes = parse_piece(out)
                if not text:
                    value, previous, violations = (
                        None,
                        str(out)[:1000],
                        ["vastauksesta puuttui <TEKSTI>-lohko / the reply had no <TEKSTI> block."],
                    )
                else:
                    value, previous, violations = {"text": text, "notes": notes}, text, spec["check"](text, brief)
            print(
                f"[{agent_name}] {channel} attempt {attempt}/{_MAX_ATTEMPTS}: "
                + ("OK" if not violations else "; ".join(violations)),
                file=sys.stderr,
            )
            if not violations:
                return value, []
            prompt = (
                base_prompt
                + "\n\n"
                + spec["retry_lead"]
                + "\n"
                + "\n".join(f"- {v}" for v in violations)
                + "\n\nEDELLINEN VERSIO / PREVIOUS VERSION:\n"
                + (previous or "(tyhjä)")
            )
        return None, violations

    # How many pieces this order wants, and in which languages. `both` produces the piece twice; the
    # product is capped so an order cannot quietly turn into six paid generations.
    langs = languages_for(order, channel, spec["lang"])
    try:
        want_versions = max(1, min(3, int(order.get("versioita") or 1)))
    except (TypeError, ValueError):
        want_versions = 1
    plan = [(lang, n) for lang in langs for n in range(1, want_versions + 1)]
    capped = ""
    if len(plan) > _MAX_VERSIONS:
        capped = f" Order asked for {len(plan)} pieces; capped at {_MAX_VERSIONS}."
        plan = plan[:_MAX_VERSIONS]

    made: list[dict] = []
    violations: list[str] = []
    for nro, (lang, _n) in enumerate(plan, start=1):
        extra = f"\n\nKIELI: kirjoita tämä versio kielellä '{lang}'."
        if made:
            openings = "\n".join(f"  - {_opening_of(m)}" for m in made)
            extra += (
                f"\n\nTÄMÄ ON VERSIO {nro}. Aiemmat versiot avasivat näin:\n{openings}\n"
                "Sinun on ERO TTAVA enemmän kuin sanamuodoilla: eri avausliike, eri asia edellä. "
                "Kaksi saman tekstin kiertoilmausta on huonompi kuin yksi teksti, koska ne maksavat "
                "kaksin verroin eivätkä ratkaise mitään."
            )
        value, violations = _one(base + extra)
        if value is None:
            return (
                f"FAILED: {channel} version {nro} did not meet the house rules after {_MAX_ATTEMPTS} "
                "attempts — " + "; ".join(violations) + f". Nothing was written to {key}.{inferred}"
            )
        made.append({"nro": nro, "kieli": lang, **value})

    # A single-version order keeps the old flat shape, so every existing reader and signal is
    # untouched. Several versions go in `versiot` — and for the script the first version's scenes are
    # ALSO mirrored at the top level, because the published success_signal counts `kohtaukset` there
    # and a multi-version script would otherwise read as "produced nothing".
    if len(made) == 1:
        value = {k: v for k, v in made[0].items() if k not in ("nro", "kieli")}
    else:
        value = {"versiot": made}
        if structured:
            value.update({k: made[0].get(k) for k in ("kesto_s", "muoto", "kohtaukset", "kuvapyynnot") if k in made[0]})

    written = _aimeat_call(
        agent_name,
        "aimeat_memory_write",
        {
            "key": key,
            "value": value,
            "visibility": "owner",
            "tags": ["julkaisupoyta", channel, f"ref:{run_id}"],
            # A model wrote this from the editor's dug-out angle, at the owner's direction, and a
            # person reads it at the workflow's gate BEFORE anything happens to it — but that gate is
            # after this write, so at write time human involvement is honestly NONE.
            "ai_provenance": declare(
                Level.SYNTHESIZED,
                method=Method.SYNTHESIZED,
                human_involvement=HumanInvolvement.NONE,
                model=resolved_model(llm),
                provider=resolved_provider(),
                notes=f"KANSI {channel} from the angle a person chose ({VALINTA_KEY.format(ref=run_id)}); not published anywhere.",
            ),
        },
    )
    if written is None:
        return f"FAILED to write '{key}' (tunnel/transport) — the piece did not land.{inferred}"
    record_deliverable_key(task_id, key)
    if structured:
        first = made[0]
        size = f"{len(first.get('kohtaukset') or [])} kohtausta, {len(first.get('kuvapyynnot') or [])} kuvapyyntöä"
    else:
        size = f"{len(made[0]['text'])} chars, notes {len(made[0]['notes'])} chars"
    how_many = f"{len(made)} versio(ta) [{', '.join(m['kieli'] for m in made)}], " if len(made) > 1 else ""
    directed = "" if slot_is_directed(order, channel) else " Ohjaus ei koskenut tätä osaa (vaikuttaa)."
    return f"OK: {spec['what']} -> {key} ({how_many}{size}). Not posted anywhere.{capped}{directed}{inferred}"


# ── the images ───────────────────────────────────────────────────────────────────────────────────
def read_kuvapyynnot(agent_name: str, ref: str) -> list[dict]:
    """The script's image requests. Raises when the script is missing — this agent generates only
    what the script asked for, and invents no prompts of its own."""
    key = PIECE_KEY.format(ref=ref, channel="video")
    value = read_owner_key(agent_name, key)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = None
    if not isinstance(value, dict):
        raise LookupError(f"script '{key}' is missing — the video step has not run. Nothing was generated.")
    reqs = [r for r in (value.get("kuvapyynnot") or []) if isinstance(r, dict) and str(r.get("prompt") or "").strip()]
    if not reqs:
        raise LookupError(
            f"script '{key}' asks for no images (every shot is a screen recording). Nothing was "
            "generated — that is the script's decision, not a failure to carry out."
        )
    return reqs


def tee_kuvat(agent_name: str, task: dict | None = None, task_id: str | None = None) -> str:
    """Generate one image per `kuvapyynto`, upload each public, write the run's kuvat key.

    The address comes from `run_address` — the dispatch's `deliverable_key`, else its variables, else
    today's date — and the script is read from the SAME run's id, never a generated one.

    No model runs here: the prompts were written by the script. Both the public URL and the STORAGE
    KEY are recorded, because the app attaches the image to a published post by key and a URL alone
    cannot be attached. A partial run is reported as partial and still stores what succeeded — an
    image that cost real money is not thrown away because a later one failed.
    """
    from crewaimeat.seedream_gen import generate_image

    key, run_id, rule = run_address(task, "kuvat")
    inferred = f" Address: {rule}."
    print(f"[{agent_name}] kuvat -> {key} ({rule})", file=sys.stderr)
    try:
        reqs = read_kuvapyynnot(agent_name, run_id)
    except LookupError as exc:
        print(f"[{agent_name}] {exc}", file=sys.stderr)
        return f"FAILED: {exc}{inferred}"

    kuvat, failed = [], []
    for r in reqs:
        prompt = str(r.get("prompt") or "").strip()
        res = generate_image(agent_name, prompt, size="2K", aspect_ratio="9:16")
        if not res.get("ok"):
            failed.append(f"nro {r.get('nro')}: {res.get('error')}")
            print(f"[{agent_name}] image for shot {r.get('nro')} FAILED: {res.get('error')}", file=sys.stderr)
            continue
        if not res.get("key"):
            failed.append(f"nro {r.get('nro')}: uploaded but returned no storage key — it cannot be attached")
            continue
        kuvat.append({"nro": r.get("nro"), "url": res["url"], "storage_key": res["key"], "prompt": prompt})
    if not kuvat:
        return (
            f"FAILED: no image was generated for {key} — " + "; ".join(failed or ["no requests succeeded"]) + inferred
        )

    written = _aimeat_call(
        agent_name,
        "aimeat_memory_write",
        {
            "key": key,
            "value": {"kuvat": kuvat},
            "visibility": "owner",
            "tags": ["julkaisupoyta", "kuvat", f"ref:{run_id}"],
            "ai_provenance": declare(
                Level.SYNTHESIZED,
                method=Method.SYNTHESIZED,
                human_involvement=HumanInvolvement.NONE,
                model="bytedance/seedream-4-5",
                provider="openrouter",
                notes=f"julkaisupöytä images for {PIECE_KEY.format(ref=run_id, channel='video')}; not published anywhere.",
            ),
        },
    )
    if written is None:
        return f"FAILED to write '{key}' (tunnel/transport) — {len(kuvat)} image(s) were uploaded but not recorded.{inferred}"
    record_deliverable_key(task_id, key)
    note = f" {len(failed)} request(s) failed: {'; '.join(failed)}." if failed else ""
    return f"OK: {len(kuvat)}/{len(reqs)} image(s) -> {key}, each with its public URL and storage key.{note}{inferred}"


# ── crew tools ───────────────────────────────────────────────────────────────────────────────────
def make_julkaisu_tools(agent_name: str, channel: str, task: dict | None = None, prompt: str | None = None) -> list:
    """The crew's ONE tool, with this run's ADDRESS already resolved from the dispatch.

    The tool takes no key and no id. The model cannot type the one thing that decides where the run
    lands, and so cannot make one up — which is exactly what it did on three prod runs before this.
    """
    from crewai.tools import tool

    task_id = (task or {}).get("id")
    reset_deliverable_key(task_id)

    @tool("write_julkaisu")
    def write_julkaisu_tool() -> str:
        """Write this run's piece: read the editor's material for this run and produce the finished
        text + notes into the key THIS RUN WAS GIVEN. Takes no arguments — the key comes from the
        task, never from you. Call it EXACTLY ONCE, then report what it returns. It posts nothing."""
        return write_julkaisu(agent_name, channel, task, task_id=task_id)

    write_julkaisu_tool.cache_function = lambda *_a, **_k: False
    return [write_julkaisu_tool]


def make_kuva_tools(agent_name: str, task: dict | None = None, prompt: str | None = None) -> list:
    """The image agent's ONE tool. Fully deterministic — the prompts come from the script."""
    from crewai.tools import tool

    task_id = (task or {}).get("id")
    reset_deliverable_key(task_id)

    @tool("tee_kuvat")
    def tee_kuvat_tool() -> str:
        """Generate the images this run's video script asked for, upload each to public storage, and
        record every one with BOTH its public URL and its storage key, at the key THIS RUN WAS GIVEN.
        Takes no arguments. Call it EXACTLY ONCE and report what it returns, including any failure."""
        return tee_kuvat(agent_name, task, task_id=task_id)

    tee_kuvat_tool.cache_function = lambda *_a, **_k: False
    return [tee_kuvat_tool]

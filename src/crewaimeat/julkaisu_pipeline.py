"""JULKAISUPÖYTÄ — one brief in, one channel piece out. The loop is code; the model writes prose.

Three task-runner agents (`julkaisu-linkedin`, `julkaisu-x`, `julkaisu-video`) are steps of the
`julkaisupoyta` workflow. Each reads the SAME input — the owner-visible brief at
``julkaisu.<ref>.brief`` — and writes ONE output of its own: ``julkaisu.<ref>.<channel>``, an object
with ``text`` (the finished piece) and ``notes`` (what it left out and why). Nothing is posted
anywhere and nobody is contacted: a person reads the pieces at the workflow's human-input gate and
decides what happens to them.

Everything except the prose is deterministic, for the reason the space-weather crew exists: an LLM
that resolves its own output key guesses, and a guessed key writes the run into the wrong place (or
into the same place every run). So:

  * ``<ref>`` is RESOLVED IN CODE from the dispatched task (``resolve_ref``) and bound into the tool
    — the model never types a key. A workflow step can arrive with no run vars at all (the node sends
    the WORKFLOW's description; measured on the Sanomat pipeline), so there is one last resort —
    ``_newest_brief_ref`` — and it is announced in the report rather than applied quietly.
  * the brief is read and REQUIRED (``read_brief``); a missing brief fails loud, it is never invented.
  * the piece is CHECKED against the channel's house rules (``check_linkedin`` / ``check_x`` /
    ``check_video``) — length, post count, hashtag pile, banned openers, stock-footage directions.
    A violation is fed back and the model rewrites; after ``_MAX_ATTEMPTS`` tries nothing is written
    and the run reports which rule failed, so the workflow step goes output-RED with a reason.
  * the write names the exact key, and records it as the task's ``deliverable_key`` so the task
    points at the piece rather than at the wrapper's report.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

from aimeat_crewai.provenance import HumanInvolvement, Level, Method, declare

from crewaimeat.aimeat_crew import _aimeat_call, record_deliverable_key, reset_deliverable_key
from crewaimeat.llm import get_llm, resolved_model, resolved_provider
from crewaimeat.memory_tools import read_owner_key
from crewaimeat.prose_style import FINNISH_NATIVE_STYLE

# The workflow variable. Every key this pipeline touches is julkaisu.<ref>.<something>; `{ref}` stays
# LITERAL in the published offers (the engine substitutes it per run) and concrete here (the run has
# resolved it). Charset kept to what a memory key segment safely carries.
_REF_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
BRIEF_KEY = "julkaisu.{ref}.brief"
PIECE_KEY = "julkaisu.{ref}.{channel}"

# The brief fields the crews were specified around. `aihe` is the one that cannot be missing — a
# piece with no subject is not a piece.
BRIEF_FIELDS = ("aihe", "mika_valmistui", "miksi_kiinnostaa", "kohdeyleiso")

_MAX_ATTEMPTS = 3  # first write + two rewrites against the violations we hand back

_TEXT_TAG = re.compile(r"<TEKSTI>(.*?)</TEKSTI>", re.S | re.I)
_NOTES_TAG = re.compile(r"<HUOMIOT>(.*?)</HUOMIOT>", re.S | re.I)

_OUTPUT_CONTRACT = (
    "\n\nVASTAUKSEN MUOTO — täsmälleen nämä kaksi lohkoa, ei mitään muuta:\n"
    "<TEKSTI>\n(valmis teksti sellaisenaan, ei otsikkoa 'teksti', ei lainausmerkkejä ympärillä)\n</TEKSTI>\n"
    "<HUOMIOT>\n(1–4 lyhyttä riviä: mitä jätit pois briiffistä ja miksi)\n</HUOMIOT>"
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


def resolve_ref(task: dict | None, prompt: str | None) -> str | None:
    """The run's `ref`, resolved DETERMINISTICALLY from the dispatched task — never by the model.

    Three places, most explicit first: a `ref` field anywhere in the task record (the workflow's own
    run params travel there), a `julkaisu.<ref>.brief` key named in the task text, and a bare
    `ref: <value>` / `ref=<value>`. Returns None when the task carries no ref at all — the caller
    fails loud rather than picking one, because a guessed ref writes over another run's piece.
    """
    for k, v in _walk(task or {}):
        if str(k).lower() in ("ref", "julkaisu_ref") and isinstance(v, str) and _REF_RE.match(v.strip()):
            return v.strip()
    blob = f"{json.dumps(task or {}, ensure_ascii=False, default=str)}\n{prompt or ''}"
    m = re.search(r"julkaisu\.([a-z0-9][a-z0-9._-]*?)\.(?:brief|linkedin|x|video)\b", blob, re.I)
    if m:
        return m.group(1).lower()
    m = re.search(r"\bref\s*[:=]\s*[\"']?([a-z0-9][a-z0-9._-]*)", prompt or "", re.I)
    if m:
        return m.group(1).lower()
    return None


def _newest_brief_ref(agent_name: str) -> tuple[str | None, str]:
    """Last resort when the dispatch named no ref: the brief this run must be about. (ref, why).

    A workflow step arrives with the WORKFLOW's description, not the step's — measured on the Sanomat
    pipeline, whose agents receive "Evening edition pipeline: fetch raw → write …" and no run vars at
    all. So a step may genuinely have no ref to read, and the choice is between an unrunnable agent
    and a rule.

    The rule is not a guess about which run: the step only dispatches once its input gate saw
    `julkaisu.{ref}.brief`, and the app writes that brief seconds before starting the run — so the
    NEWEST brief is this run's brief. It is applied only when it is UNAMBIGUOUS (one brief, or one
    strictly newest), it is announced in the report and on stderr, and it cannot hide a mistake: the
    step's success_signal is keyed on the real `{ref}`, so a wrong pick lands the piece somewhere the
    workflow then reads as output-RED, with the chosen ref written down next to it.
    """
    r = _aimeat_call(agent_name, "aimeat_memory_list", {"owner_scope": True, "prefix": "julkaisu.", "limit": 200}) or {}
    briefs = []
    for it in r.get("items") or []:
        m = re.match(r"^julkaisu\.(.+)\.brief$", str((it or {}).get("key") or ""))
        if m and _REF_RE.match(m.group(1)):
            briefs.append((str(it.get("updated_at") or it.get("updatedAt") or ""), m.group(1)))
    if not briefs:
        return None, "no julkaisu.*.brief exists on this node at all"
    if len(briefs) == 1:
        return briefs[0][1], f"it is the only brief on the node ({BRIEF_KEY.format(ref=briefs[0][1])})"
    briefs.sort()
    newest, runner_up = briefs[-1], briefs[-2]
    if newest[0] and newest[0] > runner_up[0]:
        return newest[1], f"it is the most recently written brief ({newest[0]})"
    tied = ", ".join(sorted(b[1] for b in briefs if b[0] == newest[0]))
    return None, f"several briefs share the newest timestamp ({tied}) — which run this is cannot be told apart"


def read_brief(agent_name: str, ref: str) -> dict:
    """The owner-visible brief at julkaisu.<ref>.brief, as a dict. Raises when it is not there.

    The brief is written by a person (or the KANSI app) before the run and read across agents, so the
    lookup is owner-scope. A missing or empty brief is a stop, never a prompt to invent a subject.
    """
    key = BRIEF_KEY.format(ref=ref)
    value = read_owner_key(agent_name, key)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = {"aihe": value}
    if not isinstance(value, dict) or not str(value.get("aihe") or "").strip():
        raise LookupError(
            f"brief '{key}' is missing or has no 'aihe' — the upstream step has not written it. "
            "Nothing was written; the brief is the input, and it is not invented here."
        )
    return value


def brief_block(brief: dict) -> str:
    """The brief rendered for a prompt — the specified fields, plus anything else the writer added."""
    labels = {
        "aihe": "AIHE",
        "mika_valmistui": "MIKÄ VALMISTUI",
        "miksi_kiinnostaa": "MIKSI KIINNOSTAA",
        "kohdeyleiso": "KOHDEYLEISÖ",
        "lahde": "LÄHDE",
    }
    rows = []
    for field, label in labels.items():
        v = str(brief.get(field) or "").strip()
        if v:
            rows.append(f"{label}: {v}")
    for k, v in brief.items():
        if k not in labels and k != "ref" and isinstance(v, (str, int, float)) and str(v).strip():
            rows.append(f"{k.upper()}: {v}")
    return "\n".join(rows)


# ── house rules, checked in code ─────────────────────────────────────────────────────────────────
_HASHTAG = re.compile(r"(?<![\w#])#[\wåäöÅÄÖ][\wåäöÅÄÖ-]*")
_BRACKET = re.compile(r"\[[^\]]+\]")
# Leading pictographs / bullet glyphs — an emoji bullet list, which the X thread must not use.
_EMOJI_BULLET = re.compile(
    r"^[ \t]*[•▪▶►\u2190-\u21FF\u2300-\u23FF\u25A0-\u27BF\u2B00-\u2BFF\uFE0F\U0001F000-\U0001FAFF]",
    re.M,
)


def check_linkedin(text: str) -> list[str]:
    """House rules for the Finnish LinkedIn post. Returns the violations, in Finnish (they are fed
    straight back into a Finnish prompt, where an English instruction would invite translationese)."""
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


def check_x(text: str) -> list[str]:
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
    return bad


_VIDEO_STOCK = ("stock", "arkistokuv", "kuvituskuv", "geneerinen", "b-roll", "kuvapankki")
# Finnish narration runs ~2.2–2.7 words/second, so a 45–75 s script is ~100–190 words. The envelope
# is deliberately wider than the target: it catches a script that is nowhere near the length, not a
# script that is five words long.
_VIDEO_WORDS = (85, 210)


def video_shots(text: str) -> list[str]:
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def check_video(text: str) -> list[str]:
    """House rules for the Finnish 9:16 script: shots, a real thing on screen, the claim up front."""
    bad: list[str] = []
    shots = video_shots(text)
    if len(shots) < 3:
        bad.append(f"kuvia on {len(shots)} — kirjoita vähintään kolme, yksi rivi per kuva.")
    missing = [i for i, s in enumerate(shots, 1) if not _BRACKET.search(s)]
    if missing:
        bad.append(
            f"kuvista {', '.join(map(str, missing[:5]))} puuttuu hakasulkeissa oleva kuvaustieto — "
            "jokainen rivi on puhuttu repliikki + [mitä ruudussa näkyy]."
        )
    narration = _BRACKET.sub(" ", text or "")
    words = len(narration.split())
    if not _VIDEO_WORDS[0] <= words <= _VIDEO_WORDS[1]:
        bad.append(
            f"puhetta on {words} sanaa — 45–75 sekunnin käsikirjoitus on noin "
            f"{_VIDEO_WORDS[0]}–{_VIDEO_WORDS[1]} sanaa."
        )
    for shot in shots:
        note = " ".join(_BRACKET.findall(shot)).lower()
        for token in _VIDEO_STOCK:
            if token in note:
                bad.append(f"kuvaustieto '{note[:60]}' on kuvituskuvaohje ('{token}') — näytä jotain mikä on olemassa.")
                break
    if shots and "logo" in " ".join(_BRACKET.findall(shots[0])).lower():
        bad.append("ensimmäisessä kuvassa on logo — kolme ensimmäistä sekuntia kuuluvat väitteelle.")
    return bad


# ── the three channels ───────────────────────────────────────────────────────────────────────────
def _linkedin_prompt(brief: dict) -> str:
    return (
        "Olet suomalainen kirjoittaja, joka tekee tuotejulkaisuista LinkedIn-postauksia. Kirjoita YKSI "
        "postaus suomeksi tästä briiffistä.\n\n"
        "TALON SÄÄNNÖT:\n"
        "- Pituus 600–1200 merkkiä.\n"
        "- Lukijan konkreettinen hyöty ensimmäisessä kappaleessa, ei kolmannessa.\n"
        "- Ei aihetunnistekasaa lopussa: korkeintaan kaksi, mieluiten ei yhtään.\n"
        "- Ei aloitusta 'olen innoissani', ei fraasia 'tässä muutama ajatus', ei retorista kysymystä "
        "ensimmäiseksi riviksi.\n"
        "- Kirjoita vain siitä mitä briiffissä on. Älä keksi lukuja, asiakkaita tai lupauksia.\n\n"
        "BRIIFFI:\n" + brief_block(brief) + FINNISH_NATIVE_STYLE + _OUTPUT_CONTRACT
    )


def _x_prompt(brief: dict) -> str:
    return (
        "You write X threads for a software product. Write ONE thread in ENGLISH from this brief.\n\n"
        "HOUSE RULES:\n"
        "- 3 to 6 posts. Separate every post with a BLANK LINE. Each post under 280 characters.\n"
        "- Post 1 has to stand alone as a claim worth reading — no '🧵', no 'a thread:', no announcement.\n"
        "- No emoji bullets anywhere.\n"
        "- The last post says what a reader can do next. Do not ask for a follow, a like or a retweet.\n"
        "- Only what the brief says. Invent no numbers, customers or promises.\n\n"
        "BRIEF (in Finnish — the thread is in English):\n" + brief_block(brief) + _OUTPUT_CONTRACT
    )


def _video_prompt(brief: dict) -> str:
    return (
        "Olet käsikirjoittaja, joka tekee pystyvideoita (9:16, 45–75 sekuntia). Kirjoita YKSI käsikirjoitus "
        "suomeksi tästä briiffistä.\n\n"
        "TALON SÄÄNNÖT:\n"
        "- Kirjoita kuvina: yksi rivi per kuva = puhuttu repliikki + hakasulkeissa mitä ruudussa näkyy.\n"
        "- Kolme ensimmäistä sekuntia kantavat väitteen, eivät logoa.\n"
        "- Ruudussa näkyy jotain mikä on olemassa: oikea näkymä tuotteesta, oikea luku. Ei kuvituskuvaa, "
        "ei arkistokuvaohjeita.\n"
        "- Lopeta siihen mitä katsoja toistaisi kollegalleen.\n"
        "- Puhetta noin 100–190 sanaa. Vain se mitä briiffissä on — älä keksi lukuja.\n\n"
        "ESIMERKKI YHDESTÄ RIVISTÄ:\n"
        "Yhteys valmistuu vasta kun olet päättänyt mitä agentti saa tehdä. [ruutu: hyväksymisikkuna, "
        "oikeusvalinnat näkyvissä]\n\n"
        "BRIIFFI:\n" + brief_block(brief) + FINNISH_NATIVE_STYLE + _OUTPUT_CONTRACT
    )


CHANNELS: dict[str, dict] = {
    "linkedin": {
        "agent": "julkaisu-linkedin",
        "what": "LinkedIn-postaus (suomi, 600–1200 merkkiä)",
        "prompt": _linkedin_prompt,
        "check": check_linkedin,
        "temperature": 0.6,
        "retry_lead": "Korjaa nämä ja kirjoita postaus uudestaan kokonaan:",
    },
    "x": {
        "agent": "julkaisu-x",
        "what": "X thread (English, 3–6 posts)",
        "prompt": _x_prompt,
        "check": check_x,
        "temperature": 0.7,
        "retry_lead": "Fix these and write the whole thread again:",
    },
    "video": {
        "agent": "julkaisu-video",
        "what": "pystyvideon käsikirjoitus (suomi, 9:16, 45–75 s)",
        "prompt": _video_prompt,
        "check": check_video,
        "temperature": 0.7,
        "retry_lead": "Korjaa nämä ja kirjoita käsikirjoitus uudestaan kokonaan:",
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
def write_julkaisu(agent_name: str, channel: str, ref: str, task_id: str | None = None) -> str:
    """Read julkaisu.<ref>.brief, write ONE checked piece to julkaisu.<ref>.<channel>. Returns a report.

    Nothing is posted and nobody is contacted — the piece lands in owner memory for the workflow's
    human-input gate. A missing brief, a model that will not meet the house rules, or a failed write
    all return a FAILED report and write nothing, so the step's success_signal reads the same absence.
    """
    spec = CHANNELS.get(channel)
    if spec is None:
        raise KeyError(f"unknown channel {channel!r} (known: {', '.join(sorted(CHANNELS))})")
    inferred = ""
    if not (ref and _REF_RE.match(str(ref).strip())):
        ref, why = _newest_brief_ref(agent_name)
        if not ref:
            return (
                f"FAILED: this run named no ref and none could be resolved — {why}. Nothing was "
                "written; a guessed ref would write over another run's piece. Name the ref in the "
                "workflow step (a `ref` param, or the key julkaisu.<ref>.brief in its text)."
            )
        inferred = f" ref '{ref}' was not in the dispatch — took it because {why}."
        print(f"[{agent_name}] {channel}:{inferred.strip()}", file=sys.stderr)
    ref = str(ref).strip()
    key = PIECE_KEY.format(ref=ref, channel=channel)
    try:
        brief = read_brief(agent_name, ref)
    except LookupError as exc:
        print(f"[{agent_name}] {exc}", file=sys.stderr)
        return f"FAILED: {exc}"

    llm = get_llm(for_tool_use=False, temperature=spec["temperature"], agent_name=agent_name)
    base = spec["prompt"](brief)
    prompt, text, notes, violations = base, "", "", ["(no attempt ran)"]
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        out = llm.call([{"role": "user", "content": prompt}])
        text, notes = parse_piece(out)
        if not text:
            violations = ["vastauksesta puuttui <TEKSTI>-lohko / the reply had no <TEKSTI> block."]
        else:
            violations = spec["check"](text)
        print(
            f"[{agent_name}] {channel} attempt {attempt}/{_MAX_ATTEMPTS}: "
            + ("OK" if not violations else "; ".join(violations)),
            file=sys.stderr,
        )
        if not violations:
            break
        prompt = (
            base
            + "\n\n"
            + spec["retry_lead"]
            + "\n"
            + "\n".join(f"- {v}" for v in violations)
            + "\n\nEDELLINEN VERSIO / PREVIOUS VERSION:\n"
            + (text or "(tyhjä)")
        )
    if violations:
        return (
            f"FAILED: {channel} did not meet the house rules after {_MAX_ATTEMPTS} attempts — "
            + "; ".join(violations)
            + f". Nothing was written to {key}."
        )

    written = _aimeat_call(
        agent_name,
        "aimeat_memory_write",
        {
            "key": key,
            "value": {"text": text, "notes": notes},
            "visibility": "owner",
            "tags": ["julkaisupoyta", channel, f"ref:{ref}"],
            # A model wrote this from the owner's own brief, at the owner's direction, and a person
            # reads it at the workflow's gate BEFORE anything happens to it — but that gate is after
            # this write, so at write time human involvement is honestly NONE.
            "ai_provenance": declare(
                Level.SYNTHESIZED,
                method=Method.SYNTHESIZED,
                human_involvement=HumanInvolvement.NONE,
                model=resolved_model(llm),
                provider=resolved_provider(),
                notes=f"julkaisupöytä {channel} from {BRIEF_KEY.format(ref=ref)}; not published anywhere.",
            ),
        },
    )
    if written is None:
        return f"FAILED to write '{key}' (tunnel/transport) — the piece did not land."
    # The task must point at the PIECE, not at this wrapper's report.
    record_deliverable_key(task_id, key)
    return f"OK: {spec['what']} -> {key} ({len(text)} chars, notes {len(notes)} chars). Not posted anywhere.{inferred}"


def make_julkaisu_tools(agent_name: str, channel: str, task: dict | None = None, prompt: str | None = None) -> list:
    """The crew's ONE tool, with the run's ref already resolved and bound.

    The tool takes no key and no ref: the model cannot mistype the one thing that decides where the
    run lands. If the dispatch carried no ref the tool says so and writes nothing.
    """
    from crewai.tools import tool

    ref = resolve_ref(task, prompt)
    task_id = (task or {}).get("id")
    reset_deliverable_key(task_id)

    @tool("write_julkaisu")
    def write_julkaisu_tool() -> str:
        """Write this run's piece: read the brief for this run and produce the finished text + notes
        into the run's own memory key. Takes no arguments — the run's ref and key are already resolved.
        Call it EXACTLY ONCE, then report what it returns. It does not post anything anywhere."""
        return write_julkaisu(agent_name, channel, ref or "", task_id=task_id)

    write_julkaisu_tool.cache_function = lambda *_a, **_k: False
    return [write_julkaisu_tool]

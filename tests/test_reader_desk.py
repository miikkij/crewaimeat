"""Lukijoilta-desk deterministic parts: edition targeting (the 17:30 cutoff must roll a late tip to
the NEXT day so it never races the 18:00 desk write), the tip append (read-modify-write, fail loud on
a dead write), owner detection (human owner vs sibling agent vs stranger), and the interview body.
All deterministic, no network, no LLM."""

import datetime
from zoneinfo import ZoneInfo

import pytest

from crewaimeat import reader_desk as rd

_TZ = ZoneInfo("Europe/Helsinki")


# ── edition targeting ─────────────────────────────────────────────────────────
def test_next_edition_before_cutoff_is_today():
    now = datetime.datetime(2026, 7, 13, 12, 0, tzinfo=_TZ)
    assert rd.next_evening_edition(now) == ("2026-07-13", "evening")


def test_next_edition_at_cutoff_rolls_to_tomorrow():
    now = datetime.datetime(2026, 7, 13, 17, 30, tzinfo=_TZ)
    assert rd.next_evening_edition(now) == ("2026-07-14", "evening")


def test_raw_key_shape_matches_write_pipeline():
    assert rd.raw_key("2026-07-13") == "news.2026-07-13.evening.raw.lukijoilta"


# ── add_tip: append + fail loud ───────────────────────────────────────────────
def _fake_store(existing: list | None):
    """A stateful _aimeat_call: read returns `existing`, write records the payload."""
    written = {}

    def _call(agent, tool, payload):
        if tool == "aimeat_memory_read":
            return {"value": existing} if existing is not None else {"value": None}
        if tool == "aimeat_memory_write":
            written.update(payload)
            return {"ok": True}
        raise AssertionError(f"unexpected tool {tool}")

    return _call, written


def test_add_tip_appends_to_existing_raw(monkeypatch):
    call, written = _fake_store([{"title": "vanha", "content": "eka vinkki", "source": "x"}])
    monkeypatch.setattr(rd, "_aimeat_call", call)
    date, edition = rd.add_tip("sanomat-desk", text="Kissa puussa.\nPalokunta paikalla.", source="lukijavinkki (u)")
    assert edition == "evening"
    items = written["value"]
    assert len(items) == 2
    assert items[1]["content"].startswith("Kissa puussa.")
    assert items[1]["title"] == "Kissa puussa."  # first line becomes the title
    assert written["key"] == rd.raw_key(date)
    assert written["visibility"] == "owner"  # same scope news-fetcher raw uses


def test_add_tip_carries_images(monkeypatch):
    call, written = _fake_store(None)
    monkeypatch.setattr(rd, "_aimeat_call", call)
    rd.add_tip("sanomat-desk", text="Juttu", source="s", images=["https://node/v1/pub/g/k.jpg"])
    assert written["value"][0]["images"] == ["https://node/v1/pub/g/k.jpg"]


def test_add_tip_raises_when_write_fails(monkeypatch):
    monkeypatch.setattr(rd, "_aimeat_call", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="tip write failed"):
        rd.add_tip("sanomat-desk", text="x", source="s")


# ── owner detection ───────────────────────────────────────────────────────────
def test_is_owner_human(monkeypatch):
    monkeypatch.setattr(rd, "_discover_owner", lambda agent: "happydude500001")
    assert rd.is_owner_human("sanomat-desk", "happydude500001@aimeat.io")
    assert not rd.is_owner_human("sanomat-desk", "concierge#happydude500001@aimeat.io")  # sibling agent
    assert not rd.is_owner_human("sanomat-desk", "stranger@aimeat.io")
    assert not rd.is_owner_human("sanomat-desk", None)


# ── interview body ────────────────────────────────────────────────────────────
def test_build_interview_mentions_date_and_thread():
    body = rd.build_interview("2026-07-13")
    assert "2026-07-13" in body
    assert "haastattelu" in body.lower()


# ── refined tips: the sanomat-vinkki fenced block (TARGET-035) ───────────────
_BLOCK = (
    "Uutisvinkki:\n```sanomat-vinkki\n"
    '{"type": "haastattelu", "title": "Päivä joka ei kaatunut", '
    '"content": "Aamulla kaatui kahvi, iltapäivällä kaatui palvelin, ilta pystyssä.", '
    '"language": "fi"}\n```\nterv. lukija'
)


def test_parse_vinkki_block_happy_path():
    parsed = rd.parse_vinkki_block(_BLOCK)
    assert parsed == {
        "type": "haastattelu",
        "title": "Päivä joka ei kaatunut",
        "content": "Aamulla kaatui kahvi, iltapäivällä kaatui palvelin, ilta pystyssä.",
        "language": "fi",
    }


def test_parse_vinkki_block_defaults_type_and_clamps_title():
    long_title = "x" * 300
    text = f'```sanomat-vinkki\n{{"type": "runo", "title": "{long_title}", "content": "sisältö"}}\n```'
    parsed = rd.parse_vinkki_block(text)
    assert parsed is not None
    assert parsed["type"] == "vinkki"  # unknown type falls back
    assert len(parsed["title"]) == 120
    assert parsed["language"] is None


@pytest.mark.parametrize(
    "text",
    [
        "pelkkä tavallinen vinkki ilman blokkia",
        "```sanomat-vinkki\nei jsonia ollenkaan\n```",
        '```sanomat-vinkki\n{"title": "", "content": "x"}\n```',  # empty title
        '```sanomat-vinkki\n{"title": "x"}\n```',  # missing content
        "```sanomat-vinkki\n[1, 2]\n```",  # not an object
        "",
    ],
)
def test_parse_vinkki_block_falls_back_to_none(text):
    assert rd.parse_vinkki_block(text) is None


def test_add_tip_refined_carries_provenance(monkeypatch):
    call, written = _fake_store(None)
    monkeypatch.setattr(rd, "_aimeat_call", call)
    rd.add_tip(
        "sanomat-desk",
        text="Jalostettu sisältö.",
        source="lukijavinkki (u)",
        title="Oma otsikko",
        refined=True,
        tip_type="haastattelu",
    )
    entry = written["value"][0]
    assert entry["title"] == "Oma otsikko"
    assert entry["refined"] is True
    assert entry["tip_type"] == "haastattelu"


def test_add_tip_plain_has_no_refined_marker(monkeypatch):
    call, written = _fake_store(None)
    monkeypatch.setattr(rd, "_aimeat_call", call)
    rd.add_tip("sanomat-desk", text="Tavallinen vinkki", source="s", tip_type="vinkki")
    entry = written["value"][0]
    assert "refined" not in entry
    assert "tip_type" not in entry  # plain "vinkki" adds no field

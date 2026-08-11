"""Resilience of the deterministic write path against a transient serve-tunnel drop (the 06-20
Sanomat incident): a failed READ must NOT look like empty raw (which silently drops a category),
and an incomplete desk must fail LOUD (WriteIncomplete) so the step is retried — never a silent
partial. Also: the shared dispatcher classifies transport failures (retry) vs tool errors (fail fast).
All deterministic, no network, no LLM."""

import pytest

from crewaimeat import write_pipeline as wp
from crewaimeat.aimeat_crew import _is_transient_error


# ── dispatcher: which failures are worth retrying ─────────────────────────────
def test_transient_classification():
    assert _is_transient_error({"code": "TOOL_CALL_ERROR", "message": "Tunnel not connected"})
    assert _is_transient_error("connection reset by peer")
    assert _is_transient_error("HTTP 503 service unavailable")
    # tool-level errors are NOT transient — they must fail fast (a missing key, a validation reject)
    assert not _is_transient_error({"code": "NOT_FOUND", "message": "key does not exist"})
    assert not _is_transient_error("validation failed: bad payload")
    assert not _is_transient_error(None)


# ── _read_raw: failed read ≠ empty raw ────────────────────────────────────────
def _fake_call(read_ret, list_ret):
    def _call(agent, tool, payload, **_kw):
        return read_ret if tool == "aimeat_memory_read" else list_ret

    return _call


def test_read_raw_returns_content_when_present(monkeypatch):
    raw = [{"content": "x" * 300}]
    monkeypatch.setattr(wp, "_aimeat_call", _fake_call({"value": raw}, None))
    assert wp._read_raw("news-writer", "urheilu", "2026-06-20", "evening") == raw


def test_read_raw_empty_when_list_succeeds_but_absent(monkeypatch):
    # own read has no value, owner-scope list SUCCEEDS (returns a dict) but has no matching key → []
    monkeypatch.setattr(wp, "_aimeat_call", _fake_call({"value": None}, {"items": []}))
    assert wp._read_raw("news-writer", "saa", "2026-06-20", "evening") == []


def test_read_raw_raises_on_transport_failure(monkeypatch):
    # both calls return None (transport failure persisted through the dispatcher's retries) → RawReadError
    monkeypatch.setattr(wp, "_aimeat_call", _fake_call(None, None))
    with pytest.raises(wp.RawReadError):
        wp._read_raw("news-writer", "urheilu", "2026-06-20", "evening")


# ── _read_edition_raw: the consolidated record, and the absence that means "old edition" ──
RAW_RECORD = {
    "fetchedAt": "2026-08-09T14:00:00+00:00",
    "categories": {"talous": [{"content": "x" * 300}], "urheilu": [{"content": "y" * 300}]},
}


def test_read_edition_raw_returns_categories_map(monkeypatch):
    monkeypatch.setattr(wp, "_aimeat_call", _fake_call({"value": RAW_RECORD}, None))
    got = wp._read_edition_raw("news-writer", "2026-08-09", "evening")
    assert set(got) == {"talous", "urheilu"} and got["talous"][0]["content"].startswith("x")


def test_read_edition_raw_parses_a_json_string_value(monkeypatch):
    import json

    monkeypatch.setattr(wp, "_aimeat_call", _fake_call({"value": json.dumps(RAW_RECORD)}, None))
    assert set(wp._read_edition_raw("news-writer", "2026-08-09", "evening")) == {"talous", "urheilu"}


def test_read_edition_raw_is_none_for_a_pre_consolidation_edition(monkeypatch):
    """The owner-scope list SUCCEEDS but carries only the OLD per-category keys — whose prefix the
    new key's prefix also matches. None (not {}) is what tells the desk to use the fallback."""
    old = {"items": [{"key": "news.2026-06-20.evening.raw.talous", "value": [{"content": "x"}]}]}
    monkeypatch.setattr(wp, "_aimeat_call", _fake_call({"value": None}, old))
    assert wp._read_edition_raw("news-writer", "2026-06-20", "evening") is None


def test_read_edition_raw_raises_on_transport_failure(monkeypatch):
    monkeypatch.setattr(wp, "_aimeat_call", _fake_call(None, None))
    with pytest.raises(wp.RawReadError):
        wp._read_edition_raw("news-writer", "2026-08-09", "evening")


def test_write_uses_the_consolidated_raw_without_touching_the_old_keys(monkeypatch):
    """A category present in the ONE record is never looked up under its old key."""
    monkeypatch.setattr(wp, "get_llm", lambda **_k: _FakeLLM())
    monkeypatch.setattr(wp, "_publish_article", lambda *a, **k: True)
    monkeypatch.setattr(wp, "_read_edition_raw", lambda *a, **k: RAW_RECORD["categories"])

    def _boom(*_a, **_k):
        raise AssertionError("fell back to the per-category keys despite a consolidated raw")

    monkeypatch.setattr(wp, "_read_raw", _boom)
    report = wp.write_edition_articles("news-writer", "2026-08-09", "evening", ["talous", "urheilu"])
    assert "talous" in report and "urheilu" in report


def test_a_category_with_a_different_producer_still_reaches_the_desk(monkeypatch):
    """`lukijoilta` is written by sanomat-desk as reader tips arrive by DM — long after the 17:00
    fetch — so it is NOT in the fetcher's consolidated record and must still be picked up. Losing
    this silently would drop reader news from the paper with nothing to show for it."""
    monkeypatch.setattr(wp, "get_llm", lambda **_k: _FakeLLM())
    published = []
    monkeypatch.setattr(wp, "_publish_article", lambda _a, _d, _e, cat, *a, **k: published.append(cat) or True)
    monkeypatch.setattr(wp, "_read_edition_raw", lambda *a, **k: RAW_RECORD["categories"])
    monkeypatch.setattr(
        wp, "_read_raw", lambda _a, cat, *_r: [{"content": "tip " * 100}] if cat == "lukijoilta" else []
    )
    wp.write_edition_articles("news-writer", "2026-08-09", "evening", ["talous", "lukijoilta"])
    assert "lukijoilta" in published


def test_write_falls_back_to_old_keys_when_there_is_no_consolidated_raw(monkeypatch):
    """An edition published before the consolidation still writes — this is what makes the fetcher
    and the desks shippable in one deploy, and keeps the 68 published editions readable."""
    monkeypatch.setattr(wp, "get_llm", lambda **_k: _FakeLLM())
    monkeypatch.setattr(wp, "_publish_article", lambda *a, **k: True)
    monkeypatch.setattr(wp, "_read_edition_raw", lambda *a, **k: None)
    monkeypatch.setattr(wp, "_read_raw", lambda *a, **k: [{"content": "x" * 300}])
    report = wp.write_edition_articles("news-writer", "2026-06-20", "evening", ["talous"])
    assert "pre-consolidation" in report and "talous" in report


def test_write_is_incomplete_when_the_single_raw_read_fails(monkeypatch):
    """One read now carries the whole desk, so its transport failure must fail the desk LOUD —
    every category reported failed, so the step goes RED and is retried."""
    monkeypatch.setattr(wp, "get_llm", lambda **_k: _FakeLLM())

    def _boom(*_a, **_k):
        raise wp.RawReadError("tunnel down")

    monkeypatch.setattr(wp, "_read_edition_raw", _boom)
    with pytest.raises(wp.WriteIncomplete) as ei:
        wp.write_edition_articles("news-writer", "2026-08-09", "evening", ["talous", "urheilu"])
    assert ei.value.failed == ["talous", "urheilu"]


# ── write_edition_articles: incomplete desk fails loud ────────────────────────
class _FakeLLM:
    def call(self, *_a, **_k):
        return "OTSIKKO\n\n" + ("kappale. " * 60)  # > 200 chars


def test_write_raises_write_incomplete_on_read_failure(monkeypatch):
    monkeypatch.setattr(wp, "get_llm", lambda **_k: _FakeLLM())
    monkeypatch.setattr(wp, "_publish_article", lambda *a, **k: True)
    monkeypatch.setattr(wp, "_read_edition_raw", lambda *a, **k: None)  # exercise the fallback path

    def _read(agent, cat, date, edition):
        if cat == "urheilu":
            raise wp.RawReadError("tunnel down")
        return [{"content": "x" * 300}]

    monkeypatch.setattr(wp, "_read_raw", _read)
    with pytest.raises(wp.WriteIncomplete) as ei:
        wp.write_edition_articles("news-writer", "2026-06-20", "evening", ["talous", "urheilu"])
    assert "urheilu" in ei.value.failed
    assert "talous" not in ei.value.failed  # the readable category still got written


def test_write_raises_when_publish_fails(monkeypatch):
    monkeypatch.setattr(wp, "get_llm", lambda **_k: _FakeLLM())
    monkeypatch.setattr(wp, "_read_edition_raw", lambda *a, **k: None)  # exercise the fallback path
    monkeypatch.setattr(wp, "_read_raw", lambda *a, **k: [{"content": "x" * 300}])
    monkeypatch.setattr(wp, "_publish_article", lambda *a, **k: False)  # publish keeps failing
    with pytest.raises(wp.WriteIncomplete) as ei:
        wp.write_edition_articles("news-writer", "2026-06-20", "evening", ["talous"])
    assert ei.value.failed == ["talous"]


def test_write_clean_run_returns_report(monkeypatch):
    monkeypatch.setattr(wp, "get_llm", lambda **_k: _FakeLLM())
    monkeypatch.setattr(wp, "_read_edition_raw", lambda *a, **k: None)  # exercise the fallback path
    monkeypatch.setattr(wp, "_read_raw", lambda *a, **k: [{"content": "x" * 300}])
    monkeypatch.setattr(wp, "_publish_article", lambda *a, **k: True)
    report = wp.write_edition_articles("news-writer", "2026-06-20", "evening", ["talous", "tiede"])
    assert "talous" in report and "tiede" in report

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
    def _call(agent, tool, payload):
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


# ── write_edition_articles: incomplete desk fails loud ────────────────────────
class _FakeLLM:
    def call(self, *_a, **_k):
        return "OTSIKKO\n\n" + ("kappale. " * 60)  # > 200 chars


def test_write_raises_write_incomplete_on_read_failure(monkeypatch):
    monkeypatch.setattr(wp, "get_llm", lambda **_k: _FakeLLM())
    monkeypatch.setattr(wp, "_publish_article", lambda *a, **k: True)

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
    monkeypatch.setattr(wp, "_read_raw", lambda *a, **k: [{"content": "x" * 300}])
    monkeypatch.setattr(wp, "_publish_article", lambda *a, **k: False)  # publish keeps failing
    with pytest.raises(wp.WriteIncomplete) as ei:
        wp.write_edition_articles("news-writer", "2026-06-20", "evening", ["talous"])
    assert ei.value.failed == ["talous"]


def test_write_clean_run_returns_report(monkeypatch):
    monkeypatch.setattr(wp, "get_llm", lambda **_k: _FakeLLM())
    monkeypatch.setattr(wp, "_read_raw", lambda *a, **k: [{"content": "x" * 300}])
    monkeypatch.setattr(wp, "_publish_article", lambda *a, **k: True)
    report = wp.write_edition_articles("news-writer", "2026-06-20", "evening", ["talous", "tiede"])
    assert "talous" in report and "tiede" in report

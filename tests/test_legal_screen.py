"""Legal screen: the verdict parse is strict (a broken screen must never wave material through),
and only genuinely-flagged material is declined. All deterministic — the LLM is faked."""

import pytest

from crewaimeat import legal_screen as ls


class _FakeLLM:
    def __init__(self, reply):
        self.reply = reply

    def call(self, messages):
        return self.reply


def _screen(monkeypatch, reply):
    monkeypatch.setattr(ls, "get_llm", lambda **_k: _FakeLLM(reply))
    return ls.screen_external("sanomat-desk", sender="user@node", text="vinkki", attachment_notes="")


def test_pass_verdict(monkeypatch):
    v = _screen(monkeypatch, 'Tässä arvio: {"ok": true, "issues": [], "summary": "ei ongelmia"}')
    assert v["ok"] is True
    assert v["issues"] == []


def test_flag_verdict(monkeypatch):
    v = _screen(
        monkeypatch,
        '{"ok": false, "issues": ["1: nimetty yksityishenkilö", "3: osoite"], "summary": "yksityisyys"}',
    )
    assert v["ok"] is False
    assert len(v["issues"]) == 2


def test_unparseable_raises_never_passes(monkeypatch):
    with pytest.raises(ls.LegalScreenUnavailable):
        _screen(monkeypatch, "En osaa vastata JSONilla, pahoittelut.")


def test_missing_ok_field_raises(monkeypatch):
    with pytest.raises(ls.LegalScreenUnavailable):
        _screen(monkeypatch, '{"issues": [], "summary": "unohtui ok"}')


def test_llm_failure_raises(monkeypatch):
    class _Boom:
        def call(self, messages):
            raise ConnectionError("provider down")

    monkeypatch.setattr(ls, "get_llm", lambda **_k: _Boom())
    with pytest.raises(ls.LegalScreenUnavailable, match="LLM call failed"):
        ls.screen_external("sanomat-desk", sender="u@n", text="x")

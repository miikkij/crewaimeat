"""The fetch step writes ONE raw key, not one per category. Deterministic: no network, no scraping.

Measured on aimeat.io 2026-08-09: the evening run wrote 44 keys and news.* had reached 2,993 keys
over 68 edition-days with nothing ageing them out. 21 of the 44 were this step.
"""

import json

import pytest

from crewaimeat import fetch_pipeline as fp


class _Writes:
    def __init__(self, ret=True):
        self.calls = []
        self.ret = {"ok": True} if ret else None

    def __call__(self, agent, tool, payload, **_kw):
        self.calls.append({"tool": tool, "payload": payload})
        return self.ret


@pytest.fixture
def wired(monkeypatch):
    """Scraping stubbed out; status patches swallowed (covered in test_edition_status)."""
    monkeypatch.setattr(fp, "fetch_category_raw", lambda _a, c, **_k: [{"url": f"u/{c}", "content": "x" * 500}])
    monkeypatch.setattr(fp, "seed_status", lambda *a, **k: True)
    monkeypatch.setattr(fp, "step_status", _noop_status)
    writes = _Writes()
    monkeypatch.setattr(fp, "_aimeat_call", writes)
    return writes


class _noop_status:
    def __init__(self, *_a, **_k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def fail(self):
        pass


def test_one_key_holds_every_category(wired):
    fp.build_edition_raw("news-fetcher", "2026-08-09", "evening", ["talous", "tiede", "urheilu"])
    assert len(wired.calls) == 1, "the edition raw must be ONE write, not one per category"
    payload = wired.calls[0]["payload"]
    assert payload["key"] == "news.2026-08-09.evening.raw"
    assert set(payload["value"]["categories"]) == {"talous", "tiede", "urheilu"}
    assert payload["value"]["fetchedAt"]


def test_categories_sit_under_a_field_not_at_the_root(wired):
    """The success signal points at `categories`, and metadata has to live beside the payload."""
    fp.build_edition_raw("news-fetcher", "2026-08-09", "evening", ["talous"])
    value = wired.calls[0]["payload"]["value"]
    assert set(value) == {"fetchedAt", "categories"}
    assert "talous" not in value


def test_raw_expires_and_is_owner_visible(wired):
    fp.build_edition_raw("news-fetcher", "2026-08-09", "evening", ["talous"])
    payload = wired.calls[0]["payload"]
    assert payload["ttl_hours"] == 14 * 24  # scraped source material, not the published paper
    assert payload["visibility"] == "owner"


def test_an_empty_category_is_left_out_so_the_signal_counts_what_is_really_there(monkeypatch, wired):
    monkeypatch.setattr(fp, "fetch_category_raw", lambda _a, c, **_k: [] if c == "saa" else [{"content": "x"}])
    fp.build_edition_raw("news-fetcher", "2026-08-09", "evening", ["talous", "saa"])
    assert set(wired.calls[0]["payload"]["value"]["categories"]) == {"talous"}


def test_a_failing_category_never_costs_the_others(monkeypatch, wired):
    def _fetch(_a, c, **_k):
        if c == "tiede":
            raise RuntimeError("extractor blew up")
        return [{"content": "x"}]

    monkeypatch.setattr(fp, "fetch_category_raw", _fetch)
    report = fp.build_edition_raw("news-fetcher", "2026-08-09", "evening", ["talous", "tiede", "urheilu"])
    assert "tiede" in report and "RuntimeError" in report
    assert set(wired.calls[0]["payload"]["value"]["categories"]) == {"talous", "urheilu"}


def test_oversized_raw_is_shouted_about_before_it_413s(monkeypatch, wired, capsys):
    monkeypatch.setattr(fp, "fetch_category_raw", lambda _a, c, **_k: [{"content": "ä" * 500_000}])
    fp.build_edition_raw("news-fetcher", "2026-08-09", "evening", ["talous"])
    err = capsys.readouterr().err
    assert "1024 kB" in err and "news.2026-08-09.evening.raw" in err
    assert len(wired.calls) == 1  # still attempted — the warning is the diagnosis, not a veto


def test_size_is_measured_in_utf8_bytes(monkeypatch, wired, capsys):
    """Finnish raw is full of two-byte characters, so a CHARACTER count under-reports the thing the
    node actually caps. 450k 'ä' is 450k chars but 900 kB — over the floor."""
    monkeypatch.setattr(fp, "fetch_category_raw", lambda _a, c, **_k: [{"content": "ä" * 450_000}])
    fp.build_edition_raw("news-fetcher", "2026-08-09", "evening", ["talous"])
    blob = json.dumps({"c": "ä" * 450_000}, ensure_ascii=False)
    assert len(blob) < fp.RAW_WARN_BYTES < len(blob.encode("utf-8"))  # a char count would have missed it
    assert "over the" in capsys.readouterr().err


def test_a_lost_raw_write_fails_the_step_loud(monkeypatch):
    """The desks read this key and nothing else, so a lost write is the whole edition — it must go
    RED here rather than leave five downstream steps to fail one at a time."""
    monkeypatch.setattr(fp, "fetch_category_raw", lambda _a, c, **_k: [{"content": "x"}])
    monkeypatch.setattr(fp, "seed_status", lambda *a, **k: True)
    monkeypatch.setattr(fp, "step_status", _noop_status)
    monkeypatch.setattr(fp, "_aimeat_call", _Writes(ret=False))
    with pytest.raises(RuntimeError, match="raw write failed"):
        fp.build_edition_raw("news-fetcher", "2026-08-09", "evening", ["talous"])

"""The shared edition status record: one key, six writers, patched field by field.

The failure this guards against is SILENT — six agents patching without `owner_scope` produce six
records in six agent namespaces, every individual write returning ok. So the tests assert on the
request BODY, not just on the outcome. Deterministic: no network, no LLM.
"""

import pytest

from crewaimeat import edition_status as es


class _Rest:
    """Records every REST call the module makes and replays canned envelopes."""

    _UNSET = object()

    def __init__(self, ret=_UNSET):
        self.calls = []
        # `None` is a MEANINGFUL return (the helper's "the call failed"), so it needs a real sentinel
        # to be distinguishable from "caller passed nothing".
        self.ret = {"ok": True} if ret is _Rest._UNSET else ret

    def __call__(self, agent, method, path, body=None, **_kw):
        self.calls.append({"agent": agent, "method": method, "path": path, "body": body})
        return self.ret


def test_every_patch_is_owner_scoped(monkeypatch):
    """THE TRAP. Without owner_scope the six agents write six separate records and nothing merges —
    and nothing errors either, which is why this is asserted rather than assumed."""
    rest = _Rest()
    monkeypatch.setattr(es, "_aimeat_rest", rest)
    es.set_status("news-writer", "2026-08-09", "evening", "writeA", es.DONE)
    es.seed_status("news-fetcher", "2026-08-09", "evening")
    assert rest.calls, "no REST call was made at all"
    for c in rest.calls:
        assert c["body"]["owner_scope"] is True, f"{c['path']} would land in the agent's own namespace"
        assert c["method"] == "PATCH"


def test_a_step_patches_only_its_own_field(monkeypatch):
    rest = _Rest()
    monkeypatch.setattr(es, "_aimeat_rest", rest)
    es.set_status("news-writer-b", "2026-08-09", "evening", "writeB", es.DONE)
    patch = rest.calls[0]["body"]["patch"]
    assert patch["writeB"] == es.DONE
    assert set(patch) == {"writeB", "updatedAt"}  # nothing else — a sibling's field is never touched
    assert rest.calls[0]["path"] == "/v1/memory/news.2026-08-09.evening.status"


def test_seed_puts_every_step_at_queued(monkeypatch):
    rest = _Rest()
    monkeypatch.setattr(es, "_aimeat_rest", rest)
    es.seed_status("news-fetcher", "2026-08-09", "evening")
    patch = rest.calls[0]["body"]["patch"]
    assert {s: patch[s] for s in es.STEPS} == dict.fromkeys(es.STEPS, es.QUEUED)
    assert len(es.STEPS) == 6


def test_unknown_step_is_rejected_at_the_boundary(monkeypatch):
    monkeypatch.setattr(es, "_aimeat_rest", _Rest())
    with pytest.raises(ValueError):
        es.set_status("news-writer", "2026-08-09", "evening", "writeC", es.DONE)


# ── the context manager: running -> done | failed ────────────────────────────
def _states(rest) -> list:
    out = []
    for c in rest.calls:
        out += [v for k, v in c["body"]["patch"].items() if k != "updatedAt"]
    return out


def test_step_status_marks_running_then_done(monkeypatch):
    rest = _Rest()
    monkeypatch.setattr(es, "_aimeat_rest", rest)
    with es.step_status("news-fetcher", "2026-08-09", "evening", "fetch"):
        pass
    assert _states(rest) == [es.RUNNING, es.DONE]


def test_step_status_marks_failed_and_reraises(monkeypatch):
    rest = _Rest()
    monkeypatch.setattr(es, "_aimeat_rest", rest)
    with pytest.raises(RuntimeError), es.step_status("news-fetcher", "2026-08-09", "evening", "fetch"):
        raise RuntimeError("scrape blew up")
    assert _states(rest) == [es.RUNNING, es.FAILED]


def test_a_stage_that_fails_by_returning_can_say_so(monkeypatch):
    """The quiz builder refuses by RETURNING 'quiz=SKIPPED(…)'. The record must not call that done."""
    rest = _Rest()
    monkeypatch.setattr(es, "_aimeat_rest", rest)
    with es.step_status("daily-features-writer", "2026-08-09", "evening", "features") as st:
        st.fail()
    assert _states(rest) == [es.RUNNING, es.FAILED]


def test_a_failed_status_write_never_takes_the_edition_down(monkeypatch):
    """This record REPORTS on the pipeline; it is not part of the newspaper. A status write that
    fails logs loud and returns False — the edition carries on."""
    monkeypatch.setattr(es, "_aimeat_rest", _Rest(ret=None))
    assert es.set_status("news-writer", "2026-08-09", "evening", "writeA", es.DONE) is False
    with es.step_status("news-writer", "2026-08-09", "evening", "writeA"):
        pass  # no raise

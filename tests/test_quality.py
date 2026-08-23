"""`crewaimeat quality` — measuring whether the output got better when the model changed.

The single most important property of a measurement tool is that it does not invent findings. The
first run of this one reported 40 well-sourced articles as "100% ungrounded" because a rate-limited
provenance read returned the same shape as a missing one. That is worse than having no metric: it
sends someone to fix a problem that does not exist, and it teaches them to distrust the next number.

So most of what is pinned here is the difference between **zero**, **unknown**, and **absent**.

All node reads are stubbed; no network.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crewaimeat import quality


@pytest.fixture
def node(monkeypatch):
    """A tiny fake node. `records` maps memory key -> (provenance_id, text); `provenance` maps
    provenance id -> (model, sources) or the sentinel FAIL to make that read fail."""

    state: dict = {"records": {}, "provenance": {}, "list_fails": set()}

    def rest(_agent, _method, path, *a, **k):
        if path.startswith("/v1/memory?"):
            date = path.split("prefix=news.")[1].split(".")[0]
            if date in state["list_fails"]:
                return None
            keys = [k_ for k_ in state["records"] if f"news.{date}." in k_]
            return {"items": [{"key": k_} for k_ in keys]}
        if path.startswith("/v1/memory/"):
            key = path.split("/v1/memory/")[1].split("?")[0]
            rec = state["records"].get(key)
            return None if rec is None else {"value": rec[1], "ai_provenance_id": rec[0]}
        if path.startswith("/v1/provenance/"):
            pid = path.split("/v1/provenance/")[1]
            entry = state["provenance"].get(pid)
            if entry is None or entry == "FAIL":
                return None
            model, sources = entry
            return {"provenance": {"generator": {"model": model, "provider": "openrouter"}, "sources": [{}] * sources}}
        return None

    monkeypatch.setattr("crewaimeat.aimeat_crew._aimeat_rest", rest)
    monkeypatch.setattr(quality, "_MIN_INTERVAL", 0.0)  # no pacing in tests
    return state


def day(n: int = 0) -> str:
    """A date INSIDE the window, n days back from today.

    These were hardcoded (day(0)) until the clock rolled past midnight mid-session and every
    one of them fell outside a 3-day window at once. A test whose subject is a rolling window has to
    compute its dates the same way the code does."""
    return quality._dates(n + 1)[-1]


def _article(state, date: str, cat: str, pid: str | None, text: str = "x" * 3000):
    state["records"][f"news.{date}.evening.article.{cat}"] = (pid, text)


def test_the_window_is_in_the_editions_own_timezone_not_utc(monkeypatch):
    """The bug this shipped with: an evening edition dated D is written the night BEFORE D, around
    22:00 UTC. Between midnight Helsinki and midnight UTC a UTC clock dates the newest paper
    "tomorrow", so it fell outside every window — and those three hours are exactly when someone
    checks what last night's run produced. `today_local` is the repo's one answer for a calendar day
    that becomes a key."""
    monkeypatch.setattr("crewaimeat.aimeat_crew.today_local", lambda: "2026-08-24")
    assert quality._dates(3) == ["2026-08-24", "2026-08-23", "2026-08-22"]


def test_todays_edition_is_inside_the_window(node, monkeypatch):
    """The whole point of the fix: the freshest edition is the one you most want measured."""
    monkeypatch.setattr("crewaimeat.aimeat_crew.today_local", lambda: "2026-08-24")
    _article(node, "2026-08-24", "talous", "p1")
    node["provenance"]["p1"] = ("m/one", 5)
    articles, _problems = quality.collect("a", days=2)
    assert [a.date for a in articles] == ["2026-08-24"]


# ── the failure the first version shipped ───────────────────────────────────────────────────────
def test_a_provenance_read_that_FAILS_is_never_counted_as_ungrounded(node):
    """The bug: a rate-limited read returned the same shape as "no sources", so 40 well-sourced
    articles were reported as 100% ungrounded. Unknown is not zero."""
    _article(node, day(0), "talous", "p1")
    node["provenance"]["p1"] = "FAIL"
    articles, problems = quality.collect("a", days=3)
    stats = quality.group(articles)
    assert stats, "the article should still be counted"
    s = stats[0]
    assert s.ungrounded == 0, "an unreadable provenance must not count as ungrounded"
    assert s.unknown == 1
    assert any("read failed" in p for p in problems), "the failed read must be REPORTED, not swallowed"


def test_a_record_with_no_provenance_at_all_is_a_real_finding(node):
    """Distinct from the above: this record genuinely declares nothing, which IS worth knowing."""
    _article(node, day(0), "talous", None)
    articles, _problems = quality.collect("a", days=3)
    assert articles[0].model == "(no provenance record)"
    assert articles[0].sources is None


def test_zero_sources_is_counted_as_ungrounded(node):
    """The one number that is unambiguously bad — an article citing nothing is indistinguishable
    from invention. `talous` carries sources on other days, so the gap is judged."""
    _article(node, day(0), "talous", "p1")
    _article(node, day(1), "talous", "p2")
    node["provenance"]["p1"] = ("m/one", 0)
    node["provenance"]["p2"] = ("m/one", 5)
    stats = quality.group(quality.collect("a", days=5)[0])
    assert stats[0].ungrounded == 1 and stats[0].ungrounded_share == 0.5
    assert stats[0].unknown == 0


def test_a_category_that_is_never_sourced_is_generated_not_broken(node):
    """`koodaus`, `matikka` and `prompt-niksi` are generated feature sections with no news sources BY
    DESIGN — ~3 of every ~23 articles. Counting them made the first version report DeepSeek as five
    times worse at grounding than the free router, which was pure artifact."""
    for d in (day(0), day(1), day(2)):
        _article(node, d, "koodaus", f"k-{d}")
        node["provenance"][f"k-{d}"] = ("m/one", 0)
        _article(node, d, "talous", f"t-{d}")
        node["provenance"][f"t-{d}"] = ("m/one", 5)
    articles, _ = quality.collect("a", days=5)
    assert quality.generated_categories(articles) == {"koodaus"}
    s = quality.group(articles)[0]
    assert s.ungrounded == 0, "a generated section is not an ungrounded article"
    assert s.median_sources == 5.0, "grounding is measured over the sourced categories only"


def test_too_little_evidence_means_JUDGED_not_excused(node):
    """The two mistakes are not symmetric. Wrongly calling a category "generated" hides a real
    failure silently; wrongly judging one produces a visible finding someone can check. So below the
    evidence floor the category is judged."""
    _article(node, day(0), "uusi-osio", "p1")
    node["provenance"]["p1"] = ("m/one", 0)
    articles, _ = quality.collect("a", days=3)
    assert quality.generated_categories(articles) == set(), "one observation is not evidence"
    assert quality.group(articles)[0].ungrounded == 1


# ── the comparison the report exists for ────────────────────────────────────────────────────────
def test_articles_are_attributed_to_the_model_that_wrote_them(node):
    """Not "before and after a date" — a routing change mid-week, or a fallback quietly serving a
    different model, would make a date-based split lie."""
    _article(node, day(0), "talous", "p1")
    _article(node, day(0), "urheilu", "p2")
    _article(node, day(1), "talous", "p3")
    _article(node, day(2), "urheilu", "p4")  # so `urheilu` is a SOURCED category, not generated
    node["provenance"]["p1"] = ("deepseek/v4", 6)
    node["provenance"]["p2"] = ("free/router", 0)
    node["provenance"]["p3"] = ("deepseek/v4", 4)
    node["provenance"]["p4"] = ("deepseek/v4", 5)
    stats = {s.model: s for s in quality.group(quality.collect("a", days=6)[0])}
    assert len(stats["deepseek/v4"].articles) == 3
    assert stats["deepseek/v4"].editions == 3
    assert stats["deepseek/v4"].median_sources == 5.0
    assert stats["deepseek/v4"].ungrounded == 0
    assert stats["free/router"].ungrounded == 1


def test_completeness_is_per_edition_not_per_day(node):
    """A weak model silently skips categories, so "articles per edition" is the signal — and an
    edition with no articles must not drag the average of a model that never ran that day."""
    for cat in ("a", "b", "c"):
        _article(node, day(0), cat, f"p-{cat}")
        node["provenance"][f"p-{cat}"] = ("m/one", 3)
    _article(node, day(1), "a", "p-x")
    node["provenance"]["p-x"] = ("m/one", 3)
    s = quality.group(quality.collect("a", days=5)[0])[0]
    assert s.editions == 2 and len(s.articles) == 4 and s.per_edition == 2.0


# ── never present a thin read as a thin newspaper ───────────────────────────────────────────────
def test_a_failed_key_listing_is_reported_not_silently_skipped(node):
    """A day whose listing failed produces no articles — which looks exactly like a day with no
    paper. The difference has to reach the reader."""
    node["list_fails"].add(day(0))
    articles, problems = quality.collect("a", days=3)
    assert articles == []
    assert any("could not list" in p for p in problems)


def test_the_report_says_how_much_it_could_not_read(node):
    _article(node, day(0), "talous", "p1")
    node["provenance"]["p1"] = "FAIL"
    articles, problems = quality.collect("a", days=3)
    text = quality.render(quality.group(articles), articles, 3, problems)
    assert "FAILED" in text and "incomplete" in text


def test_the_report_never_claims_grounding_it_could_not_measure(node):
    _article(node, day(0), "talous", "p1")
    node["provenance"]["p1"] = "FAIL"
    articles, problems = quality.collect("a", days=3)
    text = quality.render(quality.group(articles), articles, 3, problems)
    assert "unknown" in text
    assert "100%" not in text, "an unmeasurable article must never render as a 100% failure"


def test_an_empty_window_says_so(node):
    articles, problems = quality.collect("a", days=2)
    assert quality.render(quality.group(articles), articles, 2, problems) == "no published articles in the window."


# ── the JSON surface, for a dashboard or an alert ───────────────────────────────────────────────
def test_json_output_separates_ungrounded_from_unknown(node, capsys, tmp_path):
    _article(node, day(0), "talous", "p1")
    _article(node, day(1), "talous", "p3")  # keeps `talous` a sourced category
    _article(node, day(0), "urheilu", "p2")
    node["provenance"]["p1"] = ("m/one", 0)
    node["provenance"]["p3"] = ("m/one", 4)
    node["provenance"]["p2"] = "FAIL"
    (tmp_path / "crews").mkdir(parents=True)
    (tmp_path / ".aimeat").mkdir()
    (tmp_path / ".aimeat" / "serve.json").write_text(json.dumps({"agents": [{"agent": "a"}]}), encoding="utf-8")
    quality.main(["--days", "3", "--json", "--root", str(tmp_path)])
    d = json.loads(capsys.readouterr().out)
    by = {m["model"]: m for m in d["models"]}
    assert by["m/one"]["ungrounded"] == 1
    assert by["(unread)"]["unknown_provenance"] == 1
    assert by["(unread)"]["ungrounded"] == 0


def test_no_registered_agent_is_an_error_not_an_empty_report(tmp_path):
    (tmp_path / "crews").mkdir(parents=True)
    (tmp_path / ".aimeat").mkdir()
    (tmp_path / ".aimeat" / "serve.json").write_text(json.dumps({"agents": []}), encoding="utf-8")
    assert quality.main(["--root", str(tmp_path)]) == 2


def test_the_real_repo_exposes_the_command():
    from crewaimeat.scaffold import _usage

    assert "crewaimeat quality" in _usage()
    assert Path("src/crewaimeat/quality.py").exists()

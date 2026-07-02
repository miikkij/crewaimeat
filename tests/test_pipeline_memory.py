"""pipeline_memory — deterministic-pipeline primitives degrade LOUD and never touch the network.

Fully offline: resolve_embedder and the crewai Memory class are stubbed (the test_embedder_cascade
idiom); storage paths land under a temp AIMEAT_HOME."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from crewaimeat import pipeline_memory as pm


class FakeMemory:
    """Captures constructor kwargs + remember/recall calls; behavior injectable per test."""

    instances: list[FakeMemory] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.remembered: list[tuple] = []
        self.recall_result: list = []
        self.raise_on: str | None = None
        FakeMemory.instances.append(self)

    def remember(self, text, **kw):
        if self.raise_on == "remember":
            raise OSError("disk gone")
        self.remembered.append((text, kw))

    def recall(self, query, **kw):
        if self.raise_on == "recall":
            raise OSError("index corrupt")
        return self.recall_result


def _hit(content: str, score: float, **meta):
    return SimpleNamespace(score=score, record=SimpleNamespace(content=content, metadata=meta))


@pytest.fixture(autouse=True)
def _fresh_cache():
    pm._OPEN.clear()
    yield
    pm._OPEN.clear()


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    monkeypatch.setattr(
        pm, "resolve_embedder", lambda a, bias=None: ({"provider": "openai", "config": {}}, "ollama-nomic")
    )
    monkeypatch.setattr(pm, "_memory_cls", lambda: FakeMemory)
    FakeMemory.instances.clear()
    s = pm.open_store("sanomat-test", analysis_llm=object())
    assert s is not None
    return s


# --- open_store availability contract ----------------------------------------
def test_open_store_none_and_loud_when_no_embedder(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))

    def _boom(a, bias=None):
        raise RuntimeError("crew memory is ON but NO embedder is reachable")

    monkeypatch.setattr(pm, "resolve_embedder", _boom)
    assert pm.open_store("x") is None  # pipeline default: ship without memory
    assert "UNAVAILABLE" in capsys.readouterr().err
    with pytest.raises(RuntimeError):  # a caller that cannot run without memory
        pm.open_store("x", required=True)


def test_open_store_builds_semantic_only_scoped_store(store, tmp_path):
    fake = FakeMemory.instances[-1]
    # semantic-only scoring -> MemoryMatch.score is comparable similarity (dedup thresholds hold)
    assert fake.kwargs["semantic_weight"] == 1.0
    assert fake.kwargs["recency_weight"] == 0.0 and fake.kwargs["importance_weight"] == 0.0
    # scope "agent" -> the ONE shared brain under AIMEAT_HOME (_shared), never the global default
    assert tmp_path.as_posix() in fake.kwargs["storage"].replace("\\", "/")
    assert "_shared" in fake.kwargs["storage"]
    assert fake.kwargs["root_scope"] == "/pipeline/sanomat-test"


# --- remember -----------------------------------------------------------------
def test_remember_passes_metadata_and_skips_empty(store):
    assert store.remember("editorial text", source="editorial", metadata={"date": "2026-07-01"})
    text, kw = FakeMemory.instances[-1].remembered[0]
    assert text == "editorial text" and kw["source"] == "editorial" and kw["metadata"]["date"] == "2026-07-01"
    assert not store.remember("   ")  # nothing stored for blank input
    assert len(FakeMemory.instances[-1].remembered) == 1


def test_remember_backend_error_is_loud_not_fatal(store, capsys):
    FakeMemory.instances[-1].raise_on = "remember"
    assert store.remember("x") is False
    assert "remember FAILED" in capsys.readouterr().err  # degrade loud, pipeline lives


# --- recall / dedup / prior-art ------------------------------------------------
def test_recall_maps_hits_and_survives_backend_error(store, capsys):
    fake = FakeMemory.instances[-1]
    fake.recall_result = [_hit("old editorial", 0.91, date="2026-06-01")]
    hits = store.recall("today topic")
    assert hits[0].content == "old editorial" and hits[0].score == 0.91 and hits[0].metadata["date"] == "2026-06-01"
    fake.raise_on = "recall"
    assert store.recall("q") == []
    assert "recall FAILED" in capsys.readouterr().err


def test_dedup_check_threshold(store):
    fake = FakeMemory.instances[-1]
    fake.recall_result = [_hit("same story, other words", 0.93)]
    dup = store.dedup_check("candidate item")
    assert dup.is_dup and dup.best_score == 0.93 and "same story" in dup.best_content
    fake.recall_result = [_hit("same broad topic, new angle", 0.62)]
    assert not store.dedup_check("candidate item").is_dup
    fake.recall_result = []
    assert not store.dedup_check("candidate item").is_dup  # empty store -> never a dup


def test_prior_art_block_formats_dates_and_min_score(store):
    fake = FakeMemory.instances[-1]
    fake.recall_result = [
        _hit("editorial about AI regulation", 0.8, date="2026-06-12"),
        _hit("weak match", 0.1),
        _hit("x" * 900, 0.7),
    ]
    block = store.prior_art_block("AI regulation today")
    assert "PRIOR ART" in block and "(2026-06-12)" in block and "AI regulation" in block
    assert "weak match" not in block  # under min_score
    assert "..." in block and len(block) < 2000  # long entries truncated
    fake.recall_result = []
    assert store.prior_art_block("anything") == ""  # empty -> safe to concatenate unconditionally

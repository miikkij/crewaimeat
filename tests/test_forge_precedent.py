"""crew-forge precedent memory — builds are remembered (order -> design) and recalled as priors.

Offline: the forge store cache is seeded with a fake (no embedder probe, no node); the live-rating
reader is stubbed. The no-request paths must NEVER touch memory (evals/tests stay offline by design).
"""

from __future__ import annotations

import pytest

from crewaimeat import forge
from crewaimeat.pipeline_memory import DedupResult, MemoryHit


class FakeStore:
    def __init__(self, hits=()):
        self.hits = list(hits)
        self.remembered: list[tuple[str, dict]] = []

    def recall(self, query, k=5, *, category=None):
        return [h for h in self.hits if category is None or h.metadata.get("category") == category][:k]

    def dedup_check(self, text, *, threshold=0.87, k=3, category=None):
        return DedupResult(False)

    def remember(self, text, *, source=None, metadata=None):
        self.remembered.append((text, metadata or {}))
        return True


@pytest.fixture
def fake_store(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(forge, "_FORGE_STORE", [store])
    return store


def test_precedent_block_lists_similar_builds_with_live_rating(fake_store, monkeypatch):
    fake_store.hits = [
        MemoryHit(
            "ORDER: watch competitors\nDESIGN: agent=competitor-watch; capabilities=web",
            0.81,
            {"category": "build", "agent_name": "competitor-watch"},
        ),
        MemoryHit("ORDER: below threshold", 0.2, {"category": "build", "agent_name": "meh"}),
        MemoryHit("not a build", 0.9, {"category": "other", "agent_name": "x"}),
    ]
    monkeypatch.setattr(forge, "_precedent_rating", lambda name: " [live research 0.67 (confident)]")
    block = forge.forge_precedent_block("an agent that watches my competitors")
    assert "PRECEDENT" in block and "competitor-watch" in block and "live research 0.67" in block
    assert "meh" not in block  # under the 0.45 similarity bar
    assert "not a build" not in block  # category-filtered: only build records are precedent


def test_precedent_block_empty_when_no_store_or_request(monkeypatch):
    monkeypatch.setattr(forge, "_FORGE_STORE", [None])  # memory unavailable -> feature just off
    assert forge.forge_precedent_block("anything") == ""
    monkeypatch.setattr(forge, "_FORGE_STORE", [FakeStore()])
    assert forge.forge_precedent_block("   ") == ""  # no order text -> nothing to match


_BD = (
    "def build_domain(ctx):\n"
    "    a = Agent(role='R', goal='g', backstory='b', llm=ctx.llm)\n"
    "    return [a], [Task(description=f'do: {ctx.prompt}', expected_output='o', agent=a)]\n"
)


def test_valid_build_with_request_is_remembered(fake_store, tmp_path, monkeypatch):
    monkeypatch.setattr(forge, "_project_root", lambda: tmp_path)
    out = forge.write_and_validate_crew.func(
        agent_name="comp-watch", build_domain_code=_BD, request="watch my competitors weekly"
    )
    assert "VALID" in out
    assert len(fake_store.remembered) == 1
    text, meta = fake_store.remembered[0]
    assert text.startswith("ORDER: watch my competitors weekly")
    assert "DESIGN: agent=comp-watch" in text and "RESULT: VALID" in text
    assert meta["agent_name"] == "comp-watch" and meta["category"] == "build"


def test_no_request_or_invalid_build_never_touches_memory(fake_store, tmp_path, monkeypatch):
    monkeypatch.setattr(forge, "_project_root", lambda: tmp_path)
    out = forge.write_and_validate_crew.func(agent_name="quiet-crew", build_domain_code=_BD)
    assert "VALID" in out and fake_store.remembered == []  # evals/tests pass no request -> offline
    out = forge.write_and_validate_crew.func(
        agent_name="broken-crew", build_domain_code="def build_domain(ctx:\n  oops", request="broken order"
    )
    assert "INVALID" in out and fake_store.remembered == []  # retry-loop spam never stored

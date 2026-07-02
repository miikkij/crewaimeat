"""local_memory — durable per-agent local tier. Isolated to a tmp AIMEAT_HOME; publish() is monkeypatched
so the storage + lifecycle tests never touch the connector."""

from __future__ import annotations


def test_remember_recall_and_scoping(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    from crewaimeat import local_memory as lm

    rid = lm.remember("watcher", {"finding": "x"}, topic="funding", source="techcrunch", tags=["raw"])
    rec = lm.recall("watcher", rid)
    assert rec is not None
    assert rec["body"] == {"finding": "x"}
    assert rec["topic"] == "funding" and rec["source"] == "techcrunch"
    assert rec["status"] == lm.RAW and rec["published_at"] is None
    assert rec["tags"] == ["raw"]

    # scoping: another agent doesn't see it
    assert lm.recall("other", rid) is None


def test_overwrite_preserves_creation_and_publish_state(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    from crewaimeat import local_memory as lm

    rid = lm.remember("watcher", {"v": 1}, topic="a")
    created_ts = lm.recall("watcher", rid)["ts"]
    lm.mark_published("watcher", rid, key="k.a", visibility="owner")

    # editing in place keeps the original ts AND the publish linkage
    lm.remember("watcher", {"v": 2}, id=rid, topic="b")
    rec = lm.recall("watcher", rid)
    assert rec["body"] == {"v": 2} and rec["topic"] == "b"
    assert rec["ts"] == created_ts
    assert rec["status"] == lm.PUBLISHED and rec["key"] == "k.a"


def test_browse_facets_and_filters(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    from crewaimeat import local_memory as lm

    lm.remember("w", "one", topic="funding", source="tc", tags=["final"])
    lm.remember("w", "two", topic="funding", source="hn")
    lm.remember("w", "three", topic="hiring", source="tc")

    assert len(lm.browse("w")) == 3
    assert len(lm.browse("w", topic="funding")) == 2
    assert len(lm.browse("w", source="tc")) == 2
    assert len(lm.browse("w", topic="funding", source="tc")) == 1
    assert [r["body"] for r in lm.browse("w", tag="final")] == ["one"]
    assert len(lm.browse("w", limit=1)) == 1

    f = lm.facets("w")
    assert f["topic"] == {"funding": 2, "hiring": 1}
    assert f["source"]["tc"] == 2
    assert f["status"]["raw"] == 3
    assert sum(f["day"].values()) == 3  # all written "today"


def test_publish_mirrors_upward_then_marks_published(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    from crewaimeat import local_memory as lm

    calls: list = []

    def fake_call(agent, tool, params):
        calls.append((agent, tool, params))
        return {"ok": True}

    # publish() lazy-imports _aimeat_call from aimeat_crew — patch it at the source.
    import crewaimeat.aimeat_crew as ac

    monkeypatch.setattr(ac, "_aimeat_call", fake_call)

    rid = lm.remember("w", {"article": "final text"}, topic="funding")
    res = lm.publish("w", rid, key="news.2026-06-28.funding", visibility="public")

    assert res["ok"] is True and res["key"] == "news.2026-06-28.funding"
    assert calls == [
        (
            "w",
            "aimeat_memory_write",
            {"key": "news.2026-06-28.funding", "value": {"article": "final text"}, "visibility": "public"},
        )
    ]
    rec = lm.recall("w", rid)
    assert rec["status"] == lm.PUBLISHED and rec["key"] == "news.2026-06-28.funding"
    assert rec["visibility"] == "public" and rec["published_at"] is not None


def test_publish_rejects_bad_input_and_missing_record(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    from crewaimeat import local_memory as lm

    assert lm.publish("w", "nope", key="k")["ok"] is False  # unknown record
    rid = lm.remember("w", "x")
    assert lm.publish("w", rid, key="", visibility="owner")["ok"] is False  # empty key
    assert lm.publish("w", rid, key="k", visibility="secret")["ok"] is False  # bad visibility
    assert lm.recall("w", rid)["status"] == lm.RAW  # nothing was published


def test_make_local_memory_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    from crewaimeat import local_memory as lm

    tools = lm.make_local_memory_tools("w")
    assert [t.name for t in tools] == ["remember", "browse_memory", "search_memory", "recall_memory", "publish_memory"]

    remember_tool = tools[0]
    out = remember_tool.run(body='{"finding": "x"}', topic="funding", tags="a, b")
    assert "remembered locally" in out
    # the tool wrote through to the real store, parsed JSON, split tags
    recs = lm.browse("w", topic="funding")
    assert len(recs) == 1
    assert recs[0]["body"] == {"finding": "x"} and recs[0]["tags"] == ["a", "b"]


def test_forget(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    from crewaimeat import local_memory as lm

    a = lm.remember("w", "a")
    lm.remember("w", "b")
    assert lm.forget("w", a) == 1
    assert lm.recall("w", a) is None
    assert len(lm.browse("w")) == 1
    assert lm.forget("w") == 1  # wipe the rest
    assert lm.browse("w") == []


def test_search_full_text_ranked_and_scoped(tmp_path, monkeypatch):
    import json

    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    from crewaimeat import local_memory as lm

    a = lm.remember("w", {"note": "Helsinki launch venue booked for March"}, topic="events")
    lm.remember("w", "Budget approved: 8400 euros for the launch party", topic="money", tags=["launch"])
    lm.remember("w", "Unrelated grocery list: milk, bread", topic="home")
    lm.remember("other", "Helsinki weather is nice")  # another agent — must not leak

    hits = lm.search("w", "Helsinki launch")
    assert hits and hits[0]["id"] == a  # both words hit -> best BM25 rank first
    assert all("grocery" not in json.dumps(h["body"]) for h in hits[:2])
    assert {h["id"] for h in hits} <= {r["id"] for r in lm.browse("w", limit=50)}  # agent-scoped

    # tags and topic are searchable too; status filters compose
    assert lm.search("w", "launch", status="raw")
    lm.mark_published("w", a, key="k")
    assert all(h["id"] != a for h in lm.search("w", "Helsinki", status="raw"))


def test_search_survives_fts_syntax_and_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    from crewaimeat import local_memory as lm

    lm.remember("w", "C++ tips AND tricks (NEAR the edge) - dashes*")
    assert lm.search("w", 'AND (NEAR) "quoted" -x *star') != []  # syntax chars disarmed, words still match
    assert lm.search("w", "") == []
    assert lm.search("w", "!!! ???") == []  # no word tokens -> no query


def test_fts_migration_rebuilds_existing_rows(tmp_path, monkeypatch):
    """A DB created BEFORE the FTS feature becomes searchable on first open after the upgrade."""
    monkeypatch.setenv("AIMEAT_HOME", str(tmp_path))
    from crewaimeat import local_memory as lm

    lm.remember("w", "the ancient record about quasars", topic="space")
    # simulate a pre-FTS database: drop the index + triggers, then reopen (which must rebuild)
    with lm._conn() as c:
        for trg in ("records_fts_ai", "records_fts_ad", "records_fts_au"):
            c.execute(f"DROP TRIGGER {trg}")
        c.execute("DROP TABLE records_fts")
    assert [h["topic"] for h in lm.search("w", "quasars")] == ["space"]  # _ensure_fts recreated + rebuilt

"""Oikaisukanava: the public index lifecycle (file -> rule -> publish), strict arbiter parsing, and
fail-loud on dead writes. All deterministic — _aimeat_call and the LLM are faked."""

import pytest

from crewaimeat import corrections as cx


class _Store:
    """Stateful fake node memory: dict of key -> value."""

    def __init__(self, initial=None):
        self.data = dict(initial or {})
        self.dead = False

    def __call__(self, agent, tool, payload):
        if self.dead:
            return None
        if tool == "aimeat_memory_read":
            key = payload["key"]
            return {"value": self.data.get(key)}
        if tool == "aimeat_memory_write":
            self.data[payload["key"]] = payload["value"]
            return {"ok": True}
        if tool == "aimeat_memory_list":
            prefix = payload.get("prefix", "")
            items = [{"key": k, "value": v} for k, v in self.data.items() if k.startswith(prefix)]
            return {"items": items}
        raise AssertionError(f"unexpected tool {tool}")


@pytest.fixture
def store(monkeypatch):
    s = _Store()
    monkeypatch.setattr(cx, "_aimeat_call", s)
    return s


# ── filing + status transitions ───────────────────────────────────────────────
def test_new_request_files_newest_first(store):
    e1 = cx.new_request("sanomat-desk", sender="user@node", text="OIKAISU: väite X on väärin")
    e2 = cx.new_request("sanomat-desk", sender="toinen@node", text="OIKAISU: juttu Y")
    items = cx.read_index("sanomat-desk")
    assert [i["id"] for i in items] == [e2["id"], e1["id"]]
    assert items[1]["status"] == "vastaanotettu"
    assert items[0]["sender"] == "toinen"  # handle only, no node routing in a public key
    assert store.data[cx.INDEX_KEY]["items"]  # the app reads exactly this key


def test_set_status_updates_entry(store):
    e = cx.new_request("sanomat-desk", sender="u@n", text="OIKAISU: z")
    cx.set_status("sanomat-desk", e["id"], status="aiheeton", perustelu="satiiri on satiiria")
    entry = cx.read_index("sanomat-desk")[0]
    assert entry["status"] == "aiheeton"
    assert entry["perustelu"] == "satiiri on satiiria"


def test_set_status_rejects_unknown_status(store):
    e = cx.new_request("sanomat-desk", sender="u@n", text="OIKAISU: z")
    with pytest.raises(ValueError, match="unknown correction status"):
        cx.set_status("sanomat-desk", e["id"], status="ehkä")


def test_set_status_missing_entry_raises(store):
    with pytest.raises(cx.CorrectionsUnavailable, match="not found"):
        cx.set_status("sanomat-desk", "oik-none-1", status="aiheeton")


def test_index_write_failure_is_loud(store):
    store.dead = True
    with pytest.raises(cx.CorrectionsUnavailable, match="index write failed"):
        cx.new_request("sanomat-desk", sender="u@n", text="OIKAISU: z")


# ── arbiter parsing (strict) ──────────────────────────────────────────────────
class _FakeLLM:
    def __init__(self, reply):
        self.reply = reply

    def call(self, messages):
        return self.reply


def _judge(monkeypatch, reply):
    monkeypatch.setattr(cx, "get_llm", lambda **_k: _FakeLLM(reply))
    entry = {"id": "oik-1", "sender": "u", "created": "2026-07-13", "claim": "väite"}
    return cx.judge_request("sanomat-desk", entry, [("news.2026-07-12.evening.article.talous", "Otsikko")])


def test_judge_aiheeton(monkeypatch):
    r = _judge(monkeypatch, '{"verdict": "aiheeton", "perustelu": "Satiiri.", "oikaisu": null, "article_key": null}')
    assert r["verdict"] == "aiheeton"
    assert r["oikaisu"] is None


def test_judge_oikaistaan_carries_text(monkeypatch):
    r = _judge(
        monkeypatch,
        '{"verdict": "oikaistaan", "perustelu": "Luku oli väärin.", '
        '"oikaisu": "Jutussa väitettiin X; oikea luku on Y.", '
        '"article_key": "news.2026-07-12.evening.article.talous"}',
    )
    assert r["verdict"] == "oikaistaan"
    assert "oikea luku" in r["oikaisu"]
    assert r["article_key"].endswith(".talous")


def test_judge_invalid_verdict_raises(monkeypatch):
    with pytest.raises(cx.CorrectionsUnavailable, match="verdict invalid"):
        _judge(monkeypatch, '{"verdict": "melkein", "perustelu": "?"}')


def test_judge_unparseable_raises(monkeypatch):
    with pytest.raises(cx.CorrectionsUnavailable, match="unparseable"):
        _judge(monkeypatch, "tuomio: aiheeton")


# ── publication ───────────────────────────────────────────────────────────────
def test_publish_correction_appends_and_flips_status(store):
    e = cx.new_request("sanomat-desk", sender="u@n", text="OIKAISU: talousjutun luku")
    date, edition = cx.publish_correction("sanomat-desk", e, "Jutussa väitettiin X; oikea luku on Y.")
    key = f"news.{date}.{edition}.article.oikaisut"
    body = store.data[key]
    assert body.startswith("# Oikaisut")  # first correction creates the article header
    assert "oikea luku on Y" in body
    assert "— Lakiosasto" in body
    entry = cx.read_index("sanomat-desk")[0]
    assert entry["status"] == "oikaistu"
    assert entry["edition"] == f"{date} {edition}"
    # a SECOND correction appends to the same edition article, no duplicate header
    e2 = cx.new_request("sanomat-desk", sender="v@n", text="OIKAISU: toinen")
    cx.publish_correction("sanomat-desk", e2, "Toinen oikaisu.")
    assert store.data[key].count("# Oikaisut") == 1
    assert store.data[key].count("## Oikaisu (") == 2


def test_publish_correction_dead_write_is_loud(store):
    e = cx.new_request("sanomat-desk", sender="u@n", text="OIKAISU: z")
    store.dead = True
    with pytest.raises(cx.CorrectionsUnavailable, match="correction publish failed"):
        cx.publish_correction("sanomat-desk", e, "teksti")

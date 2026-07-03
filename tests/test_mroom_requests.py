"""Tests for the M-ROOM REQuest fleet engine (crewaimeat.mroom_requests).

Covers the hard privacy invariant (no guest identity re-persisted), the records-driven lifecycle
(sniffing -> processing -> researched -> scored -> archived), stage idempotency + self-heal, and
builder robustness — all with an in-memory record store + canned LLM outputs (no network / no real LLM).
"""

from __future__ import annotations

import pytest

from crewaimeat import mroom_requests as mr


class _LLM:
    def __init__(self, out: str):
        self.out = out

    def call(self, _msgs):
        return self.out


@pytest.fixture
def room(monkeypatch):
    """An in-memory {space: {id: record}} store wired into the engine's read/write + a stub _gather."""
    store = {"request": {}, "outbox": {}, "archive-entry": {}}

    def _rd(agent="x"):
        return {"manifest": {}, "objects": {sp: list(v.values()) for sp, v in store.items()}}

    def _wr(space, rec_id, value, *, publish, namespace=None, agent=None):
        store[space][rec_id] = {**value, "id": rec_id}
        return True, rec_id

    monkeypatch.setattr(mr, "_room_read", _rd)
    monkeypatch.setattr(mr, "_room_write", _wr)
    monkeypatch.setattr(
        mr, "_gather", lambda q: [{"url": "https://ex.com/a", "text": "AIMEAT federated. MCP stateless."}]
    )
    return store


def _llms() -> dict:
    return {
        "plan": _LLM(
            '{"title":"T","classification":"comparison","poi_id":"POI_004","angle":"a","queries":["q1","q2"],"steps":["s"]}'
        ),
        "find": _LLM('{"fi":"F [1]\\n\\n## Lähteet\\n- u","en":"F [1]\\n\\n## Sources\\n- u"}'),
        "score": _LLM('{"signal_value":7.0,"verdict":"RETAINED","line":"gap"}'),
        "arch": _LLM('{"title":"A","markdown":"md","markdown_en":"md-en"}'),
    }


# --------------------------------------------------------------------------- privacy
def test_ask_strips_self_id_and_email():
    a = mr._ask({"ask": "Hi, I'm Jane Doe, mail me a@b.com about MCP"})
    assert "Jane Doe" not in a and "a@b.com" not in a and "MCP" in a


def test_vip_refuses_non_exc():
    assert mr._safe_vip({"vip": "jane@example.com"}) == "EXC_VIP"
    assert mr._safe_vip({"guest": "EXC_VIP_42"}) == "EXC_VIP_42"


def test_advance_allowlists_and_drops_identity(room):
    req = {
        "id": "r",
        "status": "sniffing",
        "vip": "EXC_VIP_1",
        "email": "a@b.com",
        "mobile": 358401234567,
        "guest_name": "Jane Doe",
        "theme": "secret-field",
        "ask": "compare X",
    }
    assert mr._advance(req, mr.SNIFFER, status=mr.ST_PROCESSING, outbox_ref="ob-r") is True
    rec = room["request"]["r"]
    for leak in ("a@b.com", "358401234567", "Jane Doe", "secret-field"):
        assert leak not in str(rec)
    assert rec["vip"] == "EXC_VIP_1" and rec["status"] == "processing" and rec["outbox_ref"] == "ob-r"
    assert not any(k in rec for k in ("mobile", "guest_name", "email", "theme"))


# --------------------------------------------------------------------------- builder robustness
@pytest.mark.parametrize("bad", ["[1,2,3]", '"just a string"', "42", "null", "not json at all"])
def test_builders_survive_non_dict_json(bad):
    assert mr._plan_ask(_LLM(bad), "ask", "EXC_VIP_1").get("queries") == []
    assert mr._compose_findings(_LLM(bad), "a", "v", "", [{"url": "u", "text": "t"}]) == {"fi": "", "en": ""}
    assert mr._score(_LLM(bad), "a", "v", "f") == {}


def test_score_rounds_before_deriving_verdict():
    s = mr._score(_LLM('{"signal_value":4.97,"line":"x"}'), "a", "v", "f")
    assert s["signal_value"] == 5.0 and s["verdict"] == "RETAINED"  # consistent, never '5.0 — DISCARDED'
    assert mr._score(_LLM('{"signal_value":4.9,"line":"x"}'), "a", "v", "f")["verdict"] == "DISCARDED"


def test_parse_scorecard():
    assert mr._parse_scorecard("## Scorecard\nSIGNAL VALUE: 6.5 — RETAINED\nx") == {
        "signal_value": 6.5,
        "verdict": "RETAINED",
    }
    assert mr._parse_scorecard("no scorecard here") == {}


# --------------------------------------------------------------------------- lifecycle
def test_full_chain_retained(room):
    lm = _llms()
    room["request"]["r"] = {"id": "r", "status": "sniffing", "vip": "EXC_VIP_9", "ask": "compare MCP vs AIMEAT"}
    mr.run_sniff(lm["plan"], dry_run=False)
    assert room["request"]["r"]["status"] == "processing" and "ob-r" in room["outbox"]
    mr.run_research(lm["find"], dry_run=False)
    assert room["request"]["r"]["status"] == "researched" and mr._HDR_FINDINGS in room["outbox"]["ob-r"]["markdown_en"]
    mr.run_score(lm["score"], dry_run=False)
    assert room["request"]["r"]["status"] == "scored" and room["request"]["r"]["verdict"] == "RETAINED"
    mr.run_archive(lm["arch"], dry_run=False)
    assert room["request"]["r"]["status"] == "archived" and "arc-r" in room["archive-entry"]
    # a terminal request matches no inbound status -> re-running every stage duplicates nothing
    mr.run_research(lm["find"], dry_run=False)
    mr.run_score(lm["score"], dry_run=False)
    en = room["outbox"]["ob-r"]["markdown_en"]
    assert en.count(mr._HDR_FINDINGS) == 1 and en.count(mr._HDR_SCORECARD) == 1


def test_discard_path_closes_with_light_note(room):
    room["request"]["r"] = {
        "id": "r",
        "status": "scored",
        "vip": "EXC_VIP_9",
        "ask": "x",
        "verdict": "DISCARDED",
        "signal_value": 2.0,
        "score_line": "no signal",
    }
    mr.run_archive(None, dry_run=False)  # no LLM needed on the discard path
    assert room["request"]["r"]["status"] == "archived"
    assert "DISCARDED" in room["archive-entry"]["arc-r"]["markdown_en"]


def test_sniff_self_heal_after_partial_write(room):
    # outbox landed on an earlier run but the status advance did not -> re-advance, don't strand
    room["request"]["r"] = {"id": "r", "status": "sniffing", "vip": "EXC_VIP_9", "ask": "x"}
    room["outbox"]["ob-r"] = {"id": "ob-r", "markdown": "## Plan", "markdown_en": "## Plan"}
    mr.run_sniff(_llms()["plan"], dry_run=False)
    assert room["request"]["r"]["status"] == "processing"


def test_digger_does_not_double_append(room):
    room["request"]["r"] = {
        "id": "r",
        "status": "processing",
        "vip": "EXC_VIP_9",
        "ask": "x",
        "outbox_ref": "ob-r",
        "plan": {"queries": ["q"]},
    }
    room["outbox"]["ob-r"] = {"id": "ob-r", "markdown": "## Plan", "markdown_en": "## Plan\n\n## Findings\nold"}
    mr.run_research(_llms()["find"], dry_run=False)
    assert room["request"]["r"]["status"] == "researched"
    assert room["outbox"]["ob-r"]["markdown_en"].count(mr._HDR_FINDINGS) == 1


def test_scorer_self_heal_uses_parsed_score(room):
    room["request"]["r"] = {"id": "r", "status": "researched", "vip": "EXC_VIP_9", "ask": "x", "outbox_ref": "ob-r"}
    room["outbox"]["ob-r"] = {
        "id": "ob-r",
        "markdown": "p",
        "markdown_en": "p\n\n## Scorecard\nSIGNAL VALUE: 8.0 — RETAINED\nl",
    }
    mr.run_score(_llms()["score"], dry_run=False)
    assert room["request"]["r"]["status"] == "scored"
    assert abs(room["request"]["r"]["signal_value"] - 8.0) < 0.01  # PARSED, not a fresh re-score
    assert room["outbox"]["ob-r"]["markdown_en"].count(mr._HDR_SCORECARD) == 1


def test_dry_run_writes_nothing(room):
    room["request"]["r"] = {"id": "r", "status": "sniffing", "vip": "EXC_VIP_9", "ask": "x"}
    mr.run_sniff(_llms()["plan"], dry_run=True)
    assert room["request"]["r"]["status"] == "sniffing" and not room["outbox"]


def test_failed_write_does_not_advance(room, monkeypatch):
    # a status advance that returns False must count as failed, not silently reported as advanced
    room["request"]["r"] = {
        "id": "r",
        "status": "scored",
        "vip": "EXC_VIP_9",
        "ask": "x",
        "verdict": "DISCARDED",
        "signal_value": 1.0,
    }
    monkeypatch.setattr(mr, "_room_write", lambda *a, **k: (False, a[1]))  # every write fails
    res = mr.run_archive(None, dry_run=False)
    assert res["processed"] == 0 and res["failed"] == 1 and room["request"]["r"]["status"] == "scored"

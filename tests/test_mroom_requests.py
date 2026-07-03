"""Tests for the M-ROOM REQuest fleet engine (crewaimeat.mroom_requests).

Covers the strict room.request@1 schema discipline (only declared fields written; `member`/`text`/`reason`
field names; no PII), the records-driven lifecycle (submitted -> sniffing -> processing -> researched ->
scored -> archived), stage idempotency + self-heal, and builder robustness — all with an in-memory record
store + canned LLM outputs (no network / no real LLM).
"""

from __future__ import annotations

import pytest

from crewaimeat import mroom_requests as mr

# every field the fleet is allowed to write (STRICT schema — anything else is rejected at publish)
_DECLARED = mr._SAFE_REQUEST_KEYS


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


def _req(**kw):
    base = {
        "id": "r",
        "status": "submitted",
        "member": "EXC_VIP_09",
        "text": "compare MCP vs AIMEAT",
        "submitted_at": "t0",
    }
    return {**base, **kw}


# --------------------------------------------------------------------------- privacy / schema discipline
def test_ask_reads_text_strips_self_id_and_email():
    a = mr._ask({"text": "Hi, I'm Jane Doe, mail me a@b.com about MCP"})
    assert "Jane Doe" not in a and "a@b.com" not in a and "MCP" in a


def test_member_preserves_handle_and_operator():
    assert mr._member({"member": "EXC_VIP_42"}) == "EXC_VIP_42"
    assert mr._member({"member": "OPERATOR"}) == "OPERATOR"  # operator test requests are not rejected
    assert mr._member({}) == "EXC_VIP"


def test_advance_writes_only_declared_fields(room):
    # a record carrying junk fields (a stray email/name/phone under any key) must publish ONLY declared fields
    req = _req(email="a@b.com", mobile=358401234567, guest_name="Jane Doe", ask="wrong-key", theme="x")
    assert mr._advance(req, mr.SNIFFER, status=mr.ST_PROCESSING, outbox_ref="ob-r", plan={"queries": ["q"]}) is True
    rec = room["request"]["r"]
    assert set(rec) <= _DECLARED, f"undeclared field written: {set(rec) - _DECLARED}"
    assert rec["member"] == "EXC_VIP_09" and rec["text"] == "compare MCP vs AIMEAT" and rec["status"] == "processing"
    for dropped in ("email", "mobile", "guest_name", "ask", "theme"):
        assert dropped not in rec
    assert "a@b.com" not in str(rec) and "Jane Doe" not in str(rec) and "358401234567" not in str(rec)


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


def test_archive_title_house_style():
    assert mr._archive_title("req-abc", "MCP vs AIMEAT").startswith("REQ req-abc // ")


# --------------------------------------------------------------------------- lifecycle
def test_full_chain_retained_writes_timestamps(room):
    lm = _llms()
    room["request"]["r"] = _req()
    mr.run_sniff(lm["plan"], dry_run=False)
    rec = room["request"]["r"]
    assert rec["status"] == "processing" and "ob-r" in room["outbox"] and rec.get("started_at")
    mr.run_research(lm["find"], dry_run=False)
    rec = room["request"]["r"]
    assert (
        rec["status"] == "researched"
        and rec.get("researched_at")
        and mr._HDR_FINDINGS in room["outbox"]["ob-r"]["markdown_en"]
    )
    mr.run_score(lm["score"], dry_run=False)
    rec = room["request"]["r"]
    assert (
        rec["status"] == "scored"
        and rec["verdict"] == "RETAINED"
        and rec.get("reason") == "gap"
        and rec.get("scored_at")
    )
    mr.run_archive(lm["arch"], dry_run=False)
    rec = room["request"]["r"]
    assert rec["status"] == "archived" and rec.get("archived_at") and "arc-r" in room["archive-entry"]
    assert room["archive-entry"]["arc-r"]["title"].startswith("REQ r // ")
    # every persisted request stayed within the declared schema
    assert set(rec) <= _DECLARED, f"undeclared field: {set(rec) - _DECLARED}"
    # terminal: re-running any stage duplicates nothing
    mr.run_research(lm["find"], dry_run=False)
    mr.run_score(lm["score"], dry_run=False)
    en = room["outbox"]["ob-r"]["markdown_en"]
    assert en.count(mr._HDR_FINDINGS) == 1 and en.count(mr._HDR_SCORECARD) == 1


def test_discard_path_closes_with_light_note(room):
    room["request"]["r"] = _req(status="scored", verdict="DISCARDED", signal_value=2.0, reason="no signal")
    mr.run_archive(None, dry_run=False)  # no LLM needed on the discard path
    assert room["request"]["r"]["status"] == "archived"
    assert "DISCARDED" in room["archive-entry"]["arc-r"]["markdown_en"]


def test_sniff_self_heal_after_partial_write(room):
    # outbox landed on an earlier run (status claimed `sniffing`) but the advance did not -> re-advance
    room["request"]["r"] = _req(status="sniffing")
    room["outbox"]["ob-r"] = {"id": "ob-r", "markdown": "## Plan", "markdown_en": "## Plan"}
    mr.run_sniff(_llms()["plan"], dry_run=False)
    assert room["request"]["r"]["status"] == "processing"


def test_digger_does_not_double_append(room):
    room["request"]["r"] = _req(status="processing", outbox_ref="ob-r", plan={"queries": ["q"]})
    room["outbox"]["ob-r"] = {"id": "ob-r", "markdown": "## Plan", "markdown_en": "## Plan\n\n## Findings\nold"}
    mr.run_research(_llms()["find"], dry_run=False)
    assert room["request"]["r"]["status"] == "researched"
    assert room["outbox"]["ob-r"]["markdown_en"].count(mr._HDR_FINDINGS) == 1


def test_scorer_self_heal_uses_parsed_score(room):
    room["request"]["r"] = _req(status="researched", outbox_ref="ob-r")
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
    room["request"]["r"] = _req()
    mr.run_sniff(_llms()["plan"], dry_run=True)
    assert room["request"]["r"]["status"] == "submitted" and not room["outbox"]


def test_failed_write_does_not_advance(room, monkeypatch):
    room["request"]["r"] = _req(status="scored", verdict="DISCARDED", signal_value=1.0, reason="x")
    monkeypatch.setattr(mr, "_room_write", lambda *a, **k: (False, a[1]))  # every write fails
    res = mr.run_archive(None, dry_run=False)
    assert res["processed"] == 0 and res["failed"] == 1 and room["request"]["r"]["status"] == "scored"

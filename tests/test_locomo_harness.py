"""Deterministic tests for the LOCOMO harness — no LLM, no network, no dataset download.

We build tiny in-memory Conversation objects and stub the answer/judge callables, so the whole
ingest -> retrieve -> answer -> judge -> aggregate pipeline is exercised offline. The dataset parser is
tested on a small inline raw sample (never hitting the network).
"""

from __future__ import annotations

from benchmarks.locomo import dataset, harness, metrics
from benchmarks.locomo.dataset import Conversation

# --- dataset parsing --------------------------------------------------------
_RAW = {
    "sample_id": "conv-x",
    "conversation": {
        "session_1_date_time": "1:00 pm on 3 May, 2023",
        "session_1": [
            {"speaker": "A", "text": "The Helsinki launch is on March 14, 2027.", "dia_id": "D1:1"},
            {"speaker": "B", "text": "Great, I'll book the venue.", "dia_id": "D1:2"},
        ],
        "session_2_date_time": "9:00 am on 10 May, 2023",
        "session_2": [{"speaker": "A", "text": "Budget is 8400 euros.", "dia_id": "D2:1"}],
    },
    "qa": [
        {"question": "When is the Helsinki launch?", "answer": "March 14, 2027", "category": 4, "evidence": ["D1:1"]},
        {"question": "What is the budget?", "answer": "8400 euros", "category": 1, "evidence": ["D2:1"]},
        {"question": "Trick question?", "adversarial_answer": "Not answerable", "category": 5, "evidence": []},
    ],
}


def test_flatten_orders_sessions_and_carries_dates():
    turns, ref_date = dataset._flatten_conversation(_RAW["conversation"])
    assert [t["dia_id"] for t in turns] == ["D1:1", "D1:2", "D2:1"]  # numeric session order preserved
    assert turns[0]["date"] == "1:00 pm on 3 May, 2023"
    assert ref_date == "9:00 am on 10 May, 2023"  # last session's date


def test_parse_qa_flags_adversarial_and_keeps_categories():
    qas = dataset._parse_qa(_RAW["qa"])
    assert len(qas) == 3
    assert qas[0].category == 4 and qas[0].answer == "March 14, 2027" and not qas[0].adversarial
    assert qas[2].adversarial and qas[2].answer == "Not answerable"  # cat 5 uses adversarial_answer


def test_scored_categories_exclude_adversarial():
    assert 5 not in metrics.SCORED_CATEGORIES
    assert set(metrics.SCORED_CATEGORIES) == {1, 2, 3, 4}


def test_turn_texts_render_speaker_date():
    turns, ref = dataset._flatten_conversation(_RAW["conversation"])
    conv = Conversation("c", turns, [], ref)
    texts = conv.turn_texts()
    assert texts[0].startswith("A (") and "Helsinki launch" in texts[0]


# --- metrics ----------------------------------------------------------------
def test_judge_label_parse_variants():
    assert metrics.parse_judge_label('{"reasoning": "ok", "label": "CORRECT"}') is True
    assert metrics.parse_judge_label('```json\n{"label": "WRONG"}\n```') is False
    assert metrics.parse_judge_label("The answer is CORRECT.") is True
    assert metrics.parse_judge_label("") is False  # ambiguous -> WRONG, never a false positive


def test_f1_and_bleu1():
    assert metrics.f1("march 14 2027", "march 14 2027") == 1.0
    assert metrics.f1("march 14", "december 25") == 0.0
    assert 0.0 < metrics.f1("the budget is 8400 euros", "8400 euros") <= 1.0
    assert metrics.bleu1("8400 euros", "the budget is 8400 euros") > 0.0
    assert metrics.bleu1("", "anything") == 0.0


# --- keyword arm ------------------------------------------------------------
def test_keyword_arm_retrieves_by_overlap():
    arm = harness.KeywordArm()
    arm.reset()
    arm.ingest(["The Helsinki launch is on March 14.", "Budget is 8400 euros.", "The weather is nice."])
    hits = arm.recall("When is the Helsinki launch?", k=2)
    assert hits and "Helsinki" in hits[0]  # the relevant turn ranks first
    assert arm.recall("zzz nonexistent qqq", k=3) == []  # no overlap -> nothing (honest floor)


# --- full pipeline (stubbed LLM) --------------------------------------------
def _conv() -> Conversation:
    turns, ref = dataset._flatten_conversation(_RAW["conversation"])
    qa = dataset._parse_qa(_RAW["qa"])
    return Conversation("conv-x", turns, qa, ref)


def _answer_fn(system: str, user: str) -> str:
    # echo the retrieved-memory prompt as the "answer" so the judge sees whatever was recalled
    return user


def _judge_fn(system: str, user: str) -> str:
    # CORRECT iff the generated answer surfaced the gold fact (deterministic, offline)
    return '{"label": "CORRECT"}' if "march 14" in user.lower() else '{"label": "WRONG"}'


def test_run_arm_scores_only_categories_1_to_4():
    rep = harness.run_arm(harness.KeywordArm(), [_conv()], _answer_fn, _judge_fn, k=3)
    assert rep.n_turns == 3
    assert len(rep.results) == 2  # the adversarial (cat 5) QA is excluded
    assert {r.category for r in rep.results} == {1, 4}
    # helsinki QA -> keyword recalls the D1:1 turn -> answer contains "march 14" -> CORRECT; budget QA -> WRONG
    j = rep.j_overall()
    assert 0.0 <= j <= 100.0 and any(r.correct for r in rep.results)


def test_run_arm_respects_max_qa_cap():
    rep = harness.run_arm(harness.KeywordArm(), [_conv()], _answer_fn, _judge_fn, k=3, max_qa_per_conv=1)
    assert len(rep.results) == 1  # capped, logged (no silent truncation)


def test_arm_report_summary_shape():
    rep = harness.run_arm(harness.KeywordArm(), [_conv()], _answer_fn, _judge_fn, k=3)
    s = rep.summary()
    for key in ("arm", "j_overall", "j_by_category", "mean_f1", "search_p50_s", "approx_tokens_per_qa", "n_qa"):
        assert key in s
    assert s["arm"] == "keyword" and s["n_qa"] == 2


def test_sample_conversations_deterministic_first_n():
    convs = [Conversation(f"c{i}", [], []) for i in range(10)]
    picked = dataset.sample_conversations(convs, 3)
    assert [c.sample_id for c in picked] == ["c0", "c1", "c2"]
    assert dataset.sample_conversations(convs, None) is convs  # None -> all


def test_render_report_runs():
    rep = harness.run_arm(harness.KeywordArm(), [_conv()], _answer_fn, _judge_fn, k=3)
    txt = harness.render_report([rep], note="unit")
    assert "LOCOMO results" in txt and "keyword" in txt and "J by category" in txt

"""L2 — unit tests for the reusable, LLM-free task guardrails in crews/_guardrails.py.

A function guardrail is a runtime test contract; here we test the contract directly with a tiny
TaskOutput stand-in (anything with a ``.raw``), no CrewAI run needed.
"""

from __future__ import annotations

from crews._guardrails import (
    feasibility_score_1_to_10,
    has_source_urls,
    json_with_fields,
    strip_fences,
)


class _Out:
    """Minimal TaskOutput stand-in: the guardrails only read ``.raw``."""

    def __init__(self, raw):
        self.raw = raw


def test_strip_fences_removes_json_fence():
    assert strip_fences('```json\n{"a":1}\n```') == '{"a":1}'
    assert strip_fences('{"a":1}') == '{"a":1}'


def test_fence_wrapped_json_passes():  # the exact FAIL-5 crash case
    ok, _ = json_with_fields("artifacts")(_Out('```json\n{"artifacts":[1]}\n```'))
    assert ok


def test_plain_json_with_fields_passes():
    ok, val = json_with_fields("artifacts", "ok")(_Out('{"artifacts":[1],"ok":true}'))
    assert ok


def test_missing_field_fails_with_reason():
    ok, msg = json_with_fields("artifacts")(_Out('{"other":1}'))
    assert not ok and "artifacts" in msg


def test_empty_field_counts_as_missing():
    ok, msg = json_with_fields("artifacts")(_Out('{"artifacts":[]}'))
    assert not ok and "artifacts" in msg


def test_invalid_json_fails():
    ok, msg = json_with_fields("x")(_Out("not json at all"))
    assert not ok and "JSON" in msg


def test_feasibility_score_in_range():
    assert feasibility_score_1_to_10(_Out("Feasibility Score: 7"))[0]
    assert feasibility_score_1_to_10(_Out("I rate it 8/10 overall"))[0]


def test_feasibility_score_out_of_range_or_absent_fails():
    assert not feasibility_score_1_to_10(_Out("Feasibility Score: 11"))[0]
    assert not feasibility_score_1_to_10(_Out("no number here"))[0]


def test_has_source_urls():
    assert has_source_urls(2)(_Out("see https://a.example and https://b.example"))[0]
    ok, msg = has_source_urls(1)(_Out("no links in this text"))
    assert not ok and "source URL" in msg

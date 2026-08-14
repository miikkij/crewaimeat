"""A subscriber's order: validated at the boundary, identified by WHO SENT IT, never repaired.

No network: every AIMEAT call is stubbed. What is worth testing here is the boundary, because two
of these rules are the difference between a subscription and a way into someone else's key space.
"""

import pytest

from crewaimeat import subscriber_briefing as sb
from crewaimeat import subscriber_intake as si
from crewaimeat import subscriptions as subs
from crewaimeat.subscriptions import MAX_QUERIES, InvalidOrder


# ── the id comes from the sender, and that is a permission boundary ──
def test_the_id_is_derived_from_the_sender_not_from_the_order():
    """The id is half of the share pattern `aamukatsaus.<id>.**`. If the ORDER could name it, one
    reader could have us write into another reader's space — and share it to them."""
    assert si.subscriber_id("matti@aimeat-finland-001-genesis") == "matti"
    assert si.subscriber_id("postman#acme-oy@node") == "acme-oy"  # a GAII resolves to its owner


@pytest.mark.parametrize("sender", ["", "ab@node", "ISO@node", "has space@node", "a" * 60 + "@node"])
def test_a_sender_that_cannot_be_an_id_is_refused(sender):
    with pytest.raises(InvalidOrder):
        si.subscriber_id(sender)


# ── the marker: an ordinary DM must never be parsed as an order ──
def test_an_unmarked_message_is_not_an_order():
    assert si.parse_order("Moi! Voisiko tästä jutella?") is None
    assert si.parse_order("") is None


def test_a_marked_message_carries_its_json():
    order = si.parse_order(si.MARKER + '\n{"topic": "purjehdus"}')
    assert order == {"topic": "purjehdus"}


def test_a_marker_with_broken_json_says_so_rather_than_guessing():
    with pytest.raises(InvalidOrder, match="not valid JSON"):
        si.parse_order(si.MARKER + "\n{competitors: oops}")


# ── validation ──
def test_a_comma_separated_string_is_accepted_as_a_list():
    assert subs.normalise({"radar": "tekoäly, sote , tekoäly"})["radar"] == ["tekoäly", "sote"]


def test_an_empty_order_is_refused_because_it_would_produce_an_empty_briefing():
    with pytest.raises(InvalidOrder, match="empty"):
        subs.normalise({"competitors": [], "radar": "", "topic": "  "})


def test_the_query_ceiling_is_the_cost_ceiling():
    """Each phrase is a live search every morning, so this is what makes a subscription's cost
    predictable rather than whatever the subscriber pasted in."""
    with pytest.raises(InvalidOrder, match=f"max {MAX_QUERIES}"):
        subs.normalise({"competitors": [f"kilpailija {i}" for i in range(MAX_QUERIES + 1)]})


def test_an_overlong_phrase_is_refused_not_truncated():
    with pytest.raises(InvalidOrder, match="max"):
        subs.normalise({"radar": ["x" * 200]})


# ── building one briefing ──
@pytest.fixture
def wired(monkeypatch):
    """Sections stubbed; the publish captured. Nothing here touches a network or a model."""
    written = {}
    monkeypatch.setattr(
        sb, "publish", lambda sub, value, day, **_k: written.update(value) or f"aamukatsaus.{sub}.{day}"
    )
    monkeypatch.setattr(sb, "_topic_section", lambda ph: f"## Aiheseuranta\n\n- {', '.join(ph)}\n")
    monkeypatch.setattr(
        "crewaimeat.mail_contract._competitor_section", lambda q=None, domain=None: f"## Kilpailijakatsaus\n\n- {q}\n"
    )
    return written


def test_only_the_sections_the_subscriber_ordered_are_produced(monkeypatch, wired):
    monkeypatch.setattr(
        sb, "get_prefs", lambda s, **_k: {"subscriber": s, "competitors": [], "radar": ["sote"], "topic": ""}
    )
    out = sb.build("acme-oy", date="2026-08-14")
    assert out["sections"] == ["aiheseuranta"]
    assert "Kilpailijakatsaus" not in wired["body_md"] and "Aiheseuranta" in wired["body_md"]


def test_the_briefing_records_the_order_it_was_built_from(monkeypatch, wired):
    """So a subscriber reading an old briefing can see what they were watching THEN — an order
    edited in September must not silently rewrite what August's briefing claims to be."""
    order = {"subscriber": "acme-oy", "competitors": ["kilpailija oy"], "radar": [], "topic": "purjehdus"}
    monkeypatch.setattr(sb, "get_prefs", lambda s, **_k: order)
    sb.build("acme-oy", date="2026-08-14")
    assert wired["order"] == {"competitors": ["kilpailija oy"], "radar": [], "topic": "purjehdus"}
    assert wired["generatedBy"].startswith("postman")


def test_a_paused_subscription_produces_nothing(monkeypatch, wired):
    monkeypatch.setattr(sb, "get_prefs", lambda s, **_k: {"subscriber": s, "topic": "x", "active": False})
    assert sb.build("acme-oy")["skipped"] == "paused"
    assert not wired


def test_an_unknown_subscriber_fails_loud(monkeypatch):
    monkeypatch.setattr(sb, "get_prefs", lambda s, **_k: None)
    with pytest.raises(LookupError):
        sb.build("nobody")


def test_one_subscriber_failing_never_costs_the_others(monkeypatch, wired):
    monkeypatch.setattr(sb, "list_subscribers", lambda **_k: [{"subscriber": "a"}, {"subscriber": "b"}])

    def _build(sub, date=None, agent=None):
        if sub == "a":
            raise RuntimeError("boom")
        return {"subscriber": sub, "key": "k"}

    monkeypatch.setattr(sb, "build", _build)
    res = sb.build_all(date="2026-08-14")
    assert "a" in res["failed"] and [b["subscriber"] for b in res["built"]] == ["b"]

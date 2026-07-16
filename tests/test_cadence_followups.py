"""CADENCE follow-up watch-logic floor — deterministic, no network, no LLM.

`scan()` is a byte-faithful Python mirror of the Tier-1 cortex engine
(CADENCE.followups.scan, aimeat-cadence.js). This pins all SEVEN proposal kinds against a fixed
`nowIso` so the two tiers can't drift — the same role the node's 66-assertion Tier-1 test plays.
Also checks the crm-task record built from a proposal stays inside the locked schema (no deal_ref,
no omistaja) and that the deterministic dedup id is stable.
"""

from __future__ import annotations

import datetime

from crewaimeat.cadence_contract import DEFAULTS, build_task_from_action, dedup_id, scan

# Midnight UTC so day-granular record dates (stored midnight) yield round daysBetween values — the
# engine floors (close - now)/day, so a noon `now` would shave a half-day off every metric.
NOW = "2026-07-16T00:00:00Z"


def _data():
    """One clean subject per kind so every one of the seven surfaces exactly once."""
    contacts = [
        # cold_contact (high): active stage, last touch 45d ago (>= 2*14), no open task
        {
            "id": "c-cold",
            "etunimi": "Cold",
            "sukunimi": "Lead",
            "email": "cold@x.fi",
            "tila": "kontaktoitu",
            "viimeisin_kosketus": "2026-06-01",
            "luotu": "2026-05-01",
        },
        # new_contact_no_task (high): stage 'uusi', 15d old (>= 2), no open task
        {"id": "c-new", "etunimi": "New", "sukunimi": "Lead", "tila": "uusi", "luotu": "2026-07-01"},
        # negative_call_followup (high): active stage, a done negative call, no open task
        {"id": "c-neg", "etunimi": "Neg", "sukunimi": "Call", "tila": "kontaktoitu", "luotu": "2026-07-10"},
        # taskholder: active stage BUT already has an open task -> suppressed from any create proposal;
        # it owns the overdue task subject below.
        {"id": "c-hold", "etunimi": "Task", "sukunimi": "Holder", "tila": "kontaktoitu", "luotu": "2026-05-01"},
        # lost contact: never proposed
        {"id": "c-lost", "etunimi": "Lost", "tila": "menetetty", "luotu": "2026-01-01"},
    ]
    activities = [
        {
            "id": "a-1",
            "tyyppi": "call",
            "tila": "done",
            "tulos": "negative",
            "kontakti_ref": "c-neg",
            "ajankohta": "2026-07-14T10:00:00Z",
        },
    ]
    tasks = [
        # overdue_task (high): open, due 2026-07-01 -> 15d overdue (> 3); tied to c-hold (suppresses it)
        {
            "id": "t-over",
            "otsikko": "Soita asiakkaalle",
            "tyyppi": "call",
            "tila": "open",
            "eranpaiva": "2026-07-01",
            "kontakti_ref": "c-hold",
        },
    ]
    deals = [
        # closing_soon (high): open, expected close in 4d (<= 7)
        {
            "id": "d-close",
            "otsikko": "Iso diili",
            "tila": "open",
            "odotettu_klousaus": "2026-07-20",
            "luotu": "2026-07-01",
        },
        # stale_deal (medium): open, no activity/contact, created 45d ago (>= 10)
        {"id": "d-stale", "otsikko": "Vanha diili", "tila": "open", "luotu": "2026-06-01"},
        # open_deal_no_task (low): open, fresh (1d, < 10), no task
        {"id": "d-fresh", "otsikko": "Tuore diili", "tila": "open", "luotu": "2026-07-15"},
        # won deal: never proposed
        {"id": "d-won", "otsikko": "Voitettu", "tila": "won", "luotu": "2026-01-01"},
    ]
    return {"contact": contacts, "activity": activities, "crm-task": tasks, "deal": deals}


def test_scan_surfaces_all_seven_kinds():
    props = {p["id"]: p for p in scan(_data(), None, NOW)}

    # exactly the seven expected subjects, nothing more (lost contact / won deal / taskholder suppressed)
    assert set(props) == {
        "cold_contact:c-cold",
        "new_contact_no_task:c-new",
        "negative_call_followup:c-neg",
        "closing_soon:d-close",
        "stale_deal:d-stale",
        "open_deal_no_task:d-fresh",
        "overdue_task:t-over",
    }

    def sev(pid):
        return props[pid]["severity"]

    def create(pid):
        return props[pid]["action"]["create"]

    # severities
    assert sev("cold_contact:c-cold") == "high"  # 45 >= 2*14
    assert sev("new_contact_no_task:c-new") == "high"  # 15 >= 2
    assert sev("negative_call_followup:c-neg") == "high"
    assert sev("closing_soon:d-close") == "high"
    assert sev("stale_deal:d-stale") == "medium"
    assert sev("open_deal_no_task:d-fresh") == "low"
    assert sev("overdue_task:t-over") == "high"  # 15 > 3

    # metrics (days) mirror the engine's daysBetween
    assert props["cold_contact:c-cold"]["metric"] == 45
    assert props["new_contact_no_task:c-new"]["metric"] == 15
    assert props["closing_soon:d-close"]["metric"] == 4
    assert props["stale_deal:d-stale"]["metric"] == 45
    assert props["open_deal_no_task:d-fresh"]["metric"] == 1
    assert props["overdue_task:t-over"]["metric"] == 15

    # create actions: due / priority per kind
    assert create("cold_contact:c-cold") == {
        "tyyppi": "call",
        "prioriteetti": "normaali",
        "dueInDays": 1,
        "kontakti_ref": "c-cold",
    }
    assert create("new_contact_no_task:c-new")["dueInDays"] == 2
    assert create("negative_call_followup:c-neg")["prioriteetti"] == "korkea"
    assert create("negative_call_followup:c-neg")["dueInDays"] == DEFAULTS["negativeCallFollowupDays"]

    # closing_soon due is capped to [0,1]; deal proposals carry deal_ref (dropped only at task-build time)
    close = create("closing_soon:d-close")
    assert close["dueInDays"] == 1 and close["prioriteetti"] == "korkea" and close["deal_ref"] == "d-close"
    assert close["kontakti_ref"] is None  # deal has no contact

    # overdue is surfaced as an openTask reference, never a duplicate create
    assert props["overdue_task:t-over"]["action"] == {"openTask": "t-over"}

    # sorted: all highs precede the medium, which precedes the low
    order = [p["severity"] for p in scan(_data(), None, NOW)]
    assert order == sorted(order, key=lambda s: {"high": 0, "medium": 1, "low": 2}[s])


def test_contact_with_open_task_is_suppressed():
    """A contact that already has an open task gets no create proposal (only the overdue subject shows)."""
    props = {p["id"]: p for p in scan(_data(), None, NOW)}
    assert not any(p.startswith(("cold_contact:c-hold", "new_contact_no_task:c-hold")) for p in props)


def test_closing_soon_past_close_due_is_zero():
    """A deal whose expected close is in the PAST still fires closing_soon, capped to due 0."""
    data = {"deal": [{"id": "d-late", "otsikko": "Myöhässä", "tila": "open", "odotettu_klousaus": "2026-07-10"}]}
    p = scan(data, None, NOW)[0]
    assert p["kind"] == "closing_soon" and p["metric"] == -6 and p["action"]["create"]["dueInDays"] == 0


def test_config_override_raises_cold_threshold():
    """Owner config overrides thresholds (cadence.config.followups) — a 20-day cold threshold spares a
    16-day-silent active contact."""
    data = {"contact": [{"id": "c-x", "tila": "asiakas", "viimeisin_kosketus": "2026-06-30", "luotu": "2026-01-01"}]}
    assert scan(data, None, NOW)  # 16 days >= default 14 -> proposed
    assert not scan(data, {"followups": {"coldContactDays": 20}}, NOW)  # raised threshold -> spared


def test_build_task_stays_inside_locked_schema():
    """The crm-task built from a deal proposal must NOT carry deal_ref or omistaja (locked schema
    rejects extras), must set tila=open + luonut, and derive eranpaiva from today+dueInDays."""
    p = next(x for x in scan(_data(), None, NOW) if x["id"] == "closing_soon:d-close")
    today = datetime.date(2026, 7, 16)
    rec = build_task_from_action(p, luonut="cadence-followup#owner@node", today=today)

    allowed = {
        "id",
        "otsikko",
        "tyyppi",
        "kontakti_ref",
        "prioriteetti",
        "eranpaiva",
        "tila",
        "kuvaus",
        "luotu",
        "luonut",
    }
    assert set(rec) <= allowed, f"unexpected keys: {set(rec) - allowed}"
    assert "deal_ref" not in rec and "omistaja" not in rec
    assert "kontakti_ref" not in rec  # the deal had no contact -> key omitted, not sent as null
    assert rec["tila"] == "open" and rec["luonut"] == "cadence-followup#owner@node"
    assert rec["eranpaiva"] == "2026-07-17"  # today + dueInDays(1)
    assert rec["id"] == dedup_id("closing_soon:d-close") == "cf-closing_soon-d-close"


def test_dedup_id_is_stable_and_charset_safe():
    assert dedup_id("cold_contact:c-abc123") == "cf-cold_contact-c-abc123"
    assert dedup_id("new_contact_no_task:c-x") == dedup_id("new_contact_no_task:c-x")  # deterministic

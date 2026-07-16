"""cadence-followup: a DETERMINISTIC workspace-contract that watches a CADENCE CRM workspace and
drafts (or, under an autonomy band, auto-creates) follow-up `crm-task` records.

This is the Tier-2 runtime of the CADENCE follow-up feature. Tier-1 already ships in the browser as
the cortex lib `aimeat-cadence-cortex` (`CADENCE.followups.scan`); the pure watch-logic below is a
BYTE-FAITHFUL Python mirror of that engine so both tiers agree on what needs attention. Canonical
source: GET https://aimeat.io/v1/cortex/aimeat-cadence-cortex/libs/aimeat-cadence.js → CADENCE.followups.

Contract:
  inputs (read):  contact / company / deal / activity  (records) + crm-task (read for dedup/overdue)
  output (write): crm-task (records) — a new follow-up task per surfaced proposal
  lifecycle: record-push wake -> read the five CRM spaces -> scan() -> per proposal draft/create a
             crm-task, keyed deterministically so re-runs never duplicate.

Autonomy bands (config/env CADENCE_FOLLOWUP_BAND, default "propose"):
  propose = write the task as a DRAFT only (owner reviews + publishes)
  auto    = write AND publish the task (the workspace publish gate may still force human review)

The loop is plain code — NO LLM (the whole point is the two tiers share ONE watch-logic). It never
contacts anyone, sends anything, or moves a deal; it only creates CRM task records for the owner.
All reads/writes go through crewfive's deterministic `_aimeat_call` (POST /local/call/<tool>) — never
the LLM liaison — so the daemon tool-filter that omits aimeat_workspace_* is irrelevant here.
"""

from __future__ import annotations

import datetime
import os
import re
import sys

from crewai.tools import tool

from crewaimeat.aimeat_crew import _aimeat_call, member_workspaces

AGENT = "cadence-followup"

# The five CADENCE object types, keyed by SPACE name (what aimeat_workspace_read returns under
# `objects`). crm-task is BOTH a read input (overdue/dedup) and the write OUTPUT.
CONTACT, COMPANY, DEAL, ACTIVITY, TASK = "contact", "company", "deal", "activity", "crm-task"
_CRM_SPACES = (CONTACT, COMPANY, DEAL, ACTIVITY, TASK)

# Machine-readable contract declaration (§2). The five spaces already exist in a CADENCE workspace
# (server-locked schemas), so adopt-contract is a verify/no-op — we declare them WITHOUT inline schemas
# so adoption never fights the existing lock. `id` MUST equal the `contract.<id>` discovery tag.
CONTRACT = {
    "id": "cadence-followup",
    "spaces": [
        {"space": CONTACT, "namespace": "crm.contacts", "mode": "records"},
        {"space": COMPANY, "namespace": "crm.companies", "mode": "records"},
        {"space": DEAL, "namespace": "crm.deals", "mode": "records"},
        {"space": ACTIVITY, "namespace": "crm.activities", "mode": "records"},
        {"space": TASK, "namespace": "crm.tasks", "mode": "records"},
    ],
}

# Follow-up thresholds — CADENCE.followups.DEFAULTS, verbatim.
DEFAULTS = dict(
    coldContactDays=14,
    dealStaleDays=10,
    closingSoonDays=7,
    negativeCallFollowupDays=3,
    overdueHighDays=3,
    newContactStages=["uusi"],
    activeContactStages=["kontaktoitu", "qualifioitu", "asiakas"],
    maxPromptItems=25,
)
_SEV = {"high": 3, "medium": 2, "low": 1}

# Runaway guard: dedup keys handled THIS run (survives a stale read within a run). The deterministic
# task id (built from the key) is the primary, restart-surviving guard.
_PROCESSED: set[str] = set()
_GAII_CACHE: dict[str, str] = {}


# ── pure watch-logic (mirror of CADENCE.followups.scan) ─────────────────────────────────────────
def _parse_ms(v) -> int | None:
    """ISO string -> epoch milliseconds (int), mirroring JS Date.parse. Date-only and naive datetimes
    are treated as UTC (CADENCE records store `new Date().toISOString()`, i.e. Z-suffixed). None if
    unparseable."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip()
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.datetime.fromisoformat(s)
    except ValueError:
        try:  # bare date "YYYY-MM-DD"
            dt = datetime.datetime.fromisoformat(s + "T00:00:00")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp() * 1000)


def _days_between(a_ms: int, b_ms: int) -> int:
    """floor((a-b)/86400000) — Python // floors toward -inf, matching JS Math.floor."""
    return (a_ms - b_ms) // 86400000


def _contact_name(c: dict) -> str:
    if not c:
        return ""
    n = " ".join(x for x in (c.get("etunimi"), c.get("sukunimi")) if x).strip()
    return n or c.get("email") or c.get("id") or ""


def scan(data: dict | None, config: dict | None = None, now_iso: str | None = None) -> list[dict]:
    """Prioritized follow-up proposals over already-loaded CRM data. Pure (no I/O). Mirrors the Tier-1
    engine exactly so the app tab and this agent never drift.

    `data` = {contact, company, deal, activity, crm-task: [records]}; `config` = {followups:{...}}
    (falls back to DEFAULTS); `now_iso` = injected "now" for determinism (real UTC now otherwise).
    Returns proposals sorted by severity then metric, each:
      {id:"<kind>:<subjectId>", kind, severity, subjectType, subjectId, subjectLabel, metric, context,
       action:{create:{tyyppi, prioriteetti, dueInDays, kontakti_ref[, deal_ref]}} | {openTask:<id>}}
    """
    data = data or {}
    cfg = {**DEFAULTS, **((config or {}).get("followups") or {})}
    now_ms = _parse_ms(now_iso)
    if now_ms is None:
        now_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
    contacts = data.get(CONTACT) or []
    deals = data.get(DEAL) or []
    activities = data.get(ACTIVITY) or []
    tasks = data.get(TASK) or data.get("task") or []
    contact_by_id = {c.get("id"): c for c in contacts if isinstance(c, dict)}

    # index open tasks by contact + collect overdue OPEN tasks
    open_task_by_contact: dict = {}
    overdue: list[dict] = []
    for t in tasks:
        if not isinstance(t, dict):
            continue
        is_open = t.get("tila") in ("open", "snoozed")
        if is_open and t.get("kontakti_ref"):
            open_task_by_contact[t["kontakti_ref"]] = True
        if t.get("tila") == "open" and t.get("eranpaiva"):
            due = _parse_ms(t.get("eranpaiva"))
            if due is not None and due < now_ms:
                overdue.append({"task": t, "over": _days_between(now_ms, due)})

    # last touch (any activity) + last poor call, per contact
    last_touch: dict = {}
    last_poor_call: dict = {}
    for a in activities:
        if not isinstance(a, dict):
            continue
        cid = a.get("kontakti_ref")
        if not cid:
            continue
        when = _parse_ms(a.get("ajankohta"))
        if when is None:
            when = _parse_ms(a.get("luotu"))
        if when is None:
            continue
        if last_touch.get(cid) is None or when > last_touch[cid]:
            last_touch[cid] = when
        is_poor_call = (
            a.get("tyyppi") == "call" and a.get("tila") == "done" and a.get("tulos") in ("negative", "neutral")
        )
        if is_poor_call and (cid not in last_poor_call or when > last_poor_call[cid]["ms"]):
            last_poor_call[cid] = {"ms": when, "tulos": a.get("tulos")}

    def last_touch_of(c: dict) -> int | None:
        vals = [
            v
            for v in (_parse_ms(c.get("viimeisin_kosketus")), last_touch.get(c.get("id")), _parse_ms(c.get("luotu")))
            if v is not None
        ]
        return max(vals) if vals else None

    out: list[dict] = []

    # per contact -> the single most relevant "create task" proposal (highest severity wins)
    for c in contacts:
        if not isinstance(c, dict):
            continue
        if c.get("tila") == "menetetty":
            continue
        if open_task_by_contact.get(c.get("id")):  # a follow-up is already scheduled
            continue
        cands: list[dict] = []
        lc = last_poor_call.get(c.get("id"))
        if lc:
            cands.append(
                {
                    "kind": "negative_call_followup",
                    "severity": "high",
                    "metric": _days_between(now_ms, lc["ms"]),
                    "due": cfg["negativeCallFollowupDays"],
                    "type": "call",
                    "prio": "korkea",
                    "tulos": lc["tulos"],
                }
            )
        if c.get("tila") in cfg["newContactStages"]:
            luotu = _parse_ms(c.get("luotu"))
            age = _days_between(now_ms, luotu) if luotu is not None else 0
            cands.append(
                {
                    "kind": "new_contact_no_task",
                    "severity": "high" if age >= 2 else "medium",
                    "metric": age,
                    "due": 2,
                    "type": "call",
                    "prio": "normaali",
                }
            )
        if c.get("tila") in cfg["activeContactStages"]:
            lt = last_touch_of(c)
            since = _days_between(now_ms, lt) if lt is not None else 999
            if since >= cfg["coldContactDays"]:
                cands.append(
                    {
                        "kind": "cold_contact",
                        "severity": "high" if since >= cfg["coldContactDays"] * 2 else "medium",
                        "metric": since,
                        "due": 1,
                        "type": "call",
                        "prio": "normaali",
                    }
                )
        if not cands:
            continue
        cands.sort(key=lambda x: (-_SEV[x["severity"]], -x["metric"]))
        best = cands[0]
        out.append(
            {
                "id": f"{best['kind']}:{c.get('id')}",
                "kind": best["kind"],
                "severity": best["severity"],
                "subjectType": "contact",
                "subjectId": c.get("id"),
                "subjectLabel": _contact_name(c),
                "metric": best["metric"],
                "context": {"tila": c.get("tila"), "email": c.get("email") or "", "tulos": best.get("tulos") or ""},
                "action": {
                    "create": {
                        "tyyppi": best["type"],
                        "prioriteetti": best["prio"],
                        "dueInDays": best["due"],
                        "kontakti_ref": c.get("id"),
                    }
                },
            }
        )

    # per open deal -> closing_soon > stale_deal > open_deal_no_task
    for d in deals:
        if not isinstance(d, dict) or d.get("tila") != "open":
            continue
        cid = d.get("kontakti_ref") or None
        has_open = bool(open_task_by_contact.get(cid)) if cid else False
        best = None
        close = _parse_ms(d.get("odotettu_klousaus"))
        if close is not None:
            days_to_close = _days_between(close, now_ms)  # negative if past
            if days_to_close <= cfg["closingSoonDays"]:
                best = {
                    "kind": "closing_soon",
                    "severity": "high",
                    "metric": days_to_close,
                    "due": max(0, min(1, days_to_close)),
                    "type": "call",
                    "prio": "korkea",
                }
        if not best and not has_open:
            last_act = last_touch.get(cid) if cid else None
            if last_act is not None:
                since_act = _days_between(now_ms, last_act)
            else:
                luotu = _parse_ms(d.get("luotu"))
                since_act = _days_between(now_ms, luotu) if luotu is not None else 999
            if since_act >= cfg["dealStaleDays"]:
                best = {
                    "kind": "stale_deal",
                    "severity": "medium",
                    "metric": since_act,
                    "due": 2,
                    "type": "call",
                    "prio": "korkea",
                }
            else:
                best = {
                    "kind": "open_deal_no_task",
                    "severity": "low",
                    "metric": since_act,
                    "due": 2,
                    "type": "call",
                    "prio": "normaali",
                }
        if not best:
            continue
        out.append(
            {
                "id": f"{best['kind']}:{d.get('id')}",
                "kind": best["kind"],
                "severity": best["severity"],
                "subjectType": "deal",
                "subjectId": d.get("id"),
                "subjectLabel": d.get("otsikko") or d.get("id"),
                "metric": best["metric"],
                "context": {
                    "arvo": d.get("arvo"),
                    "valuutta": d.get("valuutta") or "EUR",
                    "vaihe": d.get("vaihe"),
                    "kontakti_ref": cid,
                    "kontakti_nimi": _contact_name(contact_by_id.get(cid)) if cid else "",
                },
                # deal_ref rides in the scan output (Tier-1 parity) but is DROPPED when we build the crm-task
                # record — the locked crm-task schema rejects deal_ref.
                "action": {
                    "create": {
                        "tyyppi": best["type"],
                        "prioriteetti": best["prio"],
                        "dueInDays": best["due"],
                        "kontakti_ref": cid,
                        "deal_ref": d.get("id"),
                    }
                },
            }
        )

    # overdue OPEN tasks (existing task subjects — surfaced, never duplicated)
    for o in overdue:
        t = o["task"]
        out.append(
            {
                "id": f"overdue_task:{t.get('id')}",
                "kind": "overdue_task",
                "severity": "high" if o["over"] > cfg["overdueHighDays"] else "medium",
                "subjectType": "crm-task",
                "subjectId": t.get("id"),
                "subjectLabel": t.get("otsikko") or t.get("id"),
                "metric": o["over"],
                "context": {
                    "tyyppi": t.get("tyyppi"),
                    "kontakti_ref": t.get("kontakti_ref") or None,
                    "kontakti_nimi": _contact_name(contact_by_id.get(t.get("kontakti_ref")))
                    if t.get("kontakti_ref")
                    else "",
                },
                "action": {"openTask": t.get("id")},
            }
        )

    out.sort(key=lambda p: (-_SEV[p["severity"]], -p["metric"]))
    return out


# ── proposal -> crm-task record ─────────────────────────────────────────────────────────────────
# Finnish follow-up titles per kind (the workspace is a Finnish CRM). subjectLabel is the contact/deal name.
_TITLE_FI = {
    "cold_contact": "Ota yhteyttä: {label} (ei kontaktia {metric} pv)",
    "new_contact_no_task": "Kontaktoi uusi liidi: {label}",
    "negative_call_followup": "Seuraa edellistä puhelua: {label}",
    "closing_soon": "Klousaus lähestyy: {label}",
    "stale_deal": "Aktivoi pysähtynyt diili: {label}",
    "open_deal_no_task": "Suunnittele seuraava askel: {label}",
}


def _reason_en(p: dict) -> str:
    """English one-line reason (mirror of CADENCE.followups.reasonEn) — stored in kuvaus."""
    k, m, ctx = p["kind"], p["metric"], p.get("context") or {}
    return {
        "overdue_task": f"task is {m} day(s) overdue",
        "cold_contact": f"no contact in {m} days",
        "new_contact_no_task": f"new lead with no task yet ({m} days old)",
        "negative_call_followup": f"last call went {ctx.get('tulos') or 'poorly'}, no follow-up scheduled",
        "closing_soon": (f"expected close was {-m} day(s) ago" if m < 0 else f"expected to close in {m} day(s)"),
        "stale_deal": f"open deal, no activity in {m} days",
        "open_deal_no_task": "open deal with no scheduled task",
    }.get(k, "needs attention")


def dedup_id(key: str) -> str:
    """Deterministic crm-task id from a proposal's dedup key ("<kind>:<subjectId>"). Stable across runs,
    so re-writing produces the SAME record slot (idempotent) — the primary, restart-surviving dedup."""
    return "cf-" + re.sub(r"[^a-zA-Z0-9_-]+", "-", key).strip("-")


def build_task_from_action(p: dict, luonut: str, today: datetime.date) -> dict:
    """Map a `create` proposal to a locked-schema-safe crm-task record. Sends ONLY allowed keys
    (id, otsikko, tyyppi, kontakti_ref?, prioriteetti, eranpaiva, tila, kuvaus, luotu, luonut) — NO
    deal_ref, NO omistaja (additionalProperties:false)."""
    create = p["action"]["create"]
    label = p.get("subjectLabel") or p.get("subjectId") or ""
    title = _TITLE_FI.get(p["kind"], "Seuraa: {label}").format(label=label, metric=p.get("metric"))
    eranpaiva = (today + datetime.timedelta(days=int(create.get("dueInDays") or 0))).isoformat()
    rec = {
        "id": dedup_id(p["id"]),
        "otsikko": title[:200],
        "tyyppi": create.get("tyyppi") or "call",
        "prioriteetti": create.get("prioriteetti") or "normaali",
        "eranpaiva": eranpaiva,
        "tila": "open",
        # store the reason + dedup marker so the owner sees WHY, and a human can trace the key.
        "kuvaus": f"{_reason_en(p)} · [cadence-followup:{p['id']}]",
        "luotu": today.isoformat(),
        "luonut": luonut,
    }
    if create.get("kontakti_ref"):
        rec["kontakti_ref"] = create["kontakti_ref"]
    return rec


# ── the work loop ───────────────────────────────────────────────────────────────────────────────
def _call(tool_name: str, payload: dict, *, quiet: bool = False):
    return _aimeat_call(AGENT, tool_name, payload, quiet=quiet)


def _own_gaii() -> str:
    """This agent's GAII (<agent>#<owner>@<node>) for `luonut`; falls back to the bare agent name."""
    if AGENT in _GAII_CACHE:
        return _GAII_CACHE[AGENT]
    data = _call("aimeat_agents_list", {}, quiet=True) or {}
    for a in data.get("agents") or []:
        if a.get("name") == AGENT and a.get("gaii"):
            _GAII_CACHE[AGENT] = a["gaii"]
            return a["gaii"]
    return AGENT


def _is_stub(it) -> bool:
    """A two-step INDEX entry: an id/title stub with none of the record's domain fields."""
    if not isinstance(it, dict):
        return False
    payload = set(it) - {"id", "title", "_updatedAt", "_createdAt", "_version"}
    return bool(it.get("id")) and not payload


def read_all_crm_records(oid: str, wid: str) -> dict | None:
    """Read every field of the five CRM spaces in (oid, wid). The proven 0.16.x connector returns full
    record values under `objects[<space>]`; a newer two-step connector returns id/title index stubs —
    detected here and batch-opened with ids:[...]. Returns {space: [records]} or None (no access)."""
    data = _call("aimeat_workspace_read", {"organism_id": oid, "ws": wid})
    if not data or data.get("manifest") is None:
        return None
    objects = data.get("objects") or {}
    out: dict = {}
    for space in _CRM_SPACES:
        items = list(objects.get(space) or [])
        if items and all(_is_stub(it) for it in items):  # index-only shape -> batch-open by ids
            ids = [it.get("id") for it in items if isinstance(it, dict) and it.get("id")]
            full = _call("aimeat_workspace_read", {"organism_id": oid, "ws": wid, "space": space, "ids": ids}) or {}
            items = list((full.get("objects") or {}).get(space) or items)
        out[space] = items
    return out


def _resolve_band(band: str | None) -> str:
    """Autonomy band: explicit arg wins, else env CADENCE_FOLLOWUP_BAND, else 'propose'. Anything other
    than 'auto' is treated as propose (draft-only) — fail safe."""
    b = band or os.getenv("CADENCE_FOLLOWUP_BAND", "propose")
    return "auto" if b == "auto" else "propose"


def process_cadence_followups(
    max_items: int = 5, targets: list[tuple[str, str]] | None = None, band: str | None = None
) -> dict:
    """Scan the agent's CADENCE workspaces and draft/create follow-up crm-tasks. Deterministic (no LLM).

    `targets` scopes the scan to specific (organism_id, ws) pairs (the record-push path passes just the
    event's own workspace); None auto-discovers member workspaces AND self-gates on engagements. `band`
    picks the autonomy level. Bounded to `max_items` created tasks per pass. Output-dedup on the
    deterministic task id makes every pass idempotent. Returns counts."""
    band = _resolve_band(band)
    pairs = targets if targets is not None else member_workspaces(AGENT)
    if targets is None:  # gate the discovery/poll path on engagements (the push path is gated by the daemon)
        from crewaimeat.engagements import engaged_pairs

        pairs = engaged_pairs(AGENT, pairs, contract=CONTRACT["id"])
    today = datetime.date.today()
    luonut = _own_gaii()
    created = published = skipped = failed = 0
    for oid, wid in pairs:
        if created >= max_items:
            break
        data = read_all_crm_records(oid, wid)
        if data is None:
            continue
        existing_task_ids = {t.get("id") for t in data.get(TASK) or [] if isinstance(t, dict)}
        for p in scan(data, None, None):
            if created >= max_items:
                break
            if "openTask" in p.get("action", {}):  # overdue existing task — surfaced only, never duplicated
                continue
            key = p["id"]
            tid = dedup_id(key)
            if key in _PROCESSED or tid in existing_task_ids:  # per-run + output dedup
                skipped += 1
                continue
            _PROCESSED.add(key)
            rec = build_task_from_action(p, luonut=luonut, today=today)
            wrote = _call(
                "aimeat_workspace_write", {"organism_id": oid, "ws": wid, "space": TASK, "id": rec["id"], "value": rec}
            )
            if not wrote:
                failed += 1
                print(f"[{AGENT}] crm-task write FAILED for {tid} in {wid}", file=sys.stderr)
                continue
            created += 1
            existing_task_ids.add(tid)
            if band == "auto":  # publish draft -> latest (the workspace publish gate may still hold it)
                pub = _call(
                    "aimeat_workspace_publish",
                    {"organism_id": oid, "ws": wid, "namespace": "crm.tasks", "id": rec["id"]},
                )
                if pub:
                    published += 1
    return {"created": created, "published": published, "skipped": skipped, "failed": failed, "band": band}


def make_cadence_tools(agent_name: str) -> list:
    """The single contract-processing tool: scan the CADENCE workspaces and draft/create follow-ups."""

    @tool("process_cadence_followups")
    def _process(max_items: int = 5) -> str:
        """Scan the CADENCE CRM workspaces this agent belongs to and draft (or, under the auto band,
        create) follow-up crm-task records for stale/cold/overdue relationships and closing deals.
        Deterministic; never contacts anyone or sends anything. Returns the counts."""
        res = process_cadence_followups(max_items=max_items)
        return (
            f"cadence-followup ({res['band']}): created {res['created']} task(s), "
            f"published {res['published']}, skipped {res['skipped']}, failed {res['failed']}."
        )

    return [_process]

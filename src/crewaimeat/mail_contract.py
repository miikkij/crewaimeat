"""postman: a DETERMINISTIC mail-out workspace-contract + the 07:00 morning report.

Layer 1 — the mail contract (any agent gets email-out by writing ONE record):
  inputs : `mail-request` (records) — trigger: status == 'requested'
             { id, subject(required), body_md(required), to?, image_key?, requested_by?, status }
  outputs: none (the lifecycle IS the audit trail: requested -> in-progress -> done | failed)
  Sending is plain smtplib — zero LLM. The recipient is FORCED through the AIMEAT_MAIL_TO
  allowlist (.env): any `to` not on the list is refused, so the structural spam risk is zero.

Layer 2 — the morning report (07:00 Europe/Helsinki):
  The idle hook checks the clock; inside the 07:00-07:30 window it composes yesterday's digest
  (activity delta across the crewaimeat organism + the SOME radar + a day-brightening image via
  SearXNG/vision) and writes it as a `mail-request` record (id = morning-<date>) — which the same
  pass then sends. Dedup = the record's existence (restart-safe, canon rule 5).

SMTP env (.env): AIMEAT_SMTP_HOST / _PORT / _USER / _PASS / _FROM + AIMEAT_MAIL_TO (allowlist).
"""

from __future__ import annotations

import datetime
import os
import re
import smtplib
import sys
from email.message import EmailMessage
from email.utils import make_msgid
from zoneinfo import ZoneInfo

from crewai.tools import tool

from crewaimeat.aimeat_crew import _aimeat_call, member_workspaces

AGENT = "postman"
IN_SPACE, IN_NS = "mail-request", "shared.mail_requests"

_HOME_ORG = "b784641b-a4dd-4d69-adb6-9954dc813e1e"   # crewaimeat — the morning report's home
_HOME_WS = "ws-mq5vvdgsjwp"                           # Internal (mail-request records live here)
_RADAR_WS = "ws-mq641mohh0e"                          # Social Radar (SOME section source)
_TZ = ZoneInfo("Europe/Helsinki")
_MORNING_HOUR = 7  # 07:00-07:30 local window

# Runaway guard (canon rule 5): ids handled THIS run; the requested->done lifecycle is the
# restart-surviving dedup, and the morning report dedups on its own record's existence.
_PROCESSED: set[str] = set()

CONTRACT = {
    "id": "mail",
    "spaces": [
        {"space": IN_SPACE, "namespace": IN_NS, "mode": "records",
         "schema": {"type": "object", "required": ["id", "subject", "body_md", "status"],
                    "properties": {"id": {"type": "string"}, "subject": {"type": "string"},
                                   "body_md": {"type": "string"}, "to": {"type": "string"},
                                   "image_key": {"type": "string"}, "requested_by": {"type": "string"},
                                   "error": {"type": "string"},
                                   "status": {"type": "string",
                                              "enum": ["requested", "in-progress", "done", "failed"]}}}},
    ],
}


def _call(tool_name: str, payload: dict):
    return _aimeat_call(AGENT, tool_name, payload)


# --------------------------------------------------------------------------- #
# SMTP send (deterministic; allowlist-enforced)
# --------------------------------------------------------------------------- #
def _md_to_html(md: str) -> str:
    """Minimal, deterministic markdown -> HTML for digest emails (headers, bold, bullets, links)."""
    html_lines, in_list = [], False
    for line in md.splitlines():
        line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
        line = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r'<a href="\2">\1</a>', line)
        if line.startswith("## "):
            if in_list:
                html_lines.append("</ul>"); in_list = False
            html_lines.append(f"<h3>{line[3:]}</h3>")
        elif line.startswith("# "):
            if in_list:
                html_lines.append("</ul>"); in_list = False
            html_lines.append(f"<h2>{line[2:]}</h2>")
        elif line.startswith("- "):
            if not in_list:
                html_lines.append("<ul>"); in_list = True
            html_lines.append(f"<li>{line[2:]}</li>")
        elif line.strip() == "":
            if in_list:
                html_lines.append("</ul>"); in_list = False
            html_lines.append("<br>")
        else:
            html_lines.append(f"<p>{line}</p>")
    if in_list:
        html_lines.append("</ul>")
    return "\n".join(html_lines)


def send_mail(subject: str, body_md: str, to: str | None = None,
              image: bytes | None = None, image_mime: str = "image/jpeg") -> str | None:
    """Send one email over SMTP. Returns None on success, an error string on failure.

    The recipient is forced through the AIMEAT_MAIL_TO allowlist — a `to` not on the list is
    REFUSED (fail loud), and when `to` is omitted the first allowlisted address is used."""
    host, user = os.getenv("AIMEAT_SMTP_HOST"), os.getenv("AIMEAT_SMTP_USER")
    pwd, sender = os.getenv("AIMEAT_SMTP_PASS"), os.getenv("AIMEAT_SMTP_FROM") or os.getenv("AIMEAT_SMTP_USER")
    port = int(os.getenv("AIMEAT_SMTP_PORT") or 587)
    allow = [a.strip().lower() for a in (os.getenv("AIMEAT_MAIL_TO") or "").split(",") if a.strip()]
    if not (host and user and pwd):
        return "SMTP not configured (AIMEAT_SMTP_HOST/_USER/_PASS missing from .env)"
    if not allow:
        return "AIMEAT_MAIL_TO allowlist is empty — refusing to send anywhere"
    rcpt = (to or allow[0]).strip().lower()
    if rcpt not in allow:
        return f"recipient '{rcpt}' is not on the AIMEAT_MAIL_TO allowlist — refused"

    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, sender, rcpt
    msg.set_content(body_md)  # plain-text part = the markdown itself
    html = _md_to_html(body_md)
    if image:
        cid = make_msgid()
        html = f'<img src="cid:{cid[1:-1]}" style="max-width:640px;border-radius:8px"><br>\n' + html
        msg.add_alternative(f"<html><body>{html}</body></html>", subtype="html")
        msg.get_payload()[1].add_related(image, maintype=image_mime.split("/")[0],
                                         subtype=image_mime.split("/")[1], cid=cid)
    else:
        msg.add_alternative(f"<html><body>{html}</body></html>", subtype="html")
    try:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            s.login(user, pwd)
            s.send_message(msg)
        return None
    except Exception as exc:  # noqa: BLE001
        return f"SMTP send failed: {exc!r}"


# --------------------------------------------------------------------------- #
# Layer 1: the mail-request contract loop
# --------------------------------------------------------------------------- #
def _advance(oid: str, wid: str, rec: dict, **changes) -> None:
    out = {k: v for k, v in {**rec, **changes}.items() if not k.startswith("_")}
    if _call("aimeat_workspace_write", {"organism_id": oid, "ws": wid, "space": IN_SPACE, "id": out["id"], "value": out}):
        _call("aimeat_workspace_publish", {"organism_id": oid, "ws": wid, "namespace": IN_NS, "id": out["id"]})


def _storage_image(image_key: str) -> tuple[bytes, str] | None:
    """Fetch an image from any /v1/pub URL or this agent's storage key, for inline embedding."""
    import requests as _rq
    try:
        if image_key.startswith("http"):
            r = _rq.get(image_key, timeout=30)
        else:
            from crewaimeat.generator_tool import _discover_owner, _token
            tok, url = _token(AGENT, _discover_owner(AGENT))
            r = _rq.get(f"{url.rstrip('/')}/v1/storage/{image_key}", headers={"Authorization": f"Bearer {tok}"}, timeout=30)
        mime = (r.headers.get("Content-Type") or "image/jpeg").split(";")[0]
        return (r.content, mime) if r.status_code == 200 and mime.startswith("image/") else None
    except Exception:  # noqa: BLE001
        return None


def process_mail(max_items: int = 5, targets: list[tuple[str, str]] | None = None) -> dict:
    """Send pending `mail-request` records across the agent's member workspaces. Deterministic."""
    pairs = targets if targets is not None else member_workspaces(AGENT)
    sent = failed = 0
    for oid, wid in pairs:
        if sent + failed >= max_items:
            break
        data = _call("aimeat_workspace_read", {"organism_id": oid, "ws": wid})
        if not data or data.get("manifest") is None:
            continue
        for rec in (data.get("objects", {}) or {}).get(IN_SPACE) or []:
            rid = rec.get("id")
            if rec.get("status") != "requested" or not rid:
                continue
            if rid in _PROCESSED:  # per-run guard against a stale 'requested' read
                continue
            if sent + failed >= max_items:
                break
            _PROCESSED.add(rid)
            _advance(oid, wid, rec, status="in-progress")
            img = _storage_image(rec["image_key"]) if rec.get("image_key") else None
            err = send_mail(rec.get("subject") or "(no subject)", rec.get("body_md") or "",
                            to=rec.get("to"), image=img[0] if img else None,
                            image_mime=img[1] if img else "image/jpeg")
            if err:
                _advance(oid, wid, rec, status="failed", error=err[:300])
                failed += 1
                print(f"[{AGENT}] mail FAILED for {rid}: {err}", file=sys.stderr)
            else:
                _advance(oid, wid, rec, status="done")
                sent += 1
    return {"sent": sent, "failed": failed}


# --------------------------------------------------------------------------- #
# Layer 2: the 07:00 morning report
# --------------------------------------------------------------------------- #
_DAY_IMAGE_QUERIES = [  # rotated by weekday — something nice to wake up to
    "sunrise over a calm finnish lake, golden light",
    "misty pine forest morning sunbeams",
    "cozy cabin morning coffee by a window, snow",
    "northern lights over lapland winter night",
    "archipelago summer morning sailboat",
    "autumn forest path golden leaves morning",
    "sunlit wildflower meadow summer morning",
]


def _day_image() -> tuple[bytes, str] | None:
    """One day-brightening image: SearXNG image search + vision pick of the best candidate."""
    from crewaimeat.image_contract import _download_image, _searxng_images, _vision_meta
    q = _DAY_IMAGE_QUERIES[datetime.date.today().toordinal() % len(_DAY_IMAGE_QUERIES)]
    best, best_rel = None, -1
    for c in _searxng_images(q, 6):
        got = _download_image(c["img_src"])
        if not got:
            continue
        meta = _vision_meta(got[0], got[1], q)
        rel = (meta or {}).get("relevance") or 0
        if rel > best_rel:
            best, best_rel = got, rel
        if best_rel >= 9 or (best and best_rel >= 7):
            break
    return best


def _radar_section(radar: list[dict]) -> str:
    """The freshest SOME-radar opportunities + reply drafts from the Social Radar workspace."""
    d = _call("aimeat_workspace_read", {"organism_id": _HOME_ORG, "ws": _RADAR_WS}) or {}
    objs = d.get("objects", {}) or {}
    draft_space = next((s for s in objs if "draft" in s.lower() or "reply" in s.lower()), None)
    drafts = len(objs.get(draft_space) or []) if draft_space else 0
    lines = [f"- {r['title']}" + (f" — {r['url']}" if r['url'] else "") for r in radar[:5]]
    if not lines:
        return "## SOME-radar\n\n- (ei uusia osumia radarilla)\n"
    return ("## SOME-radar\n\n" + "\n".join(lines)
            + (f"\n\n{drafts} vastausluonnosta odottaa katselmointiasi." if drafts else "") + "\n")


# Competitor / domain watch — what commercial players in our space sell, advertise and discuss.
# Override with AIMEAT_COMPETITOR_QUERIES (comma-separated search queries) in .env.
_COMPETITOR_QUERIES = [
    "AI agent platform pricing launch news",
    "CrewAI OR LangGraph OR AutoGen agent orchestration news",
    "autonomous AI agents product launch commercial",
]


def _radar_items() -> list[dict]:
    """Fresh SOME-radar opportunities (title+url) — shared by the radar section + the analyst."""
    d = _call("aimeat_workspace_read", {"organism_id": _HOME_ORG, "ws": _RADAR_WS}) or {}
    objs = d.get("objects", {}) or {}
    opp_space = next((s for s in objs if "opportunit" in s.lower()), None)
    return [{"title": (o.get("title") or o.get("id") or "?")[:90], "url": o.get("url") or "",
             "score": o.get("score")} for o in (objs.get(opp_space) or [])[:8]] if opp_space else []


def _insights_section(events: list[dict], radar: list[dict]) -> str:
    """Effort analysis + accomplishments + TODAY's action points (incl. SOME threads worth a reply)."""
    from crewaimeat.llm import get_llm
    ev_lines = "\n".join(
        f"- {e.get('at','')} · {(e.get('agent') or e.get('actor') or '?')} {e.get('action')} "
        f"{e.get('type')}/{e.get('instance')}" for e in events[:200]) or "(no events)"
    radar_lines = "\n".join(f"- {r['title']} — {r['url']}" for r in radar) or "(radar empty)"
    prompt = (
        "You are a sharp, warm morning-briefing analyst for a one-person AI-agent project.\n\n"
        f"RAW ACTIVITY (last 24h, who did what):\n{ev_lines}\n\n"
        f"SOME RADAR (fresh threads where engaging might be worth it):\n{radar_lines}\n\n"
        "Write THREE markdown sections, in Finnish, concise and concrete:\n"
        "## Mihin tehot menivät\n(2-4 sentences: where the effort actually went, any imbalance worth noticing)\n\n"
        "## Mitä saatiin aikaan\n(3-6 bullets of OUTCOMES, not activity — things that now exist/work)\n\n"
        "## Tänään kannattaa\n(3-5 action points for TODAY. If a radar thread looks genuinely worth a reply, "
        "say 'Käy katsomassa: <title> — <url>' with one line on WHY and what angle a reply could take. "
        "Only real items from the data above; if the radar is empty, suggest the most leveraged next step instead.)\n\n"
        "Use ONLY the data above. No fluff, no invented items."
    )
    try:
        llm = get_llm(for_tool_use=False, temperature=0.4, agent_name=AGENT)
        out = (llm.call([{"role": "user", "content": prompt}]) or "").strip()
        return out + "\n" if out else "## Tänään kannattaa\n\n- (analyysi epäonnistui — tyhjä vastaus)\n"
    except Exception as exc:  # noqa: BLE001 — the mail still goes out, loudly noting the gap
        return f"## Tänään kannattaa\n\n- (analyysin tuotanto epäonnistui: {exc!r})\n"


def _competitor_section() -> str:
    """What commercial players in our domain sell, advertise and discuss — a daily sweep."""
    from crewaimeat.article_extract import _trafilatura_text
    from crewaimeat.fetch_pipeline import _searxng_urls
    from crewaimeat.llm import get_llm
    queries = [q.strip() for q in (os.getenv("AIMEAT_COMPETITOR_QUERIES") or "").split(",") if q.strip()] \
        or _COMPETITOR_QUERIES
    docs: list[str] = []
    for q in queries:
        for u in _searxng_urls(q, "en", "week", n=3):
            if len(docs) >= 6:
                break
            try:
                txt = _trafilatura_text(u)
            except Exception:  # noqa: BLE001
                txt = ""
            if txt and len(txt) > 400:
                docs.append(f"[{u}]\n{txt[:2500]}")
    if not docs:
        return "## Kilpailijakatsaus\n\n- (ei tuoreita osumia tällä haulla tänään)\n"
    prompt = (
        "You are a competitor-watch analyst for an AI-agent substrate/orchestration product.\n\n"
        "SOURCES (this week, our domain):\n\n" + "\n\n".join(docs) +
        "\n\nWrite ONE markdown section in Finnish:\n"
        "## Kilpailijakatsaus\n(4-7 bullets: WHO did/said WHAT — what they sell, what they advertise, "
        "what people discuss; each bullet names the player and cites its source URL in parentheses. "
        "End with one line: the single most relevant signal for us and why.)\n\n"
        "Use ONLY facts from the sources. No speculation beyond the final signal line."
    )
    try:
        llm = get_llm(for_tool_use=False, temperature=0.3, agent_name=AGENT)
        out = (llm.call([{"role": "user", "content": prompt}]) or "").strip()
        return out + "\n" if out.startswith("##") else f"## Kilpailijakatsaus\n\n{out}\n"
    except Exception as exc:  # noqa: BLE001
        return f"## Kilpailijakatsaus\n\n- (katsauksen tuotanto epäonnistui: {exc!r})\n"


def _activity_section(now: datetime.datetime) -> str:
    """Yesterday's organism-wide activity, distilled (reuses the activity-reporter machinery)."""
    from crewaimeat.activity_contract import _distill, _gather
    since = (now - datetime.timedelta(hours=24)).isoformat()
    try:
        events = _gather(_HOME_ORG, "*", since)
        if not events:
            return "## Eilen organismissa\n\n- (hiljainen vuorokausi — ei kirjattua aktiviteettia)\n"
        report = _distill(events, "the whole organism", since,
                          "a warm, concise morning-briefing narrator — factual, a little sunshine")
        return f"## Eilen organismissa\n\n{report}\n"
    except Exception as exc:  # noqa: BLE001 — the mail still goes out, loudly noting the gap
        return f"## Eilen organismissa\n\n- (aktiviteettikoosteen tuotanto epäonnistui: {exc!r})\n"


def morning_report_due(now: datetime.datetime | None = None) -> bool:
    """True inside the 07:00-07:30 Europe/Helsinki window when today's report record is absent."""
    now = now or datetime.datetime.now(_TZ)
    if not (now.hour == _MORNING_HOUR and now.minute < 30):
        return False
    rid = f"morning-{now.date().isoformat()}"
    d = _call("aimeat_workspace_read", {"organism_id": _HOME_ORG, "ws": _HOME_WS}) or {}
    existing = {r.get("id") for r in (d.get("objects", {}) or {}).get(IN_SPACE) or []}
    return rid not in existing


def build_morning_report() -> dict:
    """Compose today's morning report as a mail-request record (the same pass then sends it).

    Sections: yesterday's story (activity distill) · effort analysis + outcomes + TODAY's action
    points (incl. SOME threads worth a reply) · the SOME radar · a competitor/domain watch."""
    from crewaimeat.activity_contract import _gather
    now = datetime.datetime.now(_TZ)
    rid = f"morning-{now.date().isoformat()}"
    since = (now - datetime.timedelta(hours=24)).isoformat()
    try:
        events = _gather(_HOME_ORG, "*", since)
    except Exception:  # noqa: BLE001
        events = []
    radar = _radar_items()
    body = (f"# Huomenta! ☀️ {now.strftime('%A %d.%m.%Y')}\n\n"
            + _activity_section(now) + "\n"
            + _insights_section(events, radar) + "\n"
            + _radar_section(radar) + "\n"
            + _competitor_section()
            + "\n*— postman · crewaimeat · kuva: SearXNG + qwen-vl*")
    img = _day_image()
    img_note = ""
    if img:  # attach inline via the record? Records carry no bytes — send directly with the image.
        err = send_mail(f"Aamuraportti · {now.date().isoformat()}", body, image=img[0], image_mime=img[1])
    else:
        err = send_mail(f"Aamuraportti · {now.date().isoformat()}", body)
        img_note = " (no day image found)"
    # The record IS the audit trail + the once-per-day dedup — written done/failed after the send.
    rec = {"id": rid, "subject": f"Aamuraportti · {now.date().isoformat()}",
           "body_md": body[:6000], "requested_by": "postman/morning",
           "status": "failed" if err else "done", **({"error": err[:300]} if err else {})}
    _advance(_HOME_ORG, _HOME_WS, rec)
    print(f"[{AGENT}] morning report {rid}: {'FAILED ' + err if err else 'sent'}{img_note}", file=sys.stderr)
    return {"sent": 0 if err else 1, "failed": 1 if err else 0}


def idle_pass() -> dict:
    """One idle-hook pass: the 07:00 window check (clock + record-existence dedup, no LLM) plus
    the pending mail-request sweep. Vision/distill run only when a morning report is actually due."""
    res = {"sent": 0, "failed": 0}
    if morning_report_due():
        m = build_morning_report()
        res = {k: res[k] + m[k] for k in res}
    p = process_mail()
    return {k: res[k] + p[k] for k in res}


def make_mail_tools(agent_name: str) -> list:
    """The contract-processing tool: send pending mail-requests; allowlist-enforced."""

    @tool("process_mail")
    def _process(max_items: int = 5) -> str:
        """Send pending `mail-request` records in the workspaces this agent belongs to, over SMTP.
        The recipient allowlist (AIMEAT_MAIL_TO) is enforced on every send. Returns the counts."""
        res = process_mail(max_items=max_items)
        return f"postman: sent {res['sent']} mail(s), {res['failed']} failed."

    return [_process]

"""sanomat-desk: the reader-news + corrections desk for (L)AIMEAT Sanomat.

Three flows, one agent (deterministic routing; LLMs only where judgement lives):
  1. INTERVIEW (P1): a daily schedule fires a kickoff task -> the desk DMs the owner the day's
     interview; every owner reply in a desk thread (text + photos) is filed as Lukijoilta raw for
     the evening edition — write_pipeline's desk-A loop writes the article (persona Vilma Vinkki).
  2. TIPS (P1+P2): any logged-in federation user DMs a news tip. EXTERNAL senders' material passes
     the legal screen (crewaimeat.legal_screen) BEFORE it becomes raw; the owner's own does not.
     A flagged tip is declined at the boundary and the owner is notified.
  3. CORRECTIONS (P3): a DM starting with "oikaisu"/"korjaus"/"correction" files a request into the
     PUBLIC index sanomat.oikaisut.index (the app's Oikaisut page). The Lakiosasto arbiter rules:
     aiheeton -> final + pompous justification; oikaistaan -> HITL approval from the OWNER, then the
     correction is published into the next edition's article.oikaisut.

Register + approve (needs messages:send + messages:read at device-auth), then restart the fleet:
  npx aimeat@latest connect add --agent sanomat-desk --mode task-runner --url https://aimeat.io --owner <you>
"""

from __future__ import annotations

import sys

from crewai import Agent, Crew, Process, Task

from crewaimeat import corrections, dm, hitl, legal_screen, orchestrator, reader_desk, vision
from crewaimeat.aimeat_crew import BuildContext, CrewSpec, _aimeat_call, _now_context, run_crew
from crewaimeat.llm import get_llm
from crewaimeat.scheduler import make_schedule_tools

AGENT_NAME = "sanomat-desk"

README = """[[FIGLET:slant]["Sanomat Desk"]]

(L)AIMEAT Sanomien **lukijoilta-deski ja oikaisukanava**. Kolme palvelua DM:llä:

- **Vinkkaa uutinen** — kerro mitä tapahtui, liitä kuvia; Vilma Vinkki kirjoittaa jutun iltapainoksen
  Lukijoilta-osastoon. Ulkopuolisten materiaali kulkee lakiosaston seulan läpi ennen julkaisua.
- **Päivän haastattelu** — ajastettuna deski haastattelee omistajaa päivän kulusta; vastaukset ja kuvat
  päätyvät lehteen.
- **Oikaisupyyntö** — aloita viesti sanalla "OIKAISU". Lakiosasto käsittelee; tilan näet lehden
  Oikaisut-sivulta (aiheettomaksi todetut perustellaan julkisesti, hyväksytyt julkaistaan
  oikaisu-uutisena seuraavassa painoksessa).
- **Jalostettu vinkki** — anna oman AI-chattisi haastatella sinut: Sanomat-appin "Kopioi
  haastatteluprompt" -nappi (tai AIMEAT-kytketylle AI:lle skill `sanomat-tip-desk`) tuottaa valmiin
  `sanomat-vinkki`-paketin, jonka deski parsii suoraan otsikoksi ja jutuksi.

Komennot: "haastattele nyt", "haastattelu päivittäin klo 16:30", "lopeta haastattelu".
"""

CAPABILITY_TAGS = ["sanomat", "news-desk", "reader-tips", "corrections", "legal-screen", "role.task-runner"]
CAPABILITIES = {
    "technical": [
        {"name": "federated-dm", "type": "tool"},
        {"name": "vision", "type": "skill"},
        {"name": "legal-screen", "type": "skill"},
        {"name": "scheduler", "type": "tool"},
    ],
    "domain": [
        "reader news intake for (L)AIMEAT Sanomat (tips + daily owner interview)",
        "legal screening of external material (satire publication)",
        "formal correction-request channel with a public status index",
    ],
    "languages": ["fi", "en"],
}

CHAT_COMMANDS = [
    {
        "id": "tip",
        "label": "Vinkkaa uutinen",
        "description": "Kerro uutinen Lukijoilta-osastoon (kuvat liitteeksi)",
        "template": "Uutisvinkki: {{vinkki}}",
        "params": [{"name": "vinkki", "type": "text", "required": True, "placeholder": "Mitä tapahtui?"}],
    },
    {
        "id": "correction",
        "label": "Pyydä oikaisua",
        "description": "Virallinen oikaisupyyntö lakiosastolle",
        "template": "OIKAISU: {{pyynto}}",
        "params": [
            {"name": "pyynto", "type": "text", "required": True, "placeholder": "Mikä juttu, mikä väite on väärin?"}
        ],
    },
    {
        "id": "interview_now",
        "label": "Haastattele nyt",
        "description": "Päivän haastattelukysymykset heti",
        "template": "haastattele nyt",
    },
    {
        "id": "interview_daily",
        "label": "Haastattelu päivittäin",
        "description": "Ajasta päivän haastattelu (ennen klo 17:30)",
        "template": "haastattelu päivittäin klo {{time}}",
        "params": [{"name": "time", "type": "text", "required": True, "placeholder": "16:30"}],
    },
]

_CORRECTION_PREFIXES = ("oikaisu", "korjaus", "correction")


def _schedule_crew(request: str, today: str, llm) -> str:
    """Create/cancel the daily interview schedule — same wiring as social-briefing's schedule crew
    (agent_task back to SELF carrying the kickoff marker)."""
    mgr = Agent(
        role="Interview Scheduler",
        goal="Create, change, or cancel the daily Sanomat interview schedule exactly as asked.",
        backstory="You manage AIMEAT server-run schedules precisely and confirm what you did.",
        llm=llm,
        tools=make_schedule_tools(AGENT_NAME),
        allow_delegation=False,
        verbose=False,
    )
    task = Task(
        description=(
            f'Today is {today}. The user said: "{request}"\n\n'
            "If they want a DAILY interview at a time: call schedule_create with kind='agent_task', "
            f"target_agent='{AGENT_NAME}', task_title='Sanomat interview kickoff', "
            f"task_description='{reader_desk.KICKOFF_MARKER}', a 5-field cron for that local time "
            "(e.g. 16:30 -> '30 16 * * *'; it should fire BEFORE 17:30 so the answers make the evening "
            "edition), timezone 'Europe/Helsinki', display_name 'Sanomat daily interview', purpose "
            "'DM the owner the daily Sanomat interview'. If they want to STOP it: schedule_list, find the "
            "'Sanomat interview kickoff' schedule, and schedule_delete it. Then reply in one short Finnish "
            "sentence confirming what you did (include the time)."
        ),
        expected_output="A one-line Finnish confirmation of the schedule change.",
        agent=mgr,
    )
    return str(Crew(agents=[mgr], tasks=[task], process=Process.sequential).kickoff())


def _dm_request_and_attachments(event: dict) -> tuple[str, list]:
    """THIS event's full body + attachments — matched by event id in the thread (read-after-write safe,
    same shape as the concierge), falling back to the wake's preview."""
    mid, conv, _sender, preview, _subject = dm._inbound_fields(event)
    target = None
    if conv:
        thread = dm.dm_thread(AGENT_NAME, conv)
        for m in (thread.get("messages") if isinstance(thread, dict) else None) or []:
            if (m.get("id") or m.get("message_id")) == mid:
                target = m
                break
    request = (target.get("body") if target else None) or preview or ""
    attachments = (target.get("attachments") if target else None) or event.get("attachments") or []
    return str(request), attachments


def _owner_conv() -> str | None:
    """A standing DM thread with the owner for HITL gates + desk notices — reuse the interview thread
    when it exists, else open (and remember) a notice thread. NB hitl keeps ONE pending gate per
    conversation, so two simultaneous correction gates would clobber; acceptable at this volume."""
    cfg = reader_desk.get_config(AGENT_NAME)
    conv = cfg.get("interview_conversation_id") or cfg.get("owner_conversation_id")
    if conv:
        return conv
    to = reader_desk.owner_gaii(AGENT_NAME)
    if not to:
        return None
    res = dm.dm_send(AGENT_NAME, to, "Sanomat-desk: ilmoitus- ja hyväksyntäkanava avattu.", subject="Sanomat-desk")
    conv = orchestrator._conv_id(res) if res else None
    if conv:
        reader_desk.set_config(AGENT_NAME, owner_conversation_id=conv)
    return conv


def _notify_owner(text: str) -> None:
    """Dashboard note to the owner (never fails the caller)."""
    try:
        _aimeat_call(AGENT_NAME, "aimeat_message_send", {"content": text})
    except Exception as exc:  # noqa: BLE001
        print(f"[{AGENT_NAME}] owner notify failed: {exc!r}", file=sys.stderr)


def _handle_correction(sender: str, text: str) -> str:
    """File -> arbiter ruling -> (aiheeton: final) | (oikaistaan: HITL gate to the owner)."""
    entry = corrections.new_request(AGENT_NAME, sender=sender, text=text)
    try:
        ruling = corrections.judge_request(AGENT_NAME, entry, corrections.recent_headlines(AGENT_NAME))
    except corrections.CorrectionsUnavailable as exc:
        print(f"[{AGENT_NAME}] arbiter failed for {entry['id']}: {exc!r}", file=sys.stderr)
        _notify_owner(
            f"[sanomat-desk] Oikaisupyyntö {entry['id']} kirjattu, mutta arbiter ei ollut käytettävissä: {exc}"
        )
        return (
            f"Oikaisupyyntösi on kirjattu tunnisteella **{entry['id']}** (tila: vastaanotettu). "
            "Lakiosaston käsittely viivästyy teknisestä syystä — tila päivittyy lehden Oikaisut-sivulle."
        )
    if ruling["verdict"] == "aiheeton":
        corrections.set_status(
            AGENT_NAME, entry["id"], status="aiheeton", perustelu=ruling["perustelu"], resolved=entry["created"]
        )
        return (
            f"Lakiosasto on käsitellyt oikaisupyyntösi **{entry['id']}** ja todennut sen **aiheettomaksi**.\n\n"
            f"> {ruling['perustelu']}\n\n"
            "Ratkaisu perusteluineen on nähtävillä lehden Oikaisut-sivulla."
        )
    # oikaistaan -> a human approves before public content changes
    corrections.set_status(
        AGENT_NAME,
        entry["id"],
        status="odottaa-hyvaksyntaa",
        perustelu=ruling["perustelu"],
        oikaisu=ruling["oikaisu"],
        article_key=ruling["article_key"],
    )
    conv = _owner_conv()
    to = reader_desk.owner_gaii(AGENT_NAME)
    if conv and to:
        hitl.ask_approval(
            AGENT_NAME,
            to,
            conv,
            summary=(
                f"Oikaisupyyntö {entry['id']} ({entry['sender']}): lakiosasto esittää OIKAISUA.\n\n"
                f"Perustelu: {ruling['perustelu']}\n\nOikaisuteksti:\n{ruling['oikaisu']}"
            ),
            action_id="publish_correction",
            payload={"req_id": entry["id"], "oikaisu": ruling["oikaisu"], "requester": sender},
            yes="Julkaise oikaisu",
            no="Hylkää",
        )
    else:
        _notify_owner(f"[sanomat-desk] Oikaisu {entry['id']} odottaa hyväksyntää, mutta HITL-kanavaa ei saatu auki.")
    return (
        f"Oikaisupyyntösi **{entry['id']}** on otettu käsittelyyn: lakiosasto esittää oikaisua ja asia "
        "odottaa päätoimittajan vahvistusta. Tila päivittyy lehden Oikaisut-sivulle; hyväksytty oikaisu "
        "julkaistaan oikaisu-uutisena seuraavassa painoksessa."
    )


def _resolve_correction_gate(event: dict) -> str | None:
    """An interactive answer in the owner thread -> publish or reject the pending correction."""
    res = hitl.resolve(AGENT_NAME, event)
    if res is None or res.get("action_id") != "publish_correction":
        return None
    payload = res.get("payload") or {}
    req_id, oikaisu = payload.get("req_id"), payload.get("oikaisu")
    if not req_id:
        return "Hyväksyntävastaus ilman oikaisutunnistetta — ei toimenpiteitä."
    if not res.get("approved"):
        corrections.set_status(AGENT_NAME, req_id, status="hylatty", resolved=corrections._today())
        return f"Selvä — oikaisu **{req_id}** hylätty. Tila päivitetty Oikaisut-sivulle."
    entry = next((e for e in corrections.read_index(AGENT_NAME) if e.get("id") == req_id), None)
    if not entry:
        return f"Oikaisua {req_id} ei löytynyt indeksistä — ei julkaistu."
    date, edition = corrections.publish_correction(AGENT_NAME, entry, oikaisu or entry.get("oikaisu") or "")
    return f"Oikaisu **{req_id}** julkaistaan painoksessa {date} ({edition}). Tila: oikaistu."


def _handle_tip(sender: str, text: str, attachments: list, *, is_owner: bool) -> str:
    """Parse a refined block if present -> screen (external only) -> publish images -> append to the
    edition's lukijoilta raw -> ack. A `sanomat-vinkki` fenced block (refined in the sender's own AI
    chat, contract in the sanomat-tip-desk skill) supplies title/content directly; anything else uses
    the first-line-is-title heuristic. The legal screen always sees the material that would publish."""
    parsed = reader_desk.parse_vinkki_block(text)
    material = parsed["content"] if parsed else text
    notes = ""
    if attachments:
        blocks = [vision.analyze_attachment(AGENT_NAME, a) for a in attachments[:5]]
        notes = "\n\n".join(blocks)
    if not is_owner:
        try:
            verdict = legal_screen.screen_external(AGENT_NAME, sender=sender, text=material, attachment_notes=notes)
        except legal_screen.LegalScreenUnavailable as exc:
            print(f"[{AGENT_NAME}] legal screen unavailable: {exc!r}", file=sys.stderr)
            return (
                "Kiitos vinkistä — lakiosaston seula ei juuri nyt ole käytettävissä, joten en voi ottaa "
                "materiaalia vastaan. Yritä hetken päästä uudelleen."
            )
        if not verdict["ok"]:
            _notify_owner(
                f"[sanomat-desk] Ulkopuolinen vinkki ({sender}) hylättiin seulassa: "
                f"{'; '.join(verdict['issues'])} — {verdict['summary']}"
            )
            return (
                "Kiitos vinkistä. Lakiosasto on arvioinut materiaalin, eikä se tässä muodossa sovellu "
                "julkaistavaksi (mm. yksityisyys- ja oikeussyistä). Voit muotoilla vinkin uudelleen ilman "
                "tunnistettavia yksityishenkilöitä tai arkaluontoisia tietoja."
            )
    date, edition = reader_desk.next_evening_edition()
    images = reader_desk.publish_tip_images(AGENT_NAME, attachments, date=date) if attachments else []
    body = material if not notes else f"{material}\n\n[Liitteiden analyysi]\n{notes}"
    source = "haastattelu/omistaja" if is_owner else f"lukijavinkki ({sender.split('@')[0]})"
    try:
        date, edition = reader_desk.add_tip(
            AGENT_NAME,
            text=body,
            source=source,
            images=images,
            title=parsed["title"] if parsed else None,
            refined=bool(parsed),
            tip_type=parsed["type"] if parsed else None,
        )
    except RuntimeError as exc:
        print(f"[{AGENT_NAME}] add_tip failed: {exc!r}", file=sys.stderr)
        return "Vinkki EI tallentunut (tekninen vika) — yritä hetken päästä uudelleen."
    img_note = f" Kuvia mukana {len(images)}." if images else ""
    return (
        f"Kirjattu Lukijoilta-osastoon — juttu ilmestyy painoksessa **{date} ({edition})**.{img_note} "
        "Vilma Vinkki hoitaa loput. Voit lähettää lisää samaan ketjuun."
    )


def build_domain(ctx: BuildContext):
    """Task path: a SCHEDULED kickoff (marker in the description) sends the owner interview; any other
    task just points at the DM flows (this desk's real work is the DM loop)."""
    if reader_desk.KICKOFF_MARKER in (ctx.prompt or ""):
        ok = reader_desk.send_interview_kickoff(AGENT_NAME, ctx.today)
        msg = "Daily interview sent to the owner." if ok else "Interview kickoff could not be delivered."
    else:
        msg = (
            "sanomat-desk runs as a DM loop — DM me a news tip, 'OIKAISU: ...' for a correction, or "
            "'haastattele nyt' for the daily interview."
        )
    note = Agent(
        role="Desk Runner",
        goal="Report the desk action result.",
        backstory="You confirm the scheduled desk action.",
        llm=ctx.llm,
        allow_delegation=False,
        verbose=False,
    )
    task = Task(description=f"State exactly this and nothing else: {msg}", expected_output=msg, agent=note)
    return ([note], [task])


def run() -> None:
    _seen: set = set()

    def _responder(event: dict):
        _mid, conv, sender, _preview, _subject = dm._inbound_fields(event)
        # Sibling agents stay silent (loop guard — two DM crews must not bounce replies forever).
        roster = orchestrator.list_node_agents(AGENT_NAME)
        if orchestrator.in_roster(roster, sender):
            return ""
        # A structured ANSWER: the owner resolving a pending correction gate.
        if event.get("interactive") == "answers":
            resolved = _resolve_correction_gate(event)
            return resolved or ""
        text, attachments = _dm_request_and_attachments(event)
        text = text.strip()
        low = text.lower()
        is_owner = reader_desk.is_owner_human(AGENT_NAME, sender)
        # Corrections: anyone, message starts with the magic word (the chat command produces this too).
        if low.startswith(_CORRECTION_PREFIXES):
            return _handle_correction(sender, text)
        # A refined sanomat-vinkki package is ALWAYS material, never a command — route it before the
        # owner-command heuristics (a tip whose prose contains e.g. "haastattelee ... nyt" must not
        # trigger the interview command; found the hard way with the intake announcement tip).
        if reader_desk.parse_vinkki_block(text):
            return _handle_tip(sender, text, attachments, is_owner=is_owner)
        # Owner control commands.
        if is_owner and "haastattele" in low and "nyt" in low:
            reader_desk.set_config(AGENT_NAME, interview_conversation_id=conv)
            return reader_desk.build_interview(_now_context())
        if is_owner and "haastattelu" in low and any(k in low for k in ("päivittäin", "daily", "lopeta", "stop")):
            try:
                return _schedule_crew(text, _now_context(), get_llm(agent_name=AGENT_NAME))
            except Exception as exc:  # noqa: BLE001
                print(f"[{AGENT_NAME}] schedule crew failed: {exc!r}", file=sys.stderr)
                return "Ajastusta ei saatu muutettua — yritä uudelleen?"
        # Too short to be material -> guide.
        if len(text) < 15 and not attachments:
            return (
                "Tämä on (L)AIMEAT Sanomien lukijoilta-deski. Kerro uutisesi tähän (kuvat liitteeksi), "
                'aloita "OIKAISU:" jos haet oikaisua, tai sano "haastattele nyt".'
            )
        # Default: the message IS news material.
        return _handle_tip(sender, text, attachments, is_owner=is_owner)

    run_crew(
        CrewSpec(
            agent_name=AGENT_NAME,
            build_domain=build_domain,
            readme_md=README,
            temperature=0.3,
            listen_for=("tasks", "dms"),
            on_dm=lambda e: dm.handle_dm_event(AGENT_NAME, e, _responder, seen=_seen),
            tags=CAPABILITY_TAGS,
            capabilities=CAPABILITIES,
            chat_commands=CHAT_COMMANDS,
        )
    )


if __name__ == "__main__":
    run()

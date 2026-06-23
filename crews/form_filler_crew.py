"""form-filler: a DM agent that completes a form for you.

Send it a form (PDF) and it reads what the form asks for, replies with a checklist, takes your answers,
and hands back the COMPLETED form — a filled PDF if the form is fillable (AcroForm), and always a
completed-answers document you can submit or paste in. Works on flat/scanned forms too (the universal path).

  1. DM the form (PDF attached).  2. It lists the fields and asks for your values.  3. You reply with them.
  4. It returns the completed form.

Register + approve before running:
  npx aimeat@latest connect add --agent form-filler --mode task-runner --url https://aimeat.io --owner <your-aimeat-account>
Run: uv run python crews/form_filler_crew.py
"""

from __future__ import annotations

import json
import re
import sys

from crewai import Agent, Crew, Process, Task

from crewaimeat import dm, form_filler, orchestrator, session_store, storage, vision
from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.llm import get_llm

AGENT_NAME = "form-filler"

README = """[[FIGLET:slant]["Form Filler"]]

Send me a **form (PDF)** and I'll read what it asks for, ask you the values, and hand back the **completed
form** — a filled PDF when the form is fillable, plus a completed-answers document you can submit. I handle
flat/scanned forms too.

**How to use me:** DM me with a PDF attached. I'll list the fields and ask for your answers; reply with
them and I'll return the finished form.
"""

CAPABILITY_TAGS = ["form-filler", "assistant", "documents", "intake"]
CAPABILITIES = {
    "technical": [{"name": "pdf-forms", "type": "tool"}, {"name": "federated-dm", "type": "tool"}],
    "domain": ["assistant", "documents", "forms"],
    "languages": ["en", "fi"],
}

CHAT_COMMANDS = [
    {
        "id": "fill_form",
        "label": "Fill a form",
        "description": "Attach a PDF form and I'll complete it with you",
        "template": "Help me fill this form. {{notes}}",
        "params": [{"name": "notes", "type": "text", "required": False, "placeholder": "any context (optional)"}],
    }
]

_PENDING = "form_fill"


def _body_and_attachments(event: dict) -> tuple[str, list]:
    """The triggering message's body + its attachments (id-match in the thread, read-after-write safe)."""
    mid, conv, _sender, preview, _subject = dm._inbound_fields(event)
    msgs = []
    if conv:
        thread = dm.dm_thread(AGENT_NAME, conv)
        msgs = (thread.get("messages") if isinstance(thread, dict) else None) or []
    target = next((m for m in msgs if (m.get("id") or m.get("message_id")) == mid), None)
    body = (target.get("body") if target else None) or preview or ""
    atts = (target.get("attachments") if target else None) or event.get("attachments") or []
    return str(body), atts


def _first_pdf(attachments: list) -> dict | None:
    for a in attachments:
        mime = (a.get("mime") or a.get("mime_type") or "").lower()
        name = (a.get("name") or "").lower()
        if "pdf" in mime or name.endswith(".pdf"):
            return a
    return None


def _identify_fields(form_text: str, acroform: list[dict], llm) -> str:
    """LLM: from the form's text (and any AcroForm field names), list the fields the user must provide."""
    names = ", ".join(f["name"] for f in acroform) if acroform else "(none detected — read the text)"
    agent = Agent(
        role="Intake Interviewer",
        goal="Identify exactly what a form asks the applicant to provide, as a clear checklist.",
        backstory="You read official forms and distil them into the precise list of inputs the person must give.",
        llm=llm,
        allow_delegation=False,
        verbose=False,
    )
    task = Task(
        description=(
            "Here is a form's extracted text:\n\n"
            f"-----\n{form_text[:8000]}\n-----\n\n"
            f"Detected fillable field names: {names}\n\n"
            "List, as a short markdown checklist, every field/question the applicant must fill in (label "
            "each clearly; group sensibly). Do NOT invent fields that aren't in the form. Keep it concise."
        ),
        expected_output="A concise markdown checklist of the fields the applicant must provide.",
        agent=agent,
    )
    return str(Crew(agents=[agent], tasks=[task], process=Process.sequential).kickoff())


def _complete(form_text: str, acroform: list[dict], answers: str, llm) -> tuple[dict, str]:
    """LLM: map the user's answers onto the form. Returns (values_for_acroform, completed_markdown_doc).
    The values dict is only used when the PDF is a fillable AcroForm; the doc is always produced."""
    names = ", ".join(f["name"] for f in acroform) if acroform else ""
    want_json = (
        f"FIRST, output a fenced ```json object mapping each AcroForm field name ({names}) to the value "
        "from the user's answers (omit fields you have no answer for). THEN output the document.\n\n"
        if acroform
        else ""
    )
    agent = Agent(
        role="Form Completion Assistant",
        goal="Complete a form accurately from the applicant's answers, never inventing facts.",
        backstory="You fill official forms precisely, mapping each answer to the right field and flagging gaps.",
        llm=llm,
        allow_delegation=False,
        verbose=False,
    )
    task = Task(
        description=(
            "Form text:\n\n"
            f"-----\n{form_text[:8000]}\n-----\n\n"
            "The applicant's answers:\n\n"
            f"-----\n{answers[:4000]}\n-----\n\n"
            f"{want_json}"
            "Produce a COMPLETED-ANSWERS document in markdown: a heading, then every field with the "
            "applicant's value filled in (write 'TO PROVIDE' for anything still missing). Use only facts "
            "from the answers — never invent values. End with a short list of anything still missing."
        ),
        expected_output="Optionally a ```json field map, then a completed-answers markdown document.",
        agent=agent,
    )
    out = str(Crew(agents=[agent], tasks=[task], process=Process.sequential).kickoff())
    values: dict = {}
    if acroform:
        m = re.search(r"```json\s*(\{.*?\})\s*```", out, re.DOTALL)
        if m:
            try:
                values = json.loads(m.group(1))
            except Exception:  # noqa: BLE001
                values = {}
        out = re.sub(r"```json\s*\{.*?\}\s*```", "", out, flags=re.DOTALL).strip()
    return (values if isinstance(values, dict) else {}), out


def build_domain(ctx: BuildContext):
    note = Agent(
        role="Form Filler",
        goal="Explain how to use the form-filler.",
        backstory="You guide the user to DM a PDF form.",
        llm=ctx.llm,
        allow_delegation=False,
        verbose=False,
    )
    msg = "Send me a PDF form by DM and I'll read its fields, ask you the values, and return it completed."
    return ([note], [Task(description=f"State exactly this: {msg}", expected_output=msg, agent=note)])


def run() -> None:
    _seen: set = set()

    def _responder(event: dict):
        _mid, conv, sender, _preview, _subject = dm._inbound_fields(event)
        # Ignore sibling agents (no agent<->agent loops); serve humans in-thread.
        if orchestrator.in_roster(orchestrator.list_node_agents(AGENT_NAME), sender):
            return ""
        body, attachments = _body_and_attachments(event)
        pdf = _first_pdf(attachments)

        # (1) A PDF arrived -> start a fill: read its fields and ask for the values.
        if pdf:
            got = storage.fetch_bytes(AGENT_NAME, pdf.get("storageKey") or pdf.get("storage_key") or "")
            if not got:
                return "I couldn't download that file — is it shared with me? Try re-attaching it."
            data, mime = got
            fields = form_filler.extract_fields(data)
            text = vision.extract_document_text(data, mime, pdf.get("name") or "form.pdf")
            try:
                checklist = _identify_fields(text, fields, get_llm(agent_name=AGENT_NAME))
            except Exception as exc:  # noqa: BLE001
                print(f"[{AGENT_NAME}] identify-fields failed: {exc!r}", file=sys.stderr)
                return "I couldn't read that form — is it a text PDF (not a scan)?"
            session_store.session_set(
                AGENT_NAME,
                conv,
                _PENDING,
                {
                    "storage_key": pdf.get("storageKey") or pdf.get("storage_key"),
                    "name": pdf.get("name") or "form.pdf",
                    "fillable": bool(fields),
                    "fields": [f["name"] for f in fields],
                },
            )
            kind = "a fillable PDF" if fields else "a flat form (I'll return a completed document)"
            return (
                f"I read your form ({kind}). It asks for:\n\n{checklist}\n\n"
                "**Reply with your values** (e.g. `Name: Jane Doe, Date: 2026-06-23, …`) and I'll complete it."
            )

        # (2) We're waiting on values and the user replied with them -> complete the form.
        pending = session_store.session_get(AGENT_NAME, conv, _PENDING)
        if pending and len(body.strip()) >= 3:
            got = storage.fetch_bytes(AGENT_NAME, pending["storage_key"])
            if not got:
                session_store.session_clear(AGENT_NAME, conv, _PENDING)
                return "I lost access to the original form — please re-attach it and we'll start over."
            data, mime = got
            text = vision.extract_document_text(data, mime, pending["name"])
            acroform = form_filler.extract_fields(data) if pending.get("fillable") else []
            try:
                values, doc = _complete(text, acroform, body, get_llm(agent_name=AGENT_NAME))
            except Exception as exc:  # noqa: BLE001
                print(f"[{AGENT_NAME}] complete failed: {exc!r}", file=sys.stderr)
                return "I hit an error completing the form — try sending the values again?"
            out_atts: list[dict] = []
            base = pending["name"].rsplit(".", 1)[0]
            if pending.get("fillable") and values:  # bonus: an actually-filled PDF
                filled = form_filler.fill_pdf(data, values)
                if filled:
                    att = dm.dm_attach_bytes(AGENT_NAME, filled, name=f"filled-{base}.pdf", mime="application/pdf")
                    if att:
                        out_atts.append(att)
            doc_att = dm.dm_attach_bytes(
                AGENT_NAME, doc.encode("utf-8"), name=f"completed-{base}.md", mime="text/markdown"
            )
            if doc_att:
                out_atts.append(doc_att)
            session_store.session_clear(AGENT_NAME, conv, _PENDING)
            head = "Here's your completed form" + (" (filled PDF + answers doc)." if len(out_atts) > 1 else ".")
            return {"text": head, "attachments": out_atts}

        # (3) Nothing attached, nothing pending.
        return "Send me a **PDF form** and I'll read its fields, ask you for the values, and return it completed. 📝"

    run_crew(
        CrewSpec(
            agent_name=AGENT_NAME,
            build_domain=build_domain,
            readme_md=README,
            temperature=0.2,
            listen_for=("tasks", "dms"),
            on_dm=lambda e: dm.handle_dm_event(AGENT_NAME, e, _responder, seen=_seen),
            tags=CAPABILITY_TAGS,
            capabilities=CAPABILITIES,
            chat_commands=CHAT_COMMANDS,
        )
    )


if __name__ == "__main__":
    run()

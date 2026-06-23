"""concierge: a conversational DM agent — web search, image moodboards, file fetch, image generation,
all handed back as a federated-inbox reply (text + links + attachments).

You DM it (federated "Postilaatikko"); it reads the thread, picks the right tool(s), and replies IN the
thread with markdown + any images/files attached. It combines capabilities proven across the fleet:
  - web search  -> crewaimeat.crew._web_tools (SearXNG/DDG/Tavily) — replies with links.
  - find images -> image_contract (_searxng_images/_download_image) — attaches them (moodboard style).
  - fetch a file-> a guarded URL download (SSRF-safe) + dm.dm_attach_bytes — attaches it.
  - generate an image -> seedream_gen.generate_image -> re-fetch -> dm.dm_attach_bytes — attaches it.
  - describe itself -> a deterministic capabilities tool + this README/offers.

Inbound is the daemon's native on_dm (aimeat-crewai>=0.8.1): a DM wake -> dm.handle_dm_event -> this
responder runs the crew -> dm_reply with the collected attachments. The first-contact gate still applies
(it only ever replies IN a thread). Tools append produced files to a per-message sink so the reply can
carry them.

Register + approve before running:
  npx aimeat@latest connect add --agent concierge --mode task-runner --url https://aimeat.io --owner <your-aimeat-account>
Run: uv run python crews/concierge_crew.py
"""

from __future__ import annotations

import ipaddress
import os
import socket
import sys
import urllib.parse

import requests
from crewai import Agent, Crew, Process, Task
from crewai.tools import tool

from crewaimeat import dm, image_contract, orchestrator, seedream_gen, session_store
from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.crew import _web_tools
from crewaimeat.llm import get_llm

AGENT_NAME = "concierge"

# ── Delegation directory: the fleet specialists the concierge can hand a request to (the SERVICE MESH).
# {agent_name: "use-when description"}. Only those whose daemon is LIVE are ever offered (orchestrator
# filters by last_seen), so a dead/unregistered entry is harmless — it's simply skipped. Keep the
# descriptions sharp: they ARE the router's menu. The concierge's OWN tools (web search, images, file
# fetch, image-gen) take priority; delegate only when a specialist clearly fits better.
SERVICE_DIRECTORY = {
    "finnish-corporate-researcher": (
        "Deep research on a FINNISH company — financials, registry (Y-tunnus/PRH), people, and sentiment, "
        "every fact with a source URL. Use for 'tell me about <Finnish company>' / due-diligence asks."
    ),
    "web-researcher": (
        "In-depth web research, market scans, and company research on ANY topic — returns a structured, "
        "sourced report. Use for substantial research questions that need more than a few links."
    ),
    "jingle-writer": (
        "Writes a short, catchy rhyming jingle (4-6 lines) for a product, brand, or campaign. "
        "Use for 'write a jingle for <X>'."
    ),
    "tagline-translator": (
        "Translates / localizes a marketing tagline or short slogan between languages while keeping the "
        "punch. Use for 'translate this tagline/slogan'."
    ),
}

CAPABILITIES_TEXT = (
    "I'm a **concierge** you can DM. I can:\n"
    "- **Search the web** and reply with the best links (title + url + a one-line why).\n"
    "- **Find images** for a vibe or topic and attach them moodboard-style (thumbnails in your inbox).\n"
    "- **Find a document** (a PDF form, application, report) on the web and attach it — if there are "
    "several good matches I'll show them as checkboxes so you can tick which ones I download.\n"
    "- **Fetch a file** from a public URL you give me and attach it.\n"
    "- **Generate an image** from a description and attach it.\n"
    "- **Delegate to a specialist** in my fleet when your request needs deep expertise (e.g. detailed "
    "research on a Finnish company, a jingle) — I hand it to the right agent and relay their answer back here.\n"
    "- If I'm unsure what you mean, I'll **ask you a quick multiple-choice question** to get it right.\n\n"
    'Just tell me what you want — e.g. "find 4 cosy cabin interiors", "find me a Business Finland funding '
    'application PDF", "search the latest on X and send links", or "make an image of a neon fox". I reply '
    "right here in this thread."
)

README = """[[FIGLET:slant]["Concierge"]]

A conversational agent you **DM**. It searches the web (returns links), finds images and attaches them
moodboard-style, **finds a document (a PDF form/application) on the web and attaches it**, fetches a file
from a URL, generates an image from a description, and **delegates to fleet specialists** (handing a
request to the right agent and relaying its reply back) — then replies right in the thread. Ask
**"what can you do?"** and it tells you.

**How to talk to me:** DM me a request — "find 4 cosy cabins", "find me a Business Finland funding
application PDF", "search latest on X + links", "make an image of a neon fox". I reply in-thread with
text, links, and attachments.
"""

CAPABILITY_TAGS = ["concierge", "chat", "web-search", "image-search", "moodboard", "file-fetch", "image-gen"]
CAPABILITIES = {
    "technical": [{"name": "federated-dm", "type": "messaging"}, {"name": "web-search", "type": "research"}],
    "domain": ["assistant", "concierge", "consumes:dm@1"],
    "languages": ["en", "fi"],
}

# Guards for the file-fetch tool.
_FETCH_MAX_BYTES = 25 * 1024 * 1024  # 25 MB cap
_MAX_IMAGES = 8


def _is_safe_url(url: str) -> bool:
    """SSRF guard: http(s) only, and the host must NOT resolve to a private / loopback / link-local IP."""
    try:
        p = urllib.parse.urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname:
            return False
        for info in socket.getaddrinfo(p.hostname, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                return False
        return True
    except Exception:  # noqa: BLE001
        return False


def _fetch_url_bytes(url: str, *, max_bytes: int = _FETCH_MAX_BYTES):
    """Download a public URL with guards (scheme, public host, size cap). Returns (data, mime, name) or None."""
    if not _is_safe_url(url):
        return None
    try:
        with requests.get(url, stream=True, timeout=60, headers={"User-Agent": "crewaimeat-concierge"}) as r:
            if r.status_code != 200:
                return None
            mime = (r.headers.get("Content-Type") or "application/octet-stream").split(";")[0].strip()
            if int(r.headers.get("Content-Length") or 0) > max_bytes:
                return None
            chunks, total = [], 0
            for chunk in r.iter_content(64 * 1024):
                total += len(chunk)
                if total > max_bytes:
                    return None
                chunks.append(chunk)
        name = os.path.basename(urllib.parse.urlparse(url).path) or "download"
        return b"".join(chunks), mime, name
    except Exception:  # noqa: BLE001
        return None


def _searxng_web(query: str, n: int = 15) -> list[dict]:
    """SearXNG general web search -> [{url, title}]. Used by find_file to locate a downloadable document."""
    base = os.getenv("SEARXNG_URL", "http://localhost:21333").rstrip("/")
    try:
        r = requests.get(base + "/search", params={"q": query, "format": "json"}, timeout=20)
        out = []
        for it in r.json().get("results") or []:
            u = it.get("url") or ""
            if u.startswith("http"):
                out.append({"url": u, "title": it.get("title") or ""})
            if len(out) >= n:
                break
        return out
    except Exception:  # noqa: BLE001
        return []


def _concierge_tools(sink: dict, *, ask_to: str | None = None, ask_conv: str | None = None) -> list:
    """The toolset, bound to a per-message `sink` (sink["attachments"] collects files for the reply; for a
    DM, ask_to/ask_conv enable the clarify tool — it asks the user a structured question and sets
    sink["asked"], so the responder sends the FORM instead of a normal reply)."""

    @tool("find_images")
    def find_images(query: str, count: int = 4) -> str:
        """Find up to `count` images on the open web for `query` and ATTACH them to the reply (moodboard)."""
        n = 0
        want = min(max(int(count or 4), 1), _MAX_IMAGES)
        for hit in image_contract._searxng_images(query, want * 3):
            if n >= want:
                break
            dl = image_contract._download_image(hit.get("img_src", ""))  # img_src = the image; url = source page
            if not dl:
                continue
            data, mime = dl
            ext = (mime.split("/")[-1] or "jpg").split("+")[0]
            att = dm.dm_attach_bytes(AGENT_NAME, data, name=f"img-{n + 1}.{ext}", mime=mime)
            if att:
                sink["attachments"].append(att)
                n += 1
        return f"Attached {n} image(s) for '{query}'." if n else f"No usable images found for '{query}'."

    @tool("fetch_file")
    def fetch_file(url: str) -> str:
        """Download a file from a public URL (guarded) and ATTACH it to the reply."""
        got = _fetch_url_bytes(url)
        if not got:
            return f"Could not fetch '{url}' (blocked host, too large, or unreachable)."
        data, mime, name = got
        att = dm.dm_attach_bytes(AGENT_NAME, data, name=name, mime=mime)
        if not att:
            return "Upload failed."
        sink["attachments"].append(att)
        return f"Attached '{name}' ({mime}, {len(data)} bytes)."

    @tool("find_file")
    def find_file(query: str, filetype: str = "pdf") -> str:
        """Search the web for a downloadable DOCUMENT (e.g. a PDF form/application) matching `query`, download
        the first one that works, and ATTACH it. Use for 'find me a <kind> form/document/pdf' requests (this
        is search+download in one; fetch_file is only for a URL the user already gave)."""
        ext = (filetype or "pdf").lstrip(".").lower()
        results = _searxng_web(f"{query} filetype:{ext}", 15) or _searxng_web(query, 15)
        direct = [r["url"] for r in results if r["url"].split("?")[0].lower().endswith(f".{ext}")]
        tried = 0
        for url in [*direct, *[r["url"] for r in results]]:
            if tried >= 6:
                break
            tried += 1
            got = _fetch_url_bytes(url)
            if not got:
                continue
            data, mime, name = got
            if ext not in mime.lower() and not url.split("?")[0].lower().endswith(f".{ext}"):
                continue  # it's a page, not the file — keep looking
            if not name.lower().endswith(f".{ext}"):
                name = f"{(name or 'document').rsplit('.', 1)[0]}.{ext}"
            att = dm.dm_attach_bytes(AGENT_NAME, data, name=name, mime=mime)
            if att:
                sink["attachments"].append(att)
                return f"Attached '{name}' from {url} ({mime}, {len(data)} bytes)."
        pages = "; ".join(f"{r['title']} — {r['url']}" for r in results[:4])
        return (
            f"Couldn't download a .{ext} for '{query}'. Closest pages: {pages}"
            if pages
            else f"No results for '{query}'."
        )

    @tool("generate_image")
    def generate_image(description: str) -> str:
        """Generate an image from `description` (Seedream) and ATTACH it to the reply."""
        res = seedream_gen.generate_image(AGENT_NAME, description)
        if not res.get("ok"):
            return f"Generation failed: {res.get('error')}"
        got = _fetch_url_bytes(res["url"])
        if not got:
            return f"Generated — link: {res['url']}"
        data, mime, _name = got
        ext = (mime.split("/")[-1] or "png").split("+")[0]
        att = dm.dm_attach_bytes(AGENT_NAME, data, name=f"generated.{ext}", mime=mime)
        if att:
            sink["attachments"].append(att)
        return "Attached a generated image."

    @tool("describe_capabilities")
    def describe_capabilities() -> str:
        """Explain what I can do and what I can return to the user."""
        return CAPABILITIES_TEXT

    tools = [*_web_tools(), find_images, fetch_file, find_file, generate_image, describe_capabilities]

    if ask_to and ask_conv:

        @tool("offer_documents")
        def offer_documents(query: str, filetype: str = "pdf") -> str:
            """Find documents on the web for `query`. If there are SEVERAL good matches I AUTOMATICALLY ask
            the user (checkboxes) which to download and deliver exactly those; if there's only ONE I just
            attach it. This is the DEFAULT for any 'find me a <kind> document/form/PDF' request — it never
            blindly grabs the wrong one. If I ask, STOP and wait for their pick."""
            ext = (filetype or "pdf").lstrip(".").lower()
            results = _searxng_web(f"{query} filetype:{ext}", 18) or _searxng_web(query, 18)
            seen: set = set()
            direct, other = [], []  # direct .ext links first — they're the actual files, not landing pages
            for r in results:
                u = r["url"]
                if u in seen:
                    continue
                seen.add(u)
                item = {"label": (r.get("title") or u)[:70], "url": u}
                (direct if u.split("?")[0].lower().endswith(f".{ext}") else other).append(item)
            ordered = (direct + other)[:8]
            cands = [{"id": f"d{i}", **it} for i, it in enumerate(ordered)]
            if not cands:
                return f"No documents found for '{query}'."
            if len(cands) == 1:  # nothing to choose — just deliver it
                c = cands[0]
                got = _fetch_url_bytes(c["url"])
                if not got:
                    return f"Found one ({c['label']}) but couldn't download it: {c['url']}"
                data, mime, name = got
                if not name.lower().endswith(f".{ext}"):
                    name = f"{(name or 'document').rsplit('.', 1)[0]}.{ext}"
                att = dm.dm_attach_bytes(AGENT_NAME, data, name=name, mime=mime)
                if att:
                    sink["attachments"].append(att)
                return f"Attached '{name}' ({c['label']})."
            session_store.session_set(AGENT_NAME, ask_conv, "doc_candidates", {"ext": ext, "items": cands})
            q = dm.build_question(
                "pick_docs",
                "Pick documents",
                f"I found {len(cands)} documents for '{query}'. Which should I download?",
                [(c["id"], c["label"]) for c in cands],
                multi_select=True,
                allow_other=False,
            )
            res = dm.dm_ask(
                AGENT_NAME,
                ask_to,
                [q],
                body=f"I found {len(cands)} documents for '{query}'. Tick the ones you want and I'll attach them:",
                conversation_id=ask_conv,
            )
            sink["asked"] = bool(res)
            return f"Found {len(cands)} and asked the user to pick." if res else "Couldn't send the picker."

        tools.append(offer_documents)

        @tool("ask_user")
        def ask_user(question: str, options: str, multi_select: bool = False) -> str:
            """Ask the user ONE clarifying multiple-choice question, ONLY when the request is genuinely
            ambiguous and a wrong guess would waste effort (don't over-use it). `options` = 2-5 short
            choices separated by '|'. It renders as a tappable form in their inbox; you'll get their answer
            as a follow-up. After calling this, STOP — do NOT also write a reply; just wait for the answer."""
            opts = [o.strip() for o in (options or "").split("|") if o.strip()][:5]
            if len(opts) < 2:
                return "Provide at least 2 options separated by '|'."
            q = dm.build_question("clarify", question[:60], question, opts, multi_select=multi_select)
            res = dm.dm_ask(AGENT_NAME, ask_to, [q], body=question, conversation_id=ask_conv)
            sink["asked"] = bool(res)
            return "Asked the user; waiting for their answer." if res else "Could not send the question."

        tools.append(ask_user)

        @tool("delegate_to_specialist")
        def delegate_to_specialist(specialist: str, request: str) -> str:
            """Hand the request to a fleet SPECIALIST (see the 'Specialists you can delegate to' menu in
            your task) and relay their reply back to the user when it's ready. Use this ONLY when a
            specialist clearly fits the request better than your own tools (e.g. deep company research, a
            jingle). `specialist` = the EXACT agent name from the menu; `request` = a complete, standalone
            brief for them (they don't see this chat). After calling this, STOP — do NOT also answer; the
            user gets a short 'on it' note now and the specialist's reply is relayed automatically later."""
            live = {s["name"]: s for s in orchestrator.live_services(AGENT_NAME, SERVICE_DIRECTORY)}
            s = live.get(specialist)
            if not s:
                avail = ", ".join(live) or "none right now"
                return f"'{specialist}' isn't an available specialist. Available: {avail}."
            conv = orchestrator.delegate(AGENT_NAME, s["gaii"], request)
            if not conv:
                return f"Couldn't reach {specialist} just now."
            orchestrator.record_delegation(
                AGENT_NAME, conv, user_to=ask_to, user_conv=ask_conv, specialist=specialist, request=request
            )
            sink["delegated"] = specialist
            return f"Delegated to {specialist}; their reply will be relayed to the user."

        tools.append(delegate_to_specialist)

    return tools


def _agent(llm, sink: dict, *, ask_to: str | None = None, ask_conv: str | None = None) -> Agent:
    return Agent(
        role="Concierge",
        goal="Understand the user's request and fulfil it with the right tool(s), then reply concisely.",
        backstory=(
            "You are a friendly, capable concierge reached over direct message. You search the web and "
            "return the best links, find images and attach them, find a document/PDF on the web and attach "
            "it, fetch a file from a URL, and generate an image from a description. When the user asks what "
            "you can do, you call describe_capabilities. If a request is genuinely ambiguous (a wrong guess "
            "would waste effort), you ask ONE structured clarifying question with ask_user instead of "
            "guessing. You keep replies concise and always cite source links for web results."
        ),
        llm=llm,
        tools=_concierge_tools(sink, ask_to=ask_to, ask_conv=ask_conv),
        allow_delegation=False,
        verbose=False,
    )


def _task(request: str, context: str, agent: Agent, today: str, directory: str = "") -> Task:
    delegation = (
        (
            "\n\nSpecialists you can delegate to (use delegate_to_specialist with the EXACT name) when one "
            "fits FAR better than your own tools — otherwise just answer yourself:\n"
            f"{directory}\n"
        )
        if directory
        else ""
    )
    return Task(
        description=(
            f"Today is {today}. The user sent this direct message:\n\n{request}\n\n"
            f"Recent conversation (for context):\n{context or '(none)'}\n"
            f"{delegation}\n"
            "Decide what they want and do it with your tools. Use find_images to FIND existing images on the "
            "web (a 'find / show me' request) and generate_image ONLY to CREATE a new image from a "
            "description (a 'make / generate / draw' request) — never substitute one for the other. To find a "
            "DOCUMENT/form/PDF on the web, use offer_documents by DEFAULT — it searches and, if several "
            "match, AUTOMATICALLY lets the user tick which to download (delivering exactly those); if only "
            "one matches it just attaches it. (Use find_file only if the user clearly wants you to grab a "
            "single best one without choosing.) fetch_file is only for a URL the user already gave. If they "
            "ask what you can do (or it's a vague greeting), call describe_capabilities. Attach images/files "
            "with the tools and mention what you attached. If the request is genuinely ambiguous (a wrong "
            "guess would waste effort), call ask_user with 2-5 options to clarify FIRST, then STOP and wait. "
            "Reply concisely in markdown; cite source links for any web results. Answer ONLY the message "
            "above — ignore earlier topics unless asked to continue."
        ),
        expected_output="A concise, friendly markdown reply. Attachments are added by the tools.",
        agent=agent,
    )


def build_domain(ctx: BuildContext):
    # Task path (an assigned task rather than a DM): same crew; attachments aren't delivered (no thread to
    # reply to), so the reply carries links/text. The DM path (run() below) collects + delivers attachments.
    sink: dict = {"attachments": []}
    agent = _agent(ctx.llm, sink)
    return ([agent], [_task(ctx.prompt, "", agent, ctx.today)])


def _dm_request_and_context(event: dict) -> tuple[str, str]:
    """THIS event's message (full body) + a short prior-context. The request is the TRIGGERING message —
    matched by the event id in the thread, else the wake's own preview. NOT inbound[-1]: the thread read
    can lag behind the just-arrived DM (read-after-write), which would make us answer the PREVIOUS one."""
    mid, conv, _sender, preview, _subject = dm._inbound_fields(event)
    msgs = []
    if conv:
        thread = dm.dm_thread(AGENT_NAME, conv)
        msgs = (thread.get("messages") if isinstance(thread, dict) else None) or []

    def _mid(m):
        return m.get("id") or m.get("message_id")

    target = next((m for m in msgs if _mid(m) == mid), None)
    request = (target.get("body") if target else None) or preview or "(empty message)"
    # context = messages strictly BEFORE this one, so the current request never leaks into "context"
    prior: list = []
    for m in msgs:
        if _mid(m) == mid:
            break
        prior.append(m)
    ctx_lines = [
        f"{'me' if m.get('direction') == 'outbound' else 'user'}: {str(m.get('body') or '')[:300]}" for m in prior[-6:]
    ]
    return str(request), "\n".join(ctx_lines)


def _deliver_picked_docs(conv: str, picks: dict):
    """If the conversation has documents we OFFERED (offer_documents) and the user ticked some, download +
    attach exactly those — deterministic, no LLM, no re-search (the URLs were remembered in session_store).
    Returns {"text","attachments"} or None when nothing is pending / nothing was picked."""
    pending = session_store.session_get(AGENT_NAME, conv, "doc_candidates")
    pick = (picks.get("pick_docs") or {}) if isinstance(picks, dict) else {}
    chosen = set(pick.get("selected") or [])
    if not pending or not chosen:
        return None
    ext = pending.get("ext", "pdf")
    attached: list[dict] = []
    lines: list[str] = []
    for c in [c for c in pending.get("items", []) if c["id"] in chosen]:
        got = _fetch_url_bytes(c["url"])
        if not got:
            lines.append(f"- {c['label']} — couldn't download")
            continue
        data, mime, name = got
        if ext not in mime.lower() and not c["url"].split("?")[0].lower().endswith(f".{ext}"):
            lines.append(f"- {c['label']} — not a {ext} file (skipped)")  # a landing page, not the doc
            continue
        if not name.lower().endswith(f".{ext}"):
            name = f"{(name or 'document').rsplit('.', 1)[0]}.{ext}"
        att = dm.dm_attach_bytes(AGENT_NAME, data, name=name, mime=mime)
        if att:
            attached.append(att)
            lines.append(f"- {c['label']}")
    session_store.session_clear(AGENT_NAME, conv, "doc_candidates")
    head = "Here are the documents you picked:" if attached else "Sorry, I couldn't download the picked documents:"
    return {"text": head + "\n" + "\n".join(lines), "attachments": attached[:20]}


def run() -> None:
    _seen: set = set()

    def _dm_responder(event: dict):
        _mid, conv, sender, _preview, _subject = dm._inbound_fields(event)
        request, context = _dm_request_and_context(event)
        # (A) Is this the reply to a request we DELEGATED to a specialist? Relay it to the original user and
        # stop (we don't reply to the specialist — that would just bounce back). Cheap: a session lookup.
        pending = orchestrator.match_delegation(AGENT_NAME, conv, sender)
        if pending:
            relay = f"**{pending['specialist']}** got back to me:\n\n{request}"
            dm.dm_reply(AGENT_NAME, pending["user_to"], relay, conversation_id=pending["user_conv"])
            return ""
        # (B) If this DM is the ANSWER to a clarifying question we asked, fold the structured picks into the
        # request and fulfil the ORIGINAL ask (which is in the thread context).
        if event.get("interactive") == "answers":
            picks = dm.dm_answers_from_event(AGENT_NAME, event)  # event-aware: THIS answer, not the latest
            # Did they pick from documents we offered? Deliver exactly those from the session store (no LLM).
            delivered = _deliver_picked_docs(conv, picks) if conv else None
            if delivered is not None:
                return delivered
            clar = "; ".join(
                f"{qid}={','.join(v.get('selected') or [])}" + (f" (other: {v['other']})" if v.get("other") else "")
                for qid, v in picks.items()
            )
            request = (
                f"The user answered your clarifying question(s): {clar or '(no picks)'}.\n\n"
                "Now fulfil their ORIGINAL request (above, in the conversation context) using these answers."
            )
        # Fetch the roster ONCE — it serves both the agent<->agent loop-guard and the delegation menu.
        roster = orchestrator.list_node_agents(AGENT_NAME)
        # (C) A DM from a SIBLING agent that ISN'T a tracked delegation -> stay silent (no crew, no reply):
        # otherwise two dm_serviceable crews would loop on each other's replies forever.
        if orchestrator.in_roster(roster, sender):
            return ""
        menu = orchestrator.directory_text(orchestrator.services_from_roster(roster, SERVICE_DIRECTORY))
        sink: dict = {"attachments": [], "asked": False}
        agent = _agent(get_llm(agent_name=AGENT_NAME), sink, ask_to=sender, ask_conv=conv)
        crew = Crew(agents=[agent], tasks=[_task(request, context, agent, "", menu)], process=Process.sequential)
        try:
            result = crew.kickoff()
        except Exception as exc:  # noqa: BLE001
            print(f"[{AGENT_NAME}] concierge crew failed: {exc!r}", file=sys.stderr)
            return "Sorry — I hit an error handling that. Try rephrasing?"
        if sink.get("asked"):
            return ""  # the clarifying FORM was already sent as the message — don't also send a reply
        if sink.get("delegated"):  # handed to a specialist — ack now; their reply is relayed later (branch A)
            return (
                f"On it — I've asked **{sink['delegated']}** to handle that. "
                "I'll relay their reply here as soon as it's ready."
            )
        return {"text": str(result), "attachments": sink["attachments"][:20]}

    run_crew(
        CrewSpec(
            agent_name=AGENT_NAME,
            build_domain=build_domain,
            readme_md=README,
            adapt_to_task=True,
            listen_for=("tasks", "dms"),
            on_dm=lambda e: dm.handle_dm_event(AGENT_NAME, e, _dm_responder, seen=_seen),
            tags=CAPABILITY_TAGS,
            capabilities=CAPABILITIES,
        )
    )


if __name__ == "__main__":
    run()

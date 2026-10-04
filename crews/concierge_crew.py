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
  npx aimeat@latest connect --url https://aimeat.io --owner <your-aimeat-account> --agent concierge
Run: uv run python crews/concierge_crew.py
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import sys
import urllib.parse

import requests
from crewai import Agent, Crew, Process, Task
from crewai.tools import tool

from crewaimeat import (
    concierge_propose,
    dm,
    hitl,
    image_contract,
    orchestrator,
    seedream_gen,
    session_store,
    vision,
    workspace_tools,
)
from crewaimeat.aimeat_crew import BuildContext, CrewSpec, _aimeat_call, _valid_chat_commands, run_crew
from crewaimeat.crew import _web_tools
from crewaimeat.decline import make_decline_tool
from crewaimeat.llm import get_llm

AGENT_NAME = "concierge"

# ── This agent's own declaration ─────────────────────────────────────────────────────────────
# The single source for what this agent is: its model routing, how it is discovered, and what it
# promises. These used to live in three central lists (fleet_identity.py / llm_providers.json /
# offers.py) that nothing kept in step, so an agent could — and did — come online missing from
# all of them. crewaimeat.agent_manifest reads these statically; the lists are derived.
LLM_PROFILE = "coding"


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
    "- **Read a file or image you attach** — I run images through vision and pull text out of PDFs/docs, "
    "then summarise or answer questions about them.\n"
    "- **Delegate to a specialist** in my fleet when your request needs deep expertise (e.g. detailed "
    "research on a Finnish company, a jingle) — I hand it to the right agent and relay their answer back here.\n"
    "- **Propose a new agent** for a job you want done, or done regularly — you approve it on your Agents "
    "page and it runs here. I look at your own workspaces first, so it works on your data.\n"
    "- **Save a request you use a lot as a command** — ask me to 'save that as a command' and, once you "
    "approve it, it becomes a one-click chip in your composer.\n"
    "- If I'm unsure what you mean, I'll **ask you a quick multiple-choice question** to get it right.\n\n"
    'Just tell me what you want — e.g. "find 4 cosy cabin interiors", "find me a Business Finland funding '
    'application PDF", "search the latest on X and send links", or "make an image of a neon fox". I reply '
    "right here in this thread."
)

README = """[[FIGLET:slant]["Concierge"]]

A conversational agent you **DM**. It searches the web (returns links), finds images and attaches them
moodboard-style, **finds a document (a PDF form/application) on the web and attaches it**, fetches a file
from a URL, generates an image from a description, and **delegates to fleet specialists** (handing a
request to the right agent and relaying its reply back) — then replies right in the thread. I can propose a new agent for you; you approve it on your Agents page and it runs here. Ask
**"what can you do?"** and it tells you.

**How to talk to me:** DM me a request — "find 4 cosy cabins", "find me a Business Finland funding
application PDF", "search latest on X + links", "make an image of a neon fox". I reply in-thread with
text, links, and attachments.
"""

CAPABILITY_TAGS = [
    "concierge",
    "chat",
    "vision",
    "web-search",
    "image-search",
    "moodboard",
    "file-analysis",
    "image-gen",
    "delegation",
    "agent-proposals",
]
CAPABILITIES = {
    # NB technical[].type MUST be one of mcp|skill|tool — the node REJECTS anything else (e.g. "messaging"/
    # "research" → INVALID_INPUT, which silently drops the whole report and the dashboard shows generic defaults).
    "technical": [
        {"name": "vision", "type": "skill"},  # sees what's in an attached image (qwen-2.5-vl)
        {"name": "web-search", "type": "skill"},
        {"name": "image-search", "type": "skill"},  # moodboards
        {"name": "image-generation", "type": "skill"},
        {"name": "file-analysis", "type": "skill"},  # reads attached PDFs/docs
        {"name": "federated-dm", "type": "skill"},
        {"name": "delegation", "type": "skill"},  # routes to fleet specialists
        {"name": "agent-proposals", "type": "tool"},  # proposes a new agent on the node; the owner approves
    ],
    "domain": [
        "assistant",
        "concierge",
        # vision MODALITY: on images & documents the USER SENDS it (provided content) — describe / extract.
        "vision over images & documents the USER sends it (provided content) — describe / OCR / extract",
        "consumes:dm@1",
    ],
    "languages": ["en", "fi"],
}


# What this agent advertises it can do. The `ask` states NEGATIVE SCOPE on purpose — what it
# will NOT do is the half a buyer needs and the half an author skips.
OFFERS = [
    {
        "id": "concierge-chat",
        "title": "A conversational assistant you DM",
        "ask": "DM me anything: I search the live web and return links, build a moodboard, read a file you "
        "attach, generate an image, or describe one you send. I can propose a new agent for you; you approve it on your Agents page and it runs here. I am a conversation, NOT a task-runner — "
        "I do not run scheduled jobs, and I do not act on the node without being asked.",
        "example": "«etsi kolme lähdettä EU:n AI-asetuksen läpinäkyvyysvelvoitteista ja tiivistä ne»",
        "cost": "cheap",
        "latency": "seconds",
        "repeatability": "accumulative",
        "verification": "ungated",
        "consequences": [{"type": "publishes-public", "note": "a generated image is stored at a public storage URL"}],
        "sample": (
            '**«etsi kolme lähdettä EU:n AI-asetuksen läpinäkyvyysvelvoitteista»**\n\n1. [EUR-Lex 2024/1689, Art. 50](https://eur-lex.europa.eu/eli/reg/2024/1689) — merkintä- ja ilmoitusvelvoitteet, sovelletaan 2.8.2026\n2. [Komission AI Act -FAQ](https://digital-strategy.ec.europa.eu) — mitä "limited risk" käytännössä tarkoittaa\n3. …\n\nHaluatko että syvennyn johonkin näistä?\n\n…'
        ),
    },
]

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
    return _download(url, max_bytes=max_bytes)


def _download(url: str, *, max_bytes: int = _FETCH_MAX_BYTES):
    """The download itself, size-capped. Called through `_fetch_url_bytes` for any address a person or a
    model gave; called directly only for an address THIS crew built for its own node (`fetch_url` from
    seedream_gen), which on a hosted place is loopback and would rightly fail the SSRF guard."""
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


# THE TASK PATH HAS NO ATTACHMENTS. A task's deliverable is text on the person's task list; nothing a
# tool attaches reaches it (build_domain: "no thread to reply to"). Measured 2026-10-04 on a sold place:
# asked as a TASK for an image "and its address at the end", the concierge answered "Kuvan osoite: (the
# generated image is attached to this message -- it is not at a public URL)" -- no link, no attachment,
# nothing, while the picture sat in public storage and opened from outside. So on the task path every
# file goes out as its PUBLIC link (crewaimeat.public_url for our own; the source address for one found
# on the web), recorded per task here so the publish step can put any link the reply left out into it.
_TASK_LINKS: dict[str, list[dict]] = {}


def _give(sink: dict, *, name: str, mime: str = "", data: bytes | None = None, link: str | None = None) -> str:
    """Deliver one file and say truthfully what happened.

    DM path: attach it to the reply; when the attach fails, the link instead. Task path
    (`sink["links_only"]`): never attach -- record the public link and tell the model to give it."""
    if sink.get("links_only"):
        if not link:
            return f"'{name}' cannot be delivered here: this reply goes on a task list and it has no public link."
        sink.setdefault("links", []).append({"name": name, "link": link})
        return (
            f"NOT attached -- this reply goes on a task list, where nothing can be attached. "
            f"Give the person this link to '{name}': {link}"
        )
    att = dm.dm_attach_bytes(AGENT_NAME, data, name=name, mime=mime) if data is not None else None
    if att:
        sink["attachments"].append(att)
        return f"Attached '{name}'" + (f" ({mime}, {len(data)} bytes)." if data is not None else ".")
    if link:
        return f"Could not attach '{name}'; give the person this link instead: {link}"
    return f"Could not attach '{name}', and it has no public link."


def _task_id_now() -> str | None:
    """The AIMEAT task of the kickoff on this context (the ledger's run id, then the progress bridge's)."""
    try:
        from crewaimeat.ledger_report import _resolve_run_id

        tid = _resolve_run_id()
        if tid:
            return str(tid)
    except Exception:  # noqa: BLE001
        pass
    try:
        from crewaimeat import progress

        tid = progress._CURRENT_TASK.get()
        if tid:
            return str(tid)
    except Exception:  # noqa: BLE001
        pass
    return next(iter(_TASK_LINKS)) if len(_TASK_LINKS) == 1 else None


def _links_into_reply(text: str) -> str:
    """The publish step's cleaner: every public link a tool handed out on this task is IN the reply.
    A link the model already wrote stays where it put it; any it left out is added at the end."""
    tid = _task_id_now()
    links = _TASK_LINKS.pop(tid, None) if tid else None
    missing = [x for x in (links or []) if x["link"] not in (text or "")]
    if not missing:
        return text
    return (text or "").rstrip() + "\n\n" + "\n".join(f"- {x['name']}: {x['link']}" for x in missing) + "\n"


def _concierge_tools(sink: dict, *, ask_to: str | None = None, ask_conv: str | None = None) -> list:
    """The toolset, bound to a per-message `sink` (sink["attachments"] collects files for the reply; for a
    DM, ask_to/ask_conv enable the clarify tool — it asks the user a structured question and sets
    sink["asked"], so the responder sends the FORM instead of a normal reply)."""

    @tool("find_images")
    def find_images(query: str, count: int = 4) -> str:
        """Find up to `count` images on the open web for `query` and deliver them (moodboard): attached
        in a chat, as their links on a task."""
        n = 0
        said: list[str] = []
        want = min(max(int(count or 4), 1), _MAX_IMAGES)
        for hit in image_contract._searxng_images(query, want * 3):
            if n >= want:
                break
            src = hit.get("img_src", "")  # img_src = the image; url = source page
            dl = image_contract._download_image(src)
            if not dl:
                continue
            data, mime = dl
            ext = (mime.split("/")[-1] or "jpg").split("+")[0]
            out = _give(sink, name=f"img-{n + 1}.{ext}", mime=mime, data=data, link=src or None)
            if not out.startswith(("Could not", "'")):
                n += 1
                said.append(out)
        return "\n".join(said) if n else f"No usable images found for '{query}'."

    @tool("fetch_file")
    def fetch_file(url: str) -> str:
        """Download a file from a public URL (guarded) and deliver it: attached in a chat, as its link on a task."""
        got = _fetch_url_bytes(url)
        if not got:
            return f"Could not fetch '{url}' (blocked host, too large, or unreachable)."
        data, mime, name = got
        return _give(sink, name=name, mime=mime, data=data, link=url)

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
            out = _give(sink, name=name, mime=mime, data=data, link=url)
            if not out.startswith("Could not attach") or "link instead" in out:
                return f"{out} (found at {url})"
        pages = "; ".join(f"{r['title']} — {r['url']}" for r in results[:4])
        return (
            f"Couldn't download a .{ext} for '{query}'. Closest pages: {pages}"
            if pages
            else f"No results for '{query}'."
        )

    @tool("generate_image")
    def generate_image(description: str) -> str:
        """Generate an image from `description` and deliver it: attached in a chat, as its public link on a task."""
        res = seedream_gen.generate_image(AGENT_NAME, description)
        if not res.get("ok"):
            return f"Generation failed: {res.get('error')}"
        if sink.get("links_only"):  # nothing to attach on a task: no need to fetch the bytes either
            return _give(sink, name="generated image", mime=res.get("mime") or "", link=res.get("url"))
        # The bytes come through the crew's OWN address for its node (`fetch_url`); the person only ever
        # sees `url`, the place's public address. On a sold place (2026-10-03) the fetch went to the
        # loopback address, the SSRF guard refused it, and the customer got "http://127.0.0.1:40050/...".
        got = _download(res["fetch_url"]) if res.get("fetch_url") else _fetch_url_bytes(res["url"])
        if not got:
            return f"Generated — link: {res['url']}"
        data, mime, _name = got
        ext = (mime.split("/")[-1] or "png").split("+")[0]
        return _give(sink, name=f"generated.{ext}", mime=mime, data=data, link=res.get("url"))

    @tool("describe_capabilities")
    def describe_capabilities() -> str:
        """Explain what I can do and what I can return to the user."""
        return CAPABILITIES_TEXT

    # ── A new agent is made HERE, on the person's own node -- on BOTH paths (a DM and an assigned task).
    # Asked for "an agent that every morning gathers the CRM's open deals", this concierge used to search
    # the web and recommend CrewAI Studio and HubSpot (2026-10-02), because web search was all it had. The
    # node makes, runs and credentials the agent itself in one owner press; these three tools are the road.
    @tool("look_at_my_workspaces")
    def look_at_my_workspaces(name: str = "") -> str:
        """List the organisms and workspaces the person keeps on this node, by name. Give `name` (e.g.
        'CADENCE', 'CRM') to also see that workspace's index: its spaces and record titles. Call this
        FIRST whenever a request is about the person's own data or about a new agent, so you name THEIR
        data instead of an outside product."""
        found = workspace_tools.list_workspaces(AGENT_NAME)
        text = workspace_tools.render_workspaces(found)
        if not name:
            return text
        hit = workspace_tools.find_workspace(found, name)
        if hit is None:
            return f"No workspace matches '{name}'.\n\n{text}"
        index = workspace_tools.workspace_index(AGENT_NAME, hit["organism_id"], hit["ws"])
        shown = json.dumps(index, ensure_ascii=False) if index is not None else "(empty, or not readable for me)"
        return f"{text}\n\nThe workspace {hit['name']}:\n{shown}"

    @tool("propose_agent")
    def propose_agent(
        name: str,
        display_name: str,
        purpose: str,
        instructions: str,
        workspace: str = "",
        tools: str = "",
        delivers: str = "",
        schedule_cron: str = "",
        timezone: str = "Europe/Helsinki",
    ) -> str:
        """Propose a NEW AGENT on this node for the person to approve. Use it whenever they ask for a new
        agent, a helper for one job, or work that should happen regularly ('every morning...', 'keep an eye
        on...'). Look at their workspaces first (look_at_my_workspaces) so it works on THEIR data. Call it
        in the SAME run as the request, even on a node with no workspace yet (leave `workspace` empty: the
        agent reads memory until one exists) -- state your assumptions beside the proposal and ask the
        person to correct them; never ask for the details instead of proposing.
        `name`: lowercase-with-hyphens, 3-40 chars, e.g. 'morning-deals'. `display_name`: what they see.
        `purpose`: one sentence naming their data, e.g. 'Reads the open deals in CADENCE every morning and
        names the ones to act on today'. `instructions`: what the agent does on each run, in plain words.
        `workspace`: the workspace it works on (its name, e.g. 'CADENCE'). `tools`: comma-separated, only
        from memory, workspace, workspace_write, web, article_fetch, schedule, dm. A named workspace adds
        'workspace' (read only) itself; give 'workspace_write' when the agent must ADD or CHANGE records
        there (contacts, deals, tasks), which also asks the owner for organism:write. Name only the tools
        each RUN calls: every tool asks the owner for permissions. 'schedule' is for an agent that manages
        schedules itself; a job that runs on a clock does NOT need it -- give `schedule_cron` instead, and
        I set the clock after the approval.
        `delivers`: what each run hands back. `schedule_cron`: a 5-field cron when it should run on a clock
        ('0 7 * * *' = 07:00 daily), with `timezone`. Then relay EXACTLY what this returns -- it carries the
        address where they approve. Do not ask them yes/no as well: their press there is the approval."""
        return concierge_propose.propose(
            AGENT_NAME,
            name=name,
            display_name=display_name,
            purpose=purpose,
            instructions=instructions,
            tools=tools,
            workspace=workspace,
            delivers=delivers,
            schedule_cron=schedule_cron,
            timezone=timezone,
        )

    @tool("start_proposed_agent")
    def start_proposed_agent(name: str) -> str:
        """After the person has APPROVED an agent you proposed with a schedule and says 'start it' / 'start
        <name>': set it to run on the schedule you proposed. It checks the agent exists first and refuses
        if it does not yet -- never set a schedule before the approval."""
        return concierge_propose.start_proposed(AGENT_NAME, name)

    tools = [
        *_web_tools(),
        find_images,
        fetch_file,
        find_file,
        generate_image,
        describe_capabilities,
        look_at_my_workspaces,
        propose_agent,
        start_proposed_agent,
    ]

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
                return _give(sink, name=name, mime=mime, data=data, link=c["url"]) + f" ({c['label']})"
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

        @tool("propose_command")
        def propose_command(name: str, template: str, param: str = "") -> str:
            """When the user asks to SAVE a repeated request as a reusable command (e.g. 'save that as a
            command', 'remember this as a command'), propose it for THEIR approval. `name` = a short label;
            `template` = the request written as prose with a {{param}} slot for the part that varies (e.g.
            'Find me a Business Finland {{programme}} funding PDF and let me pick'); `param` = that slot's
            name (omit if the command has no variable part). I show it to the user as a Yes/No approval and,
            only if they approve, add it to my command menu. Use ONLY on an explicit save request; then STOP."""
            cid = re.sub(r"[^a-z0-9_]+", "_", (name or "").lower()).strip("_") or "cmd"
            cmd = {
                "id": f"learned_{cid}"[:40],
                "label": (name or "Saved command")[:40],
                "description": f"Saved command: {name}"[:120],
                "template": template or "",
            }
            if param and ("{{" + param + "}}") in (template or ""):
                cmd["params"] = [{"name": param, "type": "text", "required": True}]
            summary = f'Save this as a command?\n\n• **{cmd["label"]}** -> "{template}"'
            ok = hitl.ask_approval(
                AGENT_NAME, ask_to, ask_conv, summary=summary, action_id="save_command", payload=cmd, body=summary
            )
            sink["asked"] = ok
            return "Asked the user to approve saving the command." if ok else "Couldn't send the approval question."

        tools.append(propose_command)

    return tools


# Static chat-commands for the inbox composer — each is a fill-in template; clicking the chip drops the
# filled PROSE into the composer, the user sends it, and concierge's crew interprets it (template-as-body,
# so the {{params}} are LLM-interpreted, not rigidly parsed). See CrewSpec.chat_commands.
_BASE_CHAT_COMMANDS = [
    {
        "id": "search_web",
        "label": "Search the web",
        "description": "Find the best links and send them",
        "template": "Search the web for {{query}} and send me the top {{count}} links.",
        "params": [
            {"name": "query", "type": "text", "required": True, "placeholder": "e.g. AI agents in Finland"},
            {"name": "count", "type": "number", "required": False, "placeholder": "5", "default": "5"},
        ],
    },
    {
        "id": "find_images",
        "label": "Find images",
        "description": "Find images for a vibe/topic and attach them (moodboard)",
        "template": "Find {{count}} images of {{topic}} and attach them.",
        "params": [
            {"name": "topic", "type": "text", "required": True, "placeholder": "e.g. cosy cabin interiors"},
            {"name": "count", "type": "number", "required": False, "placeholder": "4", "default": "4"},
        ],
    },
    {
        "id": "find_document",
        "label": "Find a document",
        "description": "Find a PDF/form on the web; pick which to download",
        "template": "Find me a {{kind}} document/PDF about {{topic}} and let me pick which to download.",
        "params": [
            {
                "name": "kind",
                "type": "text",
                "required": False,
                "placeholder": "e.g. application form",
                "default": "PDF",
            },
            {"name": "topic", "type": "text", "required": True, "placeholder": "e.g. Business Finland funding"},
        ],
    },
    {
        "id": "generate_image",
        "label": "Make an image",
        "description": "Generate an image from a description",
        "template": "Make an image of {{description}}.",
        "params": [{"name": "description", "type": "text", "required": True, "placeholder": "e.g. a neon fox in snow"}],
    },
    {
        "id": "analyze_file",
        "label": "Read my file",
        "description": "Attach a file/image; I read it and extract what you ask",
        "template": "Read the file I attached and {{what}}.",
        "params": [
            {
                "name": "what",
                "type": "text",
                "required": False,
                "placeholder": "extract everything useful",
                "default": "extract everything useful and summarise it",
            }
        ],
    },
]


_LEARNED_CMDS_KEY = "chat.commands.learned"  # owner memory: commands the user taught me (self-authored)


def _learned_commands(agent_name: str) -> list[dict]:
    """Commands the owner has SAVED via the self-authoring flow (persisted in owner memory)."""
    r = _aimeat_call(agent_name, "aimeat_memory_read", {"key": _LEARNED_CMDS_KEY}) or {}
    val = r.get("value") if isinstance(r, dict) else None
    val = val if val is not None else (r.get("data") or {}).get("value") if isinstance(r, dict) else None
    items = (val or {}).get("commands") if isinstance(val, dict) else val
    return items if isinstance(items, list) else []


def _save_learned_command(agent_name: str, cmd: dict) -> bool:
    """Append a learned command (dedup by id), persist to owner memory, and REPUBLISH the public palette."""
    learned = [c for c in _learned_commands(agent_name) if c.get("id") != cmd.get("id")]
    learned.append(cmd)
    _aimeat_call(
        agent_name,
        "aimeat_memory_write",
        {"key": _LEARNED_CMDS_KEY, "value": {"commands": learned[:24]}, "visibility": "owner"},
    )
    return _republish_chat_commands(agent_name)


def _chat_commands(agent_name: str) -> list[dict]:
    """Build concierge's PUBLIC command palette DYNAMICALLY: the static base commands, PLUS one
    'Ask <specialist>' command per specialist whose daemon is LIVE right now (orchestrator.live_services),
    PLUS any commands the owner has SELF-AUTHORED (saved via the approval flow). Regenerated on every start
    + on each new saved command, so the advertised menu reflects who's up and what the owner has taught me."""
    cmds = list(_BASE_CHAT_COMMANDS)
    for s in orchestrator.live_services(agent_name, SERVICE_DIRECTORY):
        cmds.append(
            {
                "id": f"ask_{s['name'].replace('-', '_')}",
                "label": f"Ask {s['name']}",
                "description": s["desc"][:120],
                "template": f"Ask {s['name']} to {{{{request}}}}.",
                "params": [{"name": "request", "type": "text", "required": True, "placeholder": "what you need"}],
            }
        )
    cmds.extend(_learned_commands(agent_name))
    return cmds


def _republish_chat_commands(agent_name: str) -> bool:
    """Rewrite the public 'chat.commands' key at runtime (after a command is learned) — same shape the
    scaffold publishes on start."""
    cmds = _valid_chat_commands(_chat_commands(agent_name))
    res = _aimeat_call(
        agent_name,
        "aimeat_memory_write",
        {"key": "chat.commands", "value": {"v": 1, "commands": cmds}, "visibility": "public"},
    )
    return bool(res)


def _agent(
    llm, sink: dict, *, ask_to: str | None = None, ask_conv: str | None = None, extra_tools: list | None = None
) -> Agent:
    return Agent(
        role="Concierge",
        goal="Understand the user's request and fulfil it with the right tool(s), then reply concisely.",
        backstory=(
            "You are a friendly, capable concierge reached over direct message. You search the web and "
            "return the best links, find images and attach them, find a document/PDF on the web and attach "
            "it, fetch a file from a URL, and generate an image from a description. When the user asks what "
            "you can do, you call describe_capabilities. If a request is genuinely ambiguous (a wrong guess "
            "would waste effort), you ask ONE structured clarifying question with ask_user instead of "
            "guessing. You keep replies concise and always cite source links for web results. "
            "You live on the person's own AIMEAT node, which keeps their organisms and workspaces (their CRM, "
            "their documents, their records) and which makes, runs and credentials agents itself. When the "
            "person asks for a new agent, a helper for one job, or work that should happen regularly "
            "('every morning...', 'keep an eye on...'), you look at their workspaces with "
            "look_at_my_workspaces and call propose_agent. Never recommend an outside agent builder, "
            "automation service or product for that -- the agent is made here, on their node, and runs here."
        ),
        llm=llm,
        tools=[*_concierge_tools(sink, ask_to=ask_to, ask_conv=ask_conv), *(extra_tools or [])],
        allow_delegation=False,
        verbose=False,
    )


def _task(request: str, context: str, agent: Agent, today: str, directory: str = "", declinable: bool = False) -> Task:
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
            "ask what you can do (or it's a vague greeting), call describe_capabilities. "
            + (
                # The task path: the reply is text on a task list and NOTHING can be attached to it.
                "This reply goes on the person's task list, where nothing can be attached: the tools give "
                "you each image or file as a LINK -- put every link in your reply, and never say that "
                "something is attached. "
                if declinable
                else "Attach images/files with the tools and mention what you attached. "
            )
            + "If the request is genuinely ambiguous (a wrong "
            "guess would waste effort), call ask_user with 2-5 options to clarify FIRST, then STOP and wait. "
            "If the context contains an 'Attached file analysis' section, the user sent file(s)/image(s) and "
            "that is what I already read from them — use it to answer their question or to summarise/extract "
            "what's in the file; don't claim I can't open files. "
            "If the user explicitly asks to SAVE or REMEMBER a request as a reusable command, call "
            "propose_command (then STOP — they'll get a Yes/No to approve it). "
            "When the person asks for a NEW AGENT, a helper for one job, or work that should happen regularly "
            "('every morning...', 'keep an eye on...'): first call look_at_my_workspaces (with the name of the "
            "data they mention, e.g. their CRM) to see what they keep, then call propose_agent with a purpose "
            "that names THEIR workspace, and reply with exactly what propose_agent returned. ALWAYS file the "
            "proposal in THIS run, also when their node has no workspace yet or a detail is missing: the "
            "person asked for a proposal and gets one. Decide the open details yourself and SAY them as "
            "assumptions beside the proposal -- the agent writes in the language of their request, a clock "
            "job runs at 07:00 Europe/Helsinki, and with no workspace yet the agent reads memory until one "
            "exists (leave `workspace` empty then) -- and ask them to correct any of it in their reply. Never "
            "ask the workspace name, the language or the time INSTEAD of proposing. This node makes, "
            "runs and credentials the agent itself. Never recommend an outside agent builder, automation "
            "service or product (no CrewAI Studio, Zapier, HubSpot or the like) for that. When they later say "
            "'start it' about an agent you proposed with a schedule, call start_proposed_agent. "
            "When a request is about their own data, look_at_my_workspaces shows where it lives -- say which "
            "workspace it belongs in rather than naming an outside product. "
            + (
                # The task path only: a task the person handed over is on their list, and "I cannot" must
                # not land there as done (crewaimeat.decline). A DM has no list; the reply is the answer.
                "If you will NOT carry out this request -- you have no tool or access for it, or it is not "
                "yours to do -- call decline_request with the reason (and who or what can do it), then reply. "
                "Proposing an agent for it IS doing it; do not decline then. "
                if declinable
                else ""
            )
            + "Reply concisely in markdown; cite source links for any web results. Answer ONLY the message "
            "above — ignore earlier topics unless asked to continue."
        ),
        expected_output="A concise, friendly markdown reply. Attachments are added by the tools.",
        agent=agent,
    )


def build_domain(ctx: BuildContext):
    # Task path (an assigned task rather than a DM): same crew; attachments aren't delivered (no thread to
    # reply to), so the reply carries links/text. The DM path (run() below) collects + delivers attachments.
    sink: dict = {"attachments": [], "links_only": True, "links": []}
    tid = (ctx.task or {}).get("id")
    if tid:
        _TASK_LINKS[str(tid)] = sink["links"]
    decline = make_decline_tool(ctx.identity or AGENT_NAME, tid)
    agent = _agent(ctx.llm, sink, extra_tools=[decline])
    return ([agent], [_task(ctx.prompt, "", agent, ctx.today, declinable=True)])


def _dm_request_and_context(event: dict) -> tuple[str, str, list]:
    """THIS event's message (full body) + a short prior-context + the message's ATTACHMENTS. The request is
    the TRIGGERING message — matched by the event id in the thread, else the wake's own preview. NOT
    inbound[-1]: the thread read can lag behind the just-arrived DM (read-after-write), which would make us
    answer the PREVIOUS one."""
    mid, conv, _sender, preview, _subject = dm._inbound_fields(event)
    msgs = []
    if conv:
        thread = dm.dm_thread(AGENT_NAME, conv)
        msgs = (thread.get("messages") if isinstance(thread, dict) else None) or []

    def _mid(m):
        return m.get("id") or m.get("message_id")

    target = next((m for m in msgs if _mid(m) == mid), None)
    request = (target.get("body") if target else None) or preview or "(empty message)"
    # full attachment objects live on the thread message; the wake's are lightweight — prefer the message's
    attachments = (target.get("attachments") if target else None) or event.get("attachments") or []
    # context = messages strictly BEFORE this one, so the current request never leaks into "context"
    prior: list = []
    for m in msgs:
        if _mid(m) == mid:
            break
        prior.append(m)
    ctx_lines = [
        f"{'me' if m.get('direction') == 'outbound' else 'user'}: {str(m.get('body') or '')[:300]}" for m in prior[-6:]
    ]
    return str(request), "\n".join(ctx_lines), attachments


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
        request, context, attachments = _dm_request_and_context(event)
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
            # (B0) Is it the answer to a HITL gate we opened (e.g. approving a self-authored command)?
            res = hitl.resolve(AGENT_NAME, event)
            if res is not None and res.get("action_id") == "save_command":
                if not res.get("approved"):
                    return "Okay, I won't save it."
                cmd = res.get("payload") or {}
                ok = _save_learned_command(AGENT_NAME, cmd)
                return (
                    f"Saved **{cmd.get('label')}** as a command — you'll see it in the composer."
                    if ok
                    else "I approved it but couldn't publish the command; try again?"
                )
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
        # (D) The user attached file(s)/image(s)? Read each through vision/text extraction and give the crew
        # the findings as context — so it can answer questions about them or just summarise what it found.
        if attachments:
            blocks = [vision.analyze_attachment(AGENT_NAME, a) for a in attachments[:5]]
            context = (context + "\n\n" if context else "") + "Attached file analysis:\n" + "\n\n".join(blocks)
            if not request or request in ("(empty message)", "(empty)") or len(request.strip()) < 4:
                request = "I attached file(s)/image(s). Read them and extract everything useful, then summarise."
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
            discover=True,  # router/front-door: survey what already exists on the node before answering/delegating
            listen_for=("tasks", "dms"),
            on_dm=lambda e: dm.handle_dm_event(AGENT_NAME, e, _dm_responder, seen=_seen),
            tags=CAPABILITY_TAGS,
            capabilities=CAPABILITIES,
            chat_commands=_chat_commands,  # dynamic: base commands + one "Ask <specialist>" per live agent
            clean_deliverable=_links_into_reply,  # a task's reply carries every link a tool handed out
        )
    )


if __name__ == "__main__":
    run()

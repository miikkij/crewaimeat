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

from crewaimeat import dm, image_contract, seedream_gen
from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.crew import _web_tools
from crewaimeat.llm import get_llm

AGENT_NAME = "concierge"

CAPABILITIES_TEXT = (
    "I'm a **concierge** you can DM. I can:\n"
    "- **Search the web** and reply with the best links (title + url + a one-line why).\n"
    "- **Find images** for a vibe or topic and attach them moodboard-style (thumbnails in your inbox).\n"
    "- **Fetch a file** from a public URL and attach it (pdf, doc, image, …).\n"
    "- **Generate an image** from a description and attach it.\n\n"
    'Just tell me what you want — e.g. "find 4 cosy cabin interiors", "search the latest on X and send '
    'links", "grab this PDF <url>", or "make an image of a neon fox". I reply right here in this thread.'
)

README = """[[FIGLET:slant]["Concierge"]]

A conversational agent you **DM**. It searches the web (returns links), finds images and attaches them
moodboard-style, fetches a file from a URL as an attachment, and generates an image from a description —
then replies right in the thread. Ask **"what can you do?"** and it tells you.

**How to talk to me:** DM me a request — "find 4 cosy cabins", "search latest on X + links",
"grab <url>", "make an image of a neon fox". I reply in-thread with text, links, and attachments.
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


def _concierge_tools(sink: dict) -> list:
    """The toolset, bound to a per-message `sink` (sink["attachments"] collects files for the reply)."""

    @tool("find_images")
    def find_images(query: str, count: int = 4) -> str:
        """Find up to `count` images on the open web for `query` and ATTACH them to the reply (moodboard)."""
        n = 0
        want = min(max(int(count or 4), 1), _MAX_IMAGES)
        for hit in image_contract._searxng_images(query, want * 2):
            if n >= want:
                break
            dl = image_contract._download_image(hit.get("url", ""))
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

    return [*_web_tools(), find_images, fetch_file, generate_image, describe_capabilities]


def _agent(llm, sink: dict) -> Agent:
    return Agent(
        role="Concierge",
        goal="Understand the user's request and fulfil it with the right tool(s), then reply concisely.",
        backstory=(
            "You are a friendly, capable concierge reached over direct message. You search the web and "
            "return the best links, find images and attach them, fetch a file from a URL and attach it, and "
            "generate an image from a description. When the user asks what you can do, you call "
            "describe_capabilities. You keep replies concise and always cite source links for web results."
        ),
        llm=llm,
        tools=_concierge_tools(sink),
        allow_delegation=False,
        verbose=False,
    )


def _task(request: str, context: str, agent: Agent, today: str) -> Task:
    return Task(
        description=(
            f"Today is {today}. The user sent this direct message:\n\n{request}\n\n"
            f"Recent conversation (for context):\n{context or '(none)'}\n\n"
            "Decide what they want and do it with your tools. If they ask what you can do (or it's a vague "
            "greeting), call describe_capabilities. Attach images/files with the tools and mention what you "
            "attached. Reply concisely in markdown; include source links for any web results."
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
    """The triggering DM's full text + a short prior-message context, fetched from the thread (the wake's
    preview is truncated). Falls back to the wake preview if the thread read fails."""
    _id, conv, _sender, preview, _subject = dm._inbound_fields(event)
    msgs = []
    if conv:
        thread = dm.dm_thread(AGENT_NAME, conv)
        msgs = (thread.get("messages") if isinstance(thread, dict) else None) or []
    inbound = [m for m in msgs if m.get("direction") == "inbound"]
    request = (inbound[-1].get("body") if inbound else None) or preview or "(empty message)"
    ctx_lines = [
        f"{'me' if m.get('direction') == 'outbound' else 'user'}: {str(m.get('body') or '')[:300]}" for m in msgs[-6:]
    ]
    return str(request), "\n".join(ctx_lines)


def run() -> None:
    _seen: set = set()

    def _dm_responder(event: dict):
        request, context = _dm_request_and_context(event)
        sink: dict = {"attachments": []}
        agent = _agent(get_llm(agent_name=AGENT_NAME), sink)
        crew = Crew(agents=[agent], tasks=[_task(request, context, agent, "")], process=Process.sequential)
        try:
            result = crew.kickoff()
        except Exception as exc:  # noqa: BLE001
            print(f"[{AGENT_NAME}] concierge crew failed: {exc!r}", file=sys.stderr)
            return "Sorry — I hit an error handling that. Try rephrasing?"
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

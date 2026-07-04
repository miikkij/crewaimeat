"""advisor — the in-app copilot brain for aimeat-agency.

A local-first conversational helper a non-developer can brainstorm with: "what can I make with the agents
and templates I have?", "build an app for my agent's data", "what should I do next?". It runs IN-PROCESS
in the cockpit (a CrewAI Agent+Task+`Crew.kickoff()` against the appliance's model — local Ollama first,
OpenRouter if a key is set), mirroring the concierge crew's shape but with APPLIANCE-LOCAL tools and no
federated DM.

Two safety rails make it reliable even on a small local model:
  1. GROUNDING — every turn injects the deterministic `journey` (where the user really is) + the real
     catalog (templates + the user's agents). The prompt forbids inventing steps.
  2. PROPOSE-THEN-CONFIRM — the mutating tools only STAGE an action (a button); nothing runs until the
     user clicks it (the button hits an already-gated cockpit route). The cockpit also appends the
     journey's next-step action deterministically, so a correct button appears even if the LLM misfires.

    from crewaimeat.agency import advisor
    out = advisor.respond("what can I build?", journey, catalog, history, llm=llm, templates=..., agents=...)
    out["text"]      # the reply
    out["actions"]   # proposed buttons [{kind, ...}]
"""

from __future__ import annotations

# Deterministic interest -> template mapping (EN + FI stems). Used to suggest concrete builds without
# relying on the LLM, and as the fallback when the model returns nothing useful.
_KEYWORDS: dict[str, str] = {
    "compan": "company-watcher",
    "competitor": "company-watcher",
    "rival": "company-watcher",
    "kilpailij": "company-watcher",
    "yhtiö": "company-watcher",
    "yritys": "company-watcher",
    "page": "page-watcher",
    "website": "page-watcher",
    "web page": "page-watcher",
    "sivu": "page-watcher",
    "verkkosivu": "page-watcher",
    "research": "research-assistant",
    "answer": "research-assistant",
    "question": "research-assistant",
    "tutki": "research-assistant",
    "kysy": "research-assistant",
    "vastaa": "research-assistant",
    "brief": "daily-briefing",
    "digest": "daily-briefing",
    "morning": "daily-briefing",
    "katsaus": "daily-briefing",
    "aamu": "daily-briefing",
    "kooste": "daily-briefing",
    "map": "map-snapshot",
    "kartta": "map-snapshot",
    "location": "map-snapshot",
    "news": "topic-watcher",
    "topic": "topic-watcher",
    "watch": "topic-watcher",
    "monitor": "topic-watcher",
    "uutis": "topic-watcher",
    "seuraa": "topic-watcher",
    "aihe": "topic-watcher",
}


def suggest_builds(text: str, templates: list) -> list[dict]:
    """Deterministic keyword → template suggestions for a stated interest. Up to 3 {template_id, title,
    why}. Pure, no LLM — the cockpit appends these as create-agent buttons so relevant options appear even
    if the model emits nothing."""
    t = (text or "").lower()
    by_id = {tpl.get("id"): tpl for tpl in templates}
    ids: list[str] = []
    for kw, tid in _KEYWORDS.items():
        if kw in t and tid not in ids and tid in by_id:
            ids.append(tid)
    out = []
    for tid in ids[:3]:
        tpl = by_id[tid]
        out.append({"template_id": tid, "title": tpl.get("title") or tid, "why": tpl.get("description") or ""})
    return out


def scripted_reply(journey: dict, lang: str = "en") -> str:
    """A deterministic next-step sentence — used before any model exists, and as the LLM-empty fallback."""
    nxt = journey.get("next")
    if not nxt:
        return {
            "fi": "Hienoa — agenttisi pyörii ja sovellus on rakennettu. Tutki seuraavaksi aimeat.io:ta!",
        }.get(lang, "Nice — your agent is running and its app is built. Explore aimeat.io next!")
    hint = nxt.get("hint") or ""
    if lang == "fi":
        return f"Seuraava askel: {nxt['title']}. {hint}".strip()
    return f"Your next step: {nxt['title']}. {hint}".strip()


def build_catalog_context(templates: list, agents: list) -> str:
    """A compact catalog block for the prompt: the templates the user can build + the agents they have."""
    lines = ["TEMPLATES the user can turn into an agent (use these exact ids):"]
    for t in templates:
        lines.append(f"- {t.get('id')}: {t.get('title')} — {t.get('description')}")
    if agents:
        lines.append("")
        lines.append("THE USER'S AGENTS (already created):")
        for a in agents:
            run = " — running" if a.get("running") else ""
            lines.append(f"- {a.get('agent_name')} (from {a.get('template_id')}){run}")
    else:
        lines.append("")
        lines.append("THE USER'S AGENTS: none yet.")
    return "\n".join(lines)


def _journey_block(journey: dict) -> str:
    lines = ["THE USER'S REAL PROGRESS (never invent a step beyond this list):"]
    for st in journey.get("steps", []):
        mark = "[done]" if st["done"] else (">> NEXT" if st.get("is_next") else "[ ]")
        opt = " (optional)" if st.get("optional") else ""
        lines.append(f"{mark} {st['title']}{opt} — {st.get('hint', '')}")
    nxt = journey.get("next")
    if nxt:
        lines.append(f"\nTHE SINGLE NEXT STEP to steer them toward: {nxt['title']}.")
    return "\n".join(lines)


def _advisor_tools(sink: dict, templates: list, agents: list):
    from crewai.tools import tool

    valid_tids = {t.get("id") for t in templates}

    def stage(action: dict) -> None:
        sink["actions"].append(action)

    @tool("propose_create_agent")
    def propose_create_agent(template_id: str, name: str, description: str = "") -> str:
        """Propose creating a NEW agent from a template. This only adds a BUTTON the user clicks to
        confirm — you do NOT create it yourself. `template_id` MUST be one of the listed template ids.
        `name` is a short lowercase agent name; `description` is what the user wants it to do."""
        tid = (template_id or "").strip()
        if tid not in valid_tids:
            return f"Unknown template '{tid}'. Use one of: {', '.join(sorted(x for x in valid_tids if x))}."
        stage(
            {
                "kind": "create_brain",
                "template_id": tid,
                "name": (name or "").strip(),
                "prose": (description or "").strip(),
            }
        )
        return f"Added a button for the user to create a '{tid}' agent."

    @tool("propose_build_app")
    def propose_build_app(agent: str) -> str:
        """Propose building an AIMEAT app that shows an agent's published data. Adds a button the user
        clicks to confirm. Use the exact name of one of the user's agents."""
        stage({"kind": "build_app", "agent": (agent or "").strip()})
        return "Added a button to build the data app."

    @tool("propose_test_run")
    def propose_test_run(agent: str, prompt: str = "") -> str:
        """Propose running an agent once so it produces something. Adds a button the user clicks. `prompt`
        is an optional one-line task for the run."""
        stage({"kind": "test_run", "agent": (agent or "").strip(), "prompt": (prompt or "").strip()})
        return "Added a button to run the agent once."

    @tool("propose_publish_offer")
    def propose_publish_offer(agent: str) -> str:
        """Propose advertising an agent's capability on aimeat.io so others can order it. Adds a button the
        user clicks to confirm."""
        stage({"kind": "publish_offer", "agent": (agent or "").strip()})
        return "Added a button to publish the agent's offer."

    @tool("propose_open_link")
    def propose_open_link(url: str) -> str:
        """Propose opening a web link (e.g. a page on aimeat.io, or the user's built app). Adds a button
        the user clicks to open it in their browser."""
        stage({"kind": "open_url", "url": (url or "").strip()})
        return "Added a link button."

    @tool("propose_generate_app")
    def propose_generate_app(idea: str = "") -> str:
        """Propose building a CUSTOM app (any idea, not just the agent's data) with AI. Adds a button that
        opens the 'Generate App with AI' panel prefilled with `idea` — it hands the user a ready-to-paste
        prompt for their own AI chat. Use this when the user wants an app that isn't one of the templates."""
        stage({"kind": "generate_app", "idea": (idea or "").strip()})
        return "Added a button to open the app generator."

    return [
        propose_create_agent,
        propose_build_app,
        propose_test_run,
        propose_publish_offer,
        propose_open_link,
        propose_generate_app,
    ]


_ROLE = "aimeat-agency copilot"
_GOAL = "Help a non-developer decide what to build with their agents and templates, and guide the single next step."
_BACKSTORY = (
    "You are the friendly copilot inside the aimeat-agency desktop app. The user is NOT a developer. You "
    "help them brainstorm what their agents can produce, and you steer them to their real next step. You "
    "are grounded in their actual state — you never make up a step or a template that isn't listed."
)


def respond(
    message: str,
    journey: dict,
    catalog_ctx: str,
    history: list,
    *,
    llm,
    templates: list | None = None,
    agents: list | None = None,
) -> dict:
    """Run one copilot turn in-process. Returns {"text", "actions"}. `actions` are proposed buttons the
    tools staged (propose-then-confirm). Never raises — returns empty text on any failure so the cockpit
    can fall back to the deterministic scripted reply."""
    sink: dict = {"actions": []}
    try:
        from crewai import Agent, Crew, Task
    except Exception:  # noqa: BLE001
        return {"text": "", "actions": []}

    tools = _advisor_tools(sink, templates or [], agents or [])
    try:
        agent = Agent(
            role=_ROLE,
            goal=_GOAL,
            backstory=_BACKSTORY,
            tools=tools,
            llm=llm,
            allow_delegation=False,
            verbose=False,
            max_iter=4,
        )
        convo = "\n".join(f"{m.get('role')}: {m.get('text')}" for m in (history or [])[-6:])
        desc = (
            f"{_journey_block(journey)}\n\n{catalog_ctx}\n\n"
            + (f"RECENT CONVERSATION:\n{convo}\n\n" if convo else "")
            + f"THE USER JUST SAID:\n{message}\n\n"
            "HOW TO REPLY:\n"
            "- Keep it short (2-4 sentences), warm, and concrete. Plain language, no jargon.\n"
            "- Base any 'what next' ONLY on THE SINGLE NEXT STEP above — never invent steps.\n"
            "- When you suggest something the user can do (create an agent, build the data app, run it, "
            "publish an offer, open a link), CALL the matching propose_* tool so a button appears. Do NOT "
            "claim you did it — the user clicks the button to confirm.\n"
            "- Only suggest templates and agents that appear in the lists above (use their exact ids/names)."
        )
        task = Task(
            description=desc,
            expected_output="A short, friendly, concrete reply (2-4 sentences) steering the user to the next step.",
            agent=agent,
        )
        result = Crew(agents=[agent], tasks=[task], verbose=False).kickoff()
        text = str(getattr(result, "raw", result) or "").strip()
    except Exception:  # noqa: BLE001 — a weak local model / tool hiccup must not 500 the chat
        text = ""
    return {"text": text, "actions": sink["actions"]}

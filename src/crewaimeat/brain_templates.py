"""brain_templates — the author-time half of the brain model: a registry of TEMPLATES devs write, that
users turn into BRAINS by adding prose + policy (see `brains`).

A **template** is a working crew skeleton: a `build(ctx, brain) -> (agents, tasks)` factory plus the
metadata the cockpit's gallery shows and the default prose/policy a new brain starts from. The user
never sees the code; in the GUI they pick a template, write prose ("what I want it to do"), and set
policy (autonomy / spend cap / model / schedule / where to publish). `brains.build_crewspec` then wires
the chosen template's `build` into a live crew.

(Named `brain_templates`, not `templates`, because `crewaimeat.templates` is already the package of
crew-FILE scaffolds — a different, author-side concept.)

This module stays import-light (no crewai at module load) so the registry can be read cheaply by the
cockpit; the heavy imports happen inside each template's `build`.

    from crewaimeat import brain_templates
    brain_templates.all_templates()          # gallery source
    t = brain_templates.get("topic-watcher")
    agents, tasks = t.build(ctx, brain)
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Template:
    """One author-time crew skeleton the gallery offers.

    The `title`/`description`/`default_prose` are the English baseline; `i18n` carries per-language
    overrides ({"fi": {"title": …, "description": …, "default_prose": …}}) so a Finnish user sees the
    template — AND the prose they start editing — in their own language. Localization is a first-class
    part of authoring a template, not an afterthought. Read a localized view with `.localized(lang)`.
    """

    id: str  # stable id stored on each brain (e.g. "topic-watcher")
    title: str  # gallery label (English baseline)
    description: str  # one-line "what this kind of agent does" (English baseline)
    default_prose: str  # the prose a new brain starts from, the user edits this (English baseline)
    default_policy: dict  # the policy a new brain starts from (autonomy/spend/model/schedule/…)
    build: Callable[[Any, dict], tuple[list, list]]  # (BuildContext, brain) -> (agents, tasks)
    policy_fields: list[dict] = field(default_factory=list)  # editor hints: [{key, label, type, help}]
    i18n: dict = field(default_factory=dict)  # {lang: {title?, description?, default_prose?}} overrides
    offer: dict | None = None  # optional offer META (crew_offer shape) the agent can ADVERTISE on the
    #   node so others can request this capability — id/title/ask/example/cost/latency/… . None = the
    #   template offers nothing to request. Advertising is opt-in per brain (policy.offer_enabled).

    def localized(self, lang: str = "en") -> dict:
        """A plain dict view of this template with `lang` strings applied (English fallback per field)."""
        loc = self.i18n.get(lang, {}) if lang != "en" else {}
        return {
            "id": self.id,
            "title": loc.get("title", self.title),
            "description": loc.get("description", self.description),
            "default_prose": loc.get("default_prose", self.default_prose),
            "default_policy": self.default_policy,
            "policy_fields": self.policy_fields,
            "languages": ["en", *sorted(self.i18n.keys())],
            "offer": self.offer,
        }


REGISTRY: dict[str, Template] = {}


def register(t: Template) -> Template:
    """Add (or replace) a template in the registry. Returns it for convenience."""
    REGISTRY[t.id] = t
    return t


def get(template_id: str) -> Template | None:
    """The template for an id, or None."""
    return REGISTRY.get(template_id)


def all_templates() -> list[Template]:
    """Every registered template (the gallery source), title-sorted."""
    return sorted(REGISTRY.values(), key=lambda t: t.title)


# --------------------------------------------------------------------------------------------------
# Built-in template: topic-watcher — the first proof crew.
# Watches a topic on the live web, keeps raw findings in its LOCAL memory, and publishes only the
# refined summary UPWARD. Exercises the whole two-tier flow (raw local -> refined published) end to end.
# --------------------------------------------------------------------------------------------------

_TOPIC_WATCHER_PROSE = (
    "Get the latest news on the topic given in the task. Find what is genuinely new, summarize the "
    "notable items concisely with their sources, and skip noise and repeats."
)

_TOPIC_WATCHER_POLICY = {
    "autonomy": "draft",  # act | draft | ask | off  (Draft = produce, wait for approval to publish)
    "spend_cap": {"amount": 2, "period": "day", "currency": "EUR"},
    "model": None,  # None = use llm_providers.json routing; else an llm.save_override spec
    "schedule": {"cron": "0 8 * * *", "timezone": "Europe/Helsinki"},  # "every morning at 8"
    "publish_key": "",  # owner memory key base the refined summary publishes to (blank = auto: watch.<agent>)
    "key_mode": "date",  # how each run's key is suffixed so outputs are distinct + sortable by apps on the
    #   node: "latest" (one key, overwritten) | "date" (watch.<agent>.YYYY-MM-DD) | "timestamp" (…THH-MM-SS)
    "visibility": "owner",  # owner | public
    "offer_enabled": False,  # opt-in: advertise this agent's capability on the node so others can request it
}

# What this agent advertises others can request from it (crew_offer META shape). Free to advertise; the
# one-click order + escrow path is the node side (Slice 0/2). The ask carries negative scope (hard rule).
_TOPIC_WATCHER_OFFER = {
    "id": "topic-summary",
    "title": "Latest on a topic, summarized",
    "ask": "Give me a topic and I return a concise, source-cited summary of what's genuinely new. "
    "I do NOT give opinions, predictions, or unsourced claims.",
    "example": "Topic: 'Finnish AI-startup funding' → a short digest of recent rounds, each with its source URL.",
    "cost": "cheap",
    "latency": "minutes",
    "repeatability": "idempotent",  # node enum: idempotent | accumulative | destructive
    "verification": "ungated",  # node enum: deterministic | gated | ungated (free prose, sources cited)
    "consequences": [],
    # what the orderer must provide is stated plainly in `ask` ("Give me a topic …") — that's what an
    # orderer sees. (A structured `inputs` field can come once its node-schema shape is pinned down.)
    "sample": None,
}


def _build_topic_watcher(ctx: Any, brain: dict) -> tuple[list, list]:
    # Lazy heavy imports — keep the registry import-light.
    from crewai import Agent, Task

    from crewaimeat.article_extract import fetch_article_text
    from crewaimeat.crew import _web_tools
    from crewaimeat.local_memory import make_local_memory_tools

    agent_name = brain["agent_name"]
    prose = (brain.get("prose") or _TOPIC_WATCHER_PROSE).strip()
    # The topic for THIS run = the task's TITLE + DESCRIPTION (from the test-run box / the offer order /
    # the schedule). This is what gets ADDED to the operator's prose so the agent acts on the real topic.
    _task = getattr(ctx, "task", None) or {}
    _parts = [str(_task.get("title") or "").strip(), str(_task.get("description") or "").strip()]
    request = "\n".join(p for p in _parts if p) or (getattr(ctx, "prompt", "") or "").strip()
    policy = brain.get("policy") or {}
    visibility = (policy.get("visibility") or "owner").strip().lower()
    # Build the publish key so each run is distinct + time-sortable by apps on the node (the operator
    # picks the rule via policy.key_mode). base.latest -> base; then suffix by mode.
    import datetime as _dt

    base = (policy.get("publish_key") or "").strip().rstrip(".")
    if base.endswith(".latest"):
        base = base[: -len(".latest")]
    base = base or f"watch.{agent_name}"
    mode = (policy.get("key_mode") or "date").strip().lower()
    _now = _dt.datetime.now()
    suffix = {"date": _now.strftime("%Y-%m-%d"), "timestamp": _now.strftime("%Y-%m-%dT%H-%M-%S")}.get(mode, "latest")
    publish_key = f"{base}.{suffix}"

    watcher = Agent(
        role="Topic Watcher",
        goal="Find what is genuinely new on the watched topic and distil it",
        backstory=(
            "You monitor a topic over time. You search the live web, open the most relevant results to "
            "read their full text, and you keep your raw findings in your own LOCAL memory as you go — "
            "then you publish ONLY a clean, refined summary upward. You never publish raw scraps."
        ),
        tools=[*_web_tools(), fetch_article_text, *make_local_memory_tools(agent_name)],
        llm=ctx.llm,
        verbose=True,
    )

    watch = Task(
        description=(
            f"{ctx.today}\n\n"
            "YOUR STANDING INSTRUCTIONS (from the operator — how you always behave):\n"
            f"{prose}\n\n"
            "THE SPECIFIC REQUEST FOR THIS RUN (the topic to act on right now):\n"
            f"{request or '(none was given for this run — use your standing instructions above to pick the topic)'}\n\n"
            "Do this, in order:\n"
            "1. Decide the TOPIC: use the specific request above; if none was given, fall back to your "
            "standing instructions. Then search the live web for what is genuinely NEW on that topic and "
            "open the best results with fetch_article_text to read their full text.\n"
            "2. As you go, save each raw finding to your LOCAL memory with the `remember` tool "
            "(topic='watch', set source=the site, body=the finding). This stays private to you.\n"
            "3. Write a concise summary of the notable new items (with source URLs).\n"
            f"4. Publish ONLY that refined summary upward with `publish_memory(id, key='{publish_key}', "
            f"visibility='{visibility}')` — first `remember` the summary to get its id, then publish "
            "that id. Do not publish the raw findings.\n\n"
            "YOUR FINAL ANSWER must be ONLY the clean summary itself — plain, readable prose with the "
            "source URLs, exactly as a person would want to read it. Do NOT output JSON, tool-call syntax, "
            "key/visibility fields, or the raw record. Just the summary text."
        ),
        expected_output=(
            "Plain readable prose: a concise summary of the notable new items with their source URLs. "
            "NOT JSON, not a record — just the summary text a person would read."
        ),
        agent=watcher,
    )
    return [watcher], [watch]


register(
    Template(
        id="topic-watcher",
        title="Topic watcher",
        description="Watches a topic on the web, keeps raw findings local, publishes only the refined summary.",
        default_prose=_TOPIC_WATCHER_PROSE,
        default_policy=_TOPIC_WATCHER_POLICY,
        build=_build_topic_watcher,
        offer=_TOPIC_WATCHER_OFFER,
        i18n={
            "fi": {
                "title": "Aiheen vahti",
                "description": "Seuraa aihetta verkossa, pitää raakahavainnot paikallisina ja "
                "julkaisee vain jalostetun yhteenvedon.",
                "default_prose": (
                    "Seuraa uutisia tehtävässä annetusta aiheesta. Etsi mikä on aidosti uutta, tiivistä "
                    "merkittävät kohdat napakasti lähteineen ja ohita kohina ja toistot."
                ),
            }
        },
        policy_fields=[
            {"key": "schedule", "label": "When to run", "type": "schedule", "help": "e.g. every morning at 8"},
            {"key": "autonomy", "label": "Autonomy", "type": "enum", "help": "act / draft / ask / off"},
            {"key": "spend_cap", "label": "Spend limit", "type": "money", "help": "hard cap per period"},
            {"key": "model", "label": "Model", "type": "model", "help": "local (free) or cloud (stronger)"},
            {"key": "publish_key", "label": "Publish to", "type": "text", "help": "owner memory key for the summary"},
            {"key": "visibility", "label": "Visibility", "type": "enum", "help": "owner / public"},
        ],
    )
)


# --------------------------------------------------------------------------------------------------
# More built-in templates. All reuse the SAME proven loop as topic-watcher (search the live web ->
# read the best sources with fetch_article_text -> keep raw findings in LOCAL memory -> publish only
# the refined result upward). Only the role + the task instructions differ, so each one is reliable.
# --------------------------------------------------------------------------------------------------

# Editor hints shared by these search-based templates (same controls as topic-watcher).
_STD_POLICY_FIELDS = [
    {"key": "schedule", "label": "When to run", "type": "schedule", "help": "e.g. every morning at 8"},
    {"key": "autonomy", "label": "Autonomy", "type": "enum", "help": "act / draft / ask / off"},
    {"key": "spend_cap", "label": "Spend limit", "type": "money", "help": "hard cap per period"},
    {"key": "model", "label": "Model", "type": "model", "help": "local (free) or cloud (stronger)"},
    {"key": "publish_key", "label": "Publish to", "type": "text", "help": "owner memory key for the result"},
    {"key": "visibility", "label": "Visibility", "type": "enum", "help": "owner / public"},
]

# The final-answer discipline that keeps a stray record-JSON / tool-call from leaking to the user (the
# same rule the 0.8.13 work proved is needed for small local models).
_FINAL_ANSWER_RULE = (
    "YOUR FINAL ANSWER must be ONLY the clean result itself — plain, readable prose with the source URLs, "
    "exactly as a person would want to read it. Do NOT output JSON, tool-call syntax, key/visibility "
    "fields, or the raw record. Just the text."
)
_EXPECTED = "Plain readable prose with source URLs, exactly as a person would read it — NOT JSON, not a record."


def _default_policy(cron: str | None = "0 8 * * *", autonomy: str = "draft") -> dict:
    """A fresh policy with the topic-watcher defaults; cron=None means on-demand (no schedule)."""
    return {
        "autonomy": autonomy,  # act | draft | ask | off
        "spend_cap": {"amount": 2, "period": "day", "currency": "EUR"},
        "model": None,
        "schedule": {"cron": cron, "timezone": "Europe/Helsinki"} if cron else None,
        "publish_key": "",
        "key_mode": "date",
        "visibility": "owner",
        "offer_enabled": False,
    }


def _resolve_publish_key(agent_name: str, policy: dict, default_base: str) -> str:
    """base.<suffix> where suffix follows policy.key_mode (latest|date|timestamp) — same rule as topic-watcher."""
    import datetime as _dt

    base = (policy.get("publish_key") or "").strip().rstrip(".")
    if base.endswith(".latest"):
        base = base[: -len(".latest")]
    base = base or default_base
    mode = (policy.get("key_mode") or "date").strip().lower()
    _now = _dt.datetime.now()
    suffix = {"date": _now.strftime("%Y-%m-%d"), "timestamp": _now.strftime("%Y-%m-%dT%H-%M-%S")}.get(mode, "latest")
    return f"{base}.{suffix}"


def _run_inputs(ctx: Any, brain: dict, default_prose: str, default_base: str) -> tuple[str, str, str, str]:
    """(operator prose, this-run request, visibility, publish_key) — shared by the search-based templates."""
    prose = (brain.get("prose") or default_prose).strip()
    _task = getattr(ctx, "task", None) or {}
    _parts = [str(_task.get("title") or "").strip(), str(_task.get("description") or "").strip()]
    request = "\n".join(p for p in _parts if p) or (getattr(ctx, "prompt", "") or "").strip()
    policy = brain.get("policy") or {}
    visibility = (policy.get("visibility") or "owner").strip().lower()
    publish_key = _resolve_publish_key(brain["agent_name"], policy, default_base)
    return prose, request, visibility, publish_key


def _searcher_agent(ctx: Any, agent_name: str, *, role: str, goal: str, backstory: str):
    """A web-research agent with the proven toolset: web search + article fetch + local memory + publish."""
    from crewai import Agent

    from crewaimeat.article_extract import fetch_article_text
    from crewaimeat.crew import _web_tools
    from crewaimeat.local_memory import make_local_memory_tools

    return Agent(
        role=role,
        goal=goal,
        backstory=backstory,
        tools=[*_web_tools(), fetch_article_text, *make_local_memory_tools(agent_name)],
        llm=ctx.llm,
        verbose=True,
    )


# ── research-assistant — one-shot Q&A with citations ──────────────────────────────────────────────
_RESEARCH_PROSE = (
    "Answer the question given in the task by researching the live web. Read several independent sources, "
    "give a clear and direct answer, and cite every source you used. Say plainly when something is "
    "uncertain rather than guessing."
)


def _build_research_assistant(ctx: Any, brain: dict) -> tuple[list, list]:
    from crewai import Task

    agent_name = brain["agent_name"]
    prose, request, visibility, publish_key = _run_inputs(ctx, brain, _RESEARCH_PROSE, f"answers.{agent_name}")
    agent = _searcher_agent(
        ctx,
        agent_name,
        role="Research Assistant",
        goal="Answer the question accurately by researching the live web and citing every source",
        backstory=(
            "You answer questions by doing real research: you search the live web, open the most relevant "
            "results to read their full text, and you base your answer only on what you actually read — "
            "always citing the sources. You say plainly when something is unknown or unclear, never guess."
        ),
    )
    task = Task(
        description=(
            f"{ctx.today}\n\n"
            "YOUR STANDING INSTRUCTIONS (from the operator):\n"
            f"{prose}\n\n"
            "THE QUESTION TO ANSWER FOR THIS RUN:\n"
            f"{request or '(none was given — ask the operator to provide a question)'}\n\n"
            "Do this, in order:\n"
            "1. Search the live web for the question and open the best results with fetch_article_text to "
            "read them. Use several independent sources.\n"
            "2. Save useful findings to your LOCAL memory with `remember` (topic='research', source=the "
            "site) as you go.\n"
            "3. Write a clear, direct answer grounded ONLY in what you read, with the source URLs next to "
            "the claims they support. If the sources disagree or the answer is uncertain, say so.\n"
            f"4. Publish the answer upward: first `remember` it to get its id, then "
            f"`publish_memory(id, key='{publish_key}', visibility='{visibility}')`.\n\n"
            f"{_FINAL_ANSWER_RULE}"
        ),
        expected_output=_EXPECTED,
        agent=agent,
    )
    return [agent], [task]


register(
    Template(
        id="research-assistant",
        title="Research assistant",
        description="Ask it a question; it researches the live web, reads sources, and answers with citations.",
        default_prose=_RESEARCH_PROSE,
        default_policy=_default_policy(cron=None),  # reactive Q&A, no default schedule
        build=_build_research_assistant,
        i18n={
            "fi": {
                "title": "Tutkimusassistentti",
                "description": "Kysy kysymys — se tutkii verkkoa livenä, lukee lähteet ja vastaa lähdeviittauksin.",
                "default_prose": (
                    "Vastaa tehtävässä annettuun kysymykseen tutkimalla verkkoa. Lue useita riippumattomia "
                    "lähteitä, anna selkeä ja suora vastaus ja viittaa jokaiseen käyttämääsi lähteeseen. "
                    "Sano suoraan jos jokin on epävarmaa, älä arvaa."
                ),
            }
        },
        policy_fields=_STD_POLICY_FIELDS,
    )
)


# ── daily-briefing — a short multi-topic morning digest ───────────────────────────────────────────
_BRIEFING_PROSE = (
    "Each morning, brief me on these topics (replace this with your own, one per line):\n"
    "- topic one\n- topic two\n- topic three\n"
    "For each topic, give the few genuinely new and notable items with their sources, and skip routine noise."
)


def _build_daily_briefing(ctx: Any, brain: dict) -> tuple[list, list]:
    from crewai import Task

    agent_name = brain["agent_name"]
    prose, request, visibility, publish_key = _run_inputs(ctx, brain, _BRIEFING_PROSE, f"briefing.{agent_name}")
    agent = _searcher_agent(
        ctx,
        agent_name,
        role="Briefing Editor",
        goal="Produce a short, scannable daily briefing across the chosen topics",
        backstory=(
            "You write a concise morning briefing. You take a handful of topics, check the live web for what "
            "is genuinely new on each, and distil it into a short digest a busy person can read in a minute. "
            "You group by topic and keep only what matters, with sources."
        ),
    )
    task = Task(
        description=(
            f"{ctx.today}\n\n"
            "YOUR STANDING INSTRUCTIONS (the topics to cover + how — from the operator):\n"
            f"{prose}\n\n"
            "ANY EXTRA FOCUS FOR THIS RUN (optional):\n"
            f"{request or '(none — use your standing topics above)'}\n\n"
            "Do this, in order:\n"
            "1. Work out the list of TOPICS from your standing instructions (and any extra focus above).\n"
            "2. For EACH topic, search the live web for what is genuinely new and open the best results with "
            "fetch_article_text. Save raw findings to LOCAL memory with `remember` (topic='briefing').\n"
            "3. Write ONE briefing: a short section per topic (a heading + a few lines), each notable item "
            "with its source URL. For a topic with nothing new, say 'nothing notable'.\n"
            f"4. Publish the briefing upward: `remember` it, then "
            f"`publish_memory(id, key='{publish_key}', visibility='{visibility}')`.\n\n"
            f"{_FINAL_ANSWER_RULE}"
        ),
        expected_output=_EXPECTED,
        agent=agent,
    )
    return [agent], [task]


register(
    Template(
        id="daily-briefing",
        title="Daily briefing",
        description="Give it a few topics; each morning it posts a short digest of what's new, with sources.",
        default_prose=_BRIEFING_PROSE,
        default_policy=_default_policy(cron="0 7 * * *"),  # 07:00 every morning
        build=_build_daily_briefing,
        i18n={
            "fi": {
                "title": "Aamukatsaus",
                "description": "Anna muutama aihe — joka aamu se julkaisee lyhyen koosteen uutuuksista lähteineen.",
                "default_prose": (
                    "Tee joka aamu katsaus näistä aiheista (korvaa omillasi, yksi per rivi):\n"
                    "- aihe yksi\n- aihe kaksi\n- aihe kolme\n"
                    "Kerro kustakin aiheesta muutama aidosti uusi ja merkittävä kohta lähteineen, ja ohita rutiini."
                ),
            }
        },
        policy_fields=_STD_POLICY_FIELDS,
    )
)


# ── page-watcher — watch one URL, report what changed ─────────────────────────────────────────────
_PAGE_WATCHER_PROSE = (
    "Watch this page (paste the URL here): https://example.com\n"
    "Each run, fetch it and tell me what is meaningfully new or changed since last time. Ignore cosmetic "
    "changes like dates, view counts, or ads."
)


def _build_page_watcher(ctx: Any, brain: dict) -> tuple[list, list]:
    from crewai import Task

    agent_name = brain["agent_name"]
    prose, request, visibility, publish_key = _run_inputs(ctx, brain, _PAGE_WATCHER_PROSE, f"page.{agent_name}")
    agent = _searcher_agent(
        ctx,
        agent_name,
        role="Page Watcher",
        goal="Report what meaningfully changed on the watched page since last time",
        backstory=(
            "You watch one specific web page over time. Each run you fetch its current content, compare it to "
            "the snapshot you saved last time, and report only what is meaningfully new or changed — ignoring "
            "cosmetic noise like dates, counters, or ads."
        ),
    )
    task = Task(
        description=(
            f"{ctx.today}\n\n"
            "YOUR STANDING INSTRUCTIONS (which page to watch + what counts as a meaningful change):\n"
            f"{prose}\n\n"
            "URL FOR THIS RUN (if given here, use it):\n"
            f"{request or '(none — use the URL in your standing instructions above)'}\n\n"
            "Do this, in order:\n"
            "1. Determine the URL to watch. Fetch its current text with fetch_article_text.\n"
            "2. Look up your PREVIOUS snapshot: `browse_memory(topic='page-snapshot', limit=1)`; if one "
            "exists, read it with recall_memory(id).\n"
            "3. Compare. If there is NO previous snapshot, say this is the first snapshot. Otherwise describe "
            "what is meaningfully NEW or CHANGED (ignore cosmetic differences). If nothing meaningful "
            "changed, say so plainly.\n"
            "4. Save the current content as the new baseline: `remember(topic='page-snapshot', source=the "
            "URL, body=the current text)`.\n"
            f"5. Publish your change report upward: `remember` it (topic='page-change'), then "
            f"`publish_memory(id, key='{publish_key}', visibility='{visibility}')`.\n\n"
            f"{_FINAL_ANSWER_RULE}"
        ),
        expected_output=_EXPECTED,
        agent=agent,
    )
    return [agent], [task]


register(
    Template(
        id="page-watcher",
        title="Page watcher",
        description="Watch one webpage; it reports when something meaningful changes, with the URL.",
        default_prose=_PAGE_WATCHER_PROSE,
        default_policy=_default_policy(cron="0 8 * * *"),
        build=_build_page_watcher,
        i18n={
            "fi": {
                "title": "Sivuvahti",
                "description": "Seuraa yhtä verkkosivua — raportoi kun jotain merkittävää muuttuu.",
                "default_prose": (
                    "Seuraa tätä sivua (liitä URL tähän): https://example.com\n"
                    "Kerro joka ajolla mikä on merkittävästi uutta tai muuttunut edellisestä kerrasta. Ohita "
                    "kosmeettiset muutokset kuten päivämäärät, katselukerrat tai mainokset."
                ),
            }
        },
        policy_fields=_STD_POLICY_FIELDS,
    )
)


# ── company-watcher — track one company's news ────────────────────────────────────────────────────
_COMPANY_WATCHER_PROSE = (
    "Track news and updates about the company named in the task — funding, product launches, leadership "
    "changes, partnerships, and incidents. Surface only what is genuinely notable, with sources, and skip "
    "routine noise."
)


def _build_company_watcher(ctx: Any, brain: dict) -> tuple[list, list]:
    from crewai import Task

    agent_name = brain["agent_name"]
    prose, request, visibility, publish_key = _run_inputs(ctx, brain, _COMPANY_WATCHER_PROSE, f"company.{agent_name}")
    agent = _searcher_agent(
        ctx,
        agent_name,
        role="Company Watcher",
        goal="Surface notable news and updates about the watched company, with sources",
        backstory=(
            "You track one company over time. You search the live web for news and updates about it — "
            "funding, product launches, leadership changes, partnerships, incidents — read the best sources, "
            "and surface only what is genuinely notable, skipping routine noise."
        ),
    )
    task = Task(
        description=(
            f"{ctx.today}\n\n"
            "YOUR STANDING INSTRUCTIONS (from the operator):\n"
            f"{prose}\n\n"
            "THE COMPANY TO TRACK FOR THIS RUN:\n"
            f"{request or '(none was given — use the company named in your standing instructions above)'}\n\n"
            "Do this, in order:\n"
            "1. Identify the COMPANY. Search the live web for its recent news and updates (funding, launches, "
            "leadership, partnerships, incidents) and open the best results with fetch_article_text.\n"
            "2. Save raw findings to LOCAL memory with `remember` (topic='company', source=the site).\n"
            "3. Write a concise summary of the notable items only, each with its source URL. Skip routine "
            "noise and repeats.\n"
            f"4. Publish the summary upward: `remember` it, then "
            f"`publish_memory(id, key='{publish_key}', visibility='{visibility}')`.\n\n"
            f"{_FINAL_ANSWER_RULE}"
        ),
        expected_output=_EXPECTED,
        agent=agent,
    )
    return [agent], [task]


register(
    Template(
        id="company-watcher",
        title="Company watcher",
        description="Track news and updates about a specific company; flags notable items with sources.",
        default_prose=_COMPANY_WATCHER_PROSE,
        default_policy=_default_policy(cron="0 8 * * *"),
        build=_build_company_watcher,
        i18n={
            "fi": {
                "title": "Yritysvahti",
                "description": "Seuraa tietyn yrityksen uutisia ja päivityksiä — nostaa merkittävät lähteineen.",
                "default_prose": (
                    "Seuraa tehtävässä nimetyn yrityksen uutisia ja päivityksiä — rahoitus, tuotejulkaisut, "
                    "johdon muutokset, kumppanuudet ja häiriöt. Nosta vain aidosti merkittävät kohdat "
                    "lähteineen ja ohita rutiini."
                ),
            }
        },
        policy_fields=_STD_POLICY_FIELDS,
    )
)

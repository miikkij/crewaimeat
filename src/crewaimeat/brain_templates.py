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
            "that id. Do not publish the raw findings.\n"
            "Report the published summary as your result."
        ),
        expected_output="A concise summary of the new items on the requested topic, with source URLs.",
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

"""Produce ONE subscriber's Aamukatsaus from their own order, every morning.

The same machinery that writes ours, pointed at their values: `_competitor_section` takes their
queries and their domain, the topic sweep takes their phrases, and the Grok run is written about
their subject. Nothing here is a filtered view of our briefing — a subscriber watching sailing and
a subscriber watching payments share no sources at all, which is the entire point of the product.

WHAT IT COSTS, stated plainly because it is the thing that decides pricing: every section is live
fetching plus one model call, every morning, per subscriber. `subscriptions.MAX_QUERIES` is the
ceiling that keeps that predictable.

WHERE IT LANDS: `aamukatsaus.<sub>.<date>` in OUR memory, private, read by the subscriber through
the one standing share (`subscriber_space`). They can copy it into their own memory to keep — the
app's copy button stamps `copiedFrom`, so a kept copy still says where it came from.

The two fleet sections of our own briefing (`Mitä saatiin aikaan`, `Mihin tehot menivät`) are NOT
produced here: they measure an agent fleet, and a subscriber who has none would get two empty
headings every morning. They come back the day a subscriber runs their own fleet.
"""

from __future__ import annotations

import datetime
import sys

from crewaimeat.subscriber_space import AGENT, publish
from crewaimeat.subscriptions import get_prefs, list_subscribers

_TOPIC_DOCS = 6  # extracted pages per briefing — the ceiling on one morning's fetch
_PER_QUERY = 3


def _topic_section(phrases: list[str]) -> str:
    """A sweep over the subscriber's own phrases: what was published about them this week.

    Distinct from our `SOME-radar`, which reads opportunities a Grok run already scored. This one
    has no Grok behind it yet, so it says what it is — published items — rather than borrowing a
    heading that would promise scored discussion threads."""
    from crewaimeat.article_extract import _trafilatura_text
    from crewaimeat.fetch_pipeline import _searxng_urls
    from crewaimeat.llm import get_llm

    docs: list[str] = []
    for phrase in phrases:
        for u in _searxng_urls(phrase, "en", "week", n=_PER_QUERY):
            if len(docs) >= _TOPIC_DOCS:
                break
            try:
                txt = _trafilatura_text(u)
            except Exception:  # noqa: BLE001 — one dead page never costs the section
                txt = ""
            if txt and len(txt) > 400:
                docs.append(f"[{u}]\n{txt[:2500]}")
    if not docs:
        return "## Aiheseuranta\n\n- (ei tuoreita osumia näillä hakusanoilla tänään)\n"
    prompt = (
        "You are a morning-briefing analyst. The reader follows these subjects: "
        + ", ".join(phrases)
        + ".\n\nSOURCES (this week):\n\n"
        + "\n\n".join(docs)
        + "\n\nWrite ONE markdown section in Finnish:\n## Aiheseuranta\n"
        "(3-6 bullets: what happened, each naming the source URL in parentheses. End with one line "
        "on the single item most worth the reader's attention and why.)\n\n"
        "Use ONLY facts from the sources."
    )
    try:
        llm = get_llm(for_tool_use=False, temperature=0.3, agent_name=AGENT)
        out = (llm.call([{"role": "user", "content": prompt}]) or "").strip()
        return out + "\n" if out.startswith("##") else f"## Aiheseuranta\n\n{out}\n"
    except Exception as exc:  # noqa: BLE001 — the briefing still goes out, naming the gap
        return f"## Aiheseuranta\n\n- (tuotanto epäonnistui: {exc!r})\n"


def build(subscriber: str, *, date: str | None = None, agent: str = AGENT) -> dict:
    """Build and publish one subscriber's briefing. Returns what was produced and what was skipped.

    A section that fails names its failure in the output rather than vanishing: a subscriber must be
    able to tell "nothing happened in your subjects" from "we could not look"."""
    order = get_prefs(subscriber, agent=agent)
    if not order:
        raise LookupError(f"no order for {subscriber} — nothing to build")
    if not order.get("active", True):
        return {"subscriber": subscriber, "skipped": "paused"}

    from crewaimeat.mail_contract import _competitor_section, grok_prompt

    day = date or datetime.date.today().isoformat()
    parts = [f"# Aamukatsaus — {day}\n"]
    produced: list[str] = []

    if order.get("competitors"):
        parts.append(_competitor_section(order["competitors"], domain=order.get("domain")))
        produced.append("kilpailijakatsaus")
    if order.get("radar"):
        parts.append(_topic_section(order["radar"]))
        produced.append("aiheseuranta")
    if order.get("topic"):
        parts.append(
            "## Grok-ajo\n\nAja tämä Grokissa ja liitä tuloste takaisin — osumat päätyvät "
            "huomiseen katsaukseesi.\n\n```\n" + grok_prompt(order["topic"]) + "\n```\n"
        )
        produced.append("grok")

    markdown = "\n".join(parts)
    value = {
        "date": day,
        "subscriber": subscriber,
        "body_md": markdown,  # the name the app already reads for our own briefing
        "sections": produced,
        "order": {k: order.get(k) for k in ("competitors", "radar", "topic")},
        "generatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "generatedBy": f"{agent} (crewaimeat)",
    }
    key = publish(subscriber, value, day, agent=agent)
    if not key:
        raise RuntimeError(f"briefing for {subscriber} was built but NOT saved — {len(markdown)} chars lost")
    print(f"[{agent}] briefing {key}: {', '.join(produced)}", file=sys.stderr)
    return {"subscriber": subscriber, "key": key, "sections": produced, "chars": len(markdown)}


def build_all(*, date: str | None = None, agent: str = AGENT) -> dict:
    """Every active subscriber's briefing. One failure never costs the others."""
    day = date or datetime.date.today().isoformat()
    done, failed = [], {}
    for order in list_subscribers(agent=agent):
        sub = order["subscriber"]
        try:
            done.append(build(sub, date=day, agent=agent))
        except Exception as exc:  # noqa: BLE001 — reported per subscriber, never swallowed
            failed[sub] = repr(exc)
            print(f"[{agent}] briefing FAILED for {sub}: {exc!r}", file=sys.stderr)
    return {"date": day, "built": done, "failed": failed}

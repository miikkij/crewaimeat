"""A subscription arrives as a DM, and is answered in the same breath.

The app cannot write into our memory — an app grant resolves to its OWN owner's namespace, so a
reader's browser has no route into ours. What it CAN do is send a message: `messages:send` is an
app-grantable scope, and the federated inbox is machinery this repo already runs. So the order
travels as a DM, postman validates it, and the write happens on OUR side where the validation lives.

That also means no group, no share and no owner action stand between a reader and placing an order.
Provisioning their READ access is a separate, later step (`subscriber_space.provision`) — an order
can sit accepted and waiting for it, which is better than refusing the order until an owner is free.

WIRE FORMAT — a marker line, then JSON:

    [AAMUKATSAUS-TILAUS]
    {"competitors": ["…"], "radar": ["…"], "topic": "…"}

A marker rather than "assume every DM is an order": postman's inbox is a general one, and a person
writing a sentence to it must not have their words parsed as a subscription. Anything unmarked is
left alone for the ordinary DM path.

The reply is not a courtesy. An order that vanished silently and an order that was rejected look
identical from the outside, so every marked DM gets an answer: what we saved, or exactly what was
wrong with it.
"""

from __future__ import annotations

import json
import re
import sys

from crewaimeat.dm import _inbound_fields, dm_reply
from crewaimeat.subscriber_space import _SUB_RE, AGENT
from crewaimeat.subscriptions import InvalidOrder, get_prefs, set_prefs

MARKER = "[AAMUKATSAUS-TILAUS]"
_JSON = re.compile(r"\{.*\}", re.S)


def subscriber_id(sender: str) -> str:
    """The subscriber id derived from who sent it — never from what they typed.

    A GHII is `name@node`, a GAII `agent#owner@node`; the owner's name is the identity that pays and
    reads, so that is the id. Letting the ORDER name its own id would let one reader write into
    another's key space, since the id is half of the share pattern."""
    if not sender:
        raise InvalidOrder("the message has no sender — cannot tell whose order this is")
    name = sender.split("@", 1)[0]
    if "#" in name:  # a GAII: agent#owner -> the owner behind it
        name = name.split("#", 1)[1]
    name = name.strip()
    if not _SUB_RE.match(name):
        # NOT lowercased into shape. Folding `Matti@node` to `matti` would hand two distinct
        # identities the SAME key space and the same share — a silent repair on the one value that
        # is half of a permission. Refusing names the problem to someone who can fix it.
        raise InvalidOrder(
            f"'{name}' cannot be a subscriber id (3-40 chars of lower-case a-z, 0-9, '-' or '_'). "
            "The id becomes part of a memory key AND of a share pattern, so it is taken exactly as "
            "the sender's identity reads — never adjusted to fit."
        )
    return name


def parse_order(body: str) -> dict | None:
    """The JSON after the marker, or None when this DM is not an order at all."""
    if not body or MARKER not in body:
        return None
    m = _JSON.search(body.split(MARKER, 1)[1])
    if not m:
        raise InvalidOrder(f"{MARKER} was there but no JSON object followed it")
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as exc:
        raise InvalidOrder(f"the order is not valid JSON: {exc}") from exc


def handle_dm(event: dict, *, agent: str = AGENT) -> dict | None:
    """Take one inbound DM. Returns the saved order, or None when the DM was not an order.

    Errors are ANSWERED, not raised: the sender is a person waiting for their subscription, and a
    stack trace in our log helps them not at all."""
    _mid, conv, sender, body, _subject = _inbound_fields(event)
    try:
        order = parse_order(body)
    except InvalidOrder as exc:
        _answer(conv, f"Tilausta ei voitu lukea: {exc}", agent, sender)
        return None
    if order is None:
        return None

    try:
        sub = subscriber_id(sender)
        # The thread is the DELIVERY ROUTE, so it is stored with the order. They wrote to us to
        # subscribe, which makes replying to them consented — and that is the whole reason the
        # morning briefing needs no group, no share and no owner action to reach them.
        saved = set_prefs(sub, {**order, "conversation_id": conv, "ghii": sender}, by=f"dm:{sender}", agent=agent)
    except (InvalidOrder, RuntimeError) as exc:
        _answer(conv, f"Tilaus EI tallentunut: {exc}", agent, sender)
        return None

    _answer(conv, _confirmation(saved), agent, sender)
    return saved


def _confirmation(saved: dict) -> str:
    """Say back what we will actually do with it — the only way they can spot a typo before the
    first briefing arrives."""
    lines = [f"Tilaus tallennettu ({saved['subscriber']}). Aamukatsauksesi rakentuu näin:"]
    if saved["competitors"]:
        lines.append("- Kilpailijakatsaus: " + ", ".join(saved["competitors"]))
    if saved["radar"]:
        lines.append("- Aiheseuranta: " + ", ".join(saved["radar"]))
    if saved["topic"]:
        lines.append(f"- Grok-ajo: {saved['topic']}")
    lines.append("\nMuuta tilausta lähettämällä uusi viesti samassa muodossa — se korvaa tämän.")
    return "\n".join(lines)


def _answer(conversation_id: str, text: str, agent: str, to: str = "") -> None:
    """`dm_reply(agent, to, body, conversation_id=…)` — the recipient AND the thread, because the
    thread is what makes the reply consented rather than a cold DM."""
    if not conversation_id:
        print(f"[{agent}] order handled but no conversation to answer: {text[:80]}", file=sys.stderr)
        return
    if dm_reply(agent, to, text, conversation_id=conversation_id) is None:
        print(f"[{agent}] could NOT answer the order in {conversation_id}: {text[:80]}", file=sys.stderr)


def order_of(subscriber: str, *, agent: str = AGENT) -> dict | None:
    """What we hold for this subscriber — the app reads this back so an edit starts from the truth."""
    return get_prefs(subscriber, agent=agent)

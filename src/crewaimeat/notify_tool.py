"""`notify`: the agent tells ITS OWN OWNER, in the bell and by email.

Jouni's v31 K46 (31.10.2026): a news agent made from the chat should bring the day's review to the person's
mailbox. Until now a crew could not: `dm` is the federated inbox (aimeat_dm_send, to anyone, first-contact
gated), and the node's aimeat_notify, which reaches the owner's bell and push, was in no crew's tool menu.
The node (aimeat-protocol, 4e's K45-K47 batch) added `email: true` to aimeat_notify: the notification is
also mailed to the owner's verified address, at most 10 a day per sender, never when the owner muted it,
and never to anyone else. This tool is that call and nothing more.

The answer's `emailed` says what happened to the mail: sent | not_asked | no_address | mail_off | limit |
failed | muted. The tool turns it into one sentence the model can act on, so a run whose mail did not go
says so in its reply instead of claiming it was sent.

Scope: notifications:send (SCOPES below, which concierge_propose asks for an agent given `notify`).
"""

from __future__ import annotations

import sys

from crewaimeat.aimeat_crew import _aimeat_call

SCOPES: tuple[str, ...] = ("notifications:send",)

# The node's limits (POST /v1/notifications): a longer text is refused there, so it is cut here.
TITLE_MAX = 200
BODY_MAX = 10000

_EMAILED = {
    "sent": "It was also emailed to the owner.",
    "not_asked": "No email was asked for; it is in the owner's notifications.",
    "no_address": "It is in the owner's notifications, but NOT emailed: the owner has no verified email address.",
    "mail_off": "It is in the owner's notifications, but NOT emailed: this node sends no mail.",
    "limit": "It is in the owner's notifications, but NOT emailed: today's limit of emails from this agent is used up.",
    "failed": "It is in the owner's notifications, but the email could NOT be sent.",
    "muted": "NOT delivered: the owner has muted notifications from this agent.",
}


def send_notification(agent_name: str, title: str, body: str, link: str | None = None, email: bool = True) -> str:
    """One aimeat_notify call; the outcome as a sentence. Never raises."""
    title = " ".join(str(title or "").split())[:TITLE_MAX]
    body = str(body or "").strip()[:BODY_MAX]
    if not title or not body:
        return "Nothing was sent: a notification needs a title and a text."
    payload: dict = {"title": title, "body": body, "email": bool(email)}
    # Only a path on the owner's own node: the node refuses anything else, and a link outside it is not ours to give.
    if link and str(link).startswith("/"):
        payload["link"] = str(link)
    answer = _aimeat_call(agent_name, "aimeat_notify", payload, retries=2, return_error=True)
    if answer is None:
        return "Nothing was sent: the node did not answer. Say so in your reply; do not claim it was sent."
    if isinstance(answer, dict) and answer.get("ok") is False:
        err = answer.get("error") or {}
        print(f"[{agent_name}] notify refused: {err.get('code', '')} {err.get('message', '')}", file=sys.stderr)
        return (
            f"Nothing was sent: the node refused it ({err.get('code', 'refused')}): {err.get('message', '')}. "
            "Say so in your reply; do not claim it was sent."
        )
    if isinstance(answer, dict) and answer.get("muted"):
        return _EMAILED["muted"]
    emailed = str(answer.get("emailed") or ("not_asked" if not email else "")) if isinstance(answer, dict) else ""
    return "Sent to the owner's notifications. " + _EMAILED.get(emailed, "Whether it was emailed is not known.")


def make_notify_tool(agent_name: str):
    """The `notify` tool for one agent."""
    from crewai.tools import tool

    @tool("notify")
    def notify(title: str, body: str, link: str = "", email: bool = True) -> str:
        """Send your finished result to YOUR OWNER: it appears in their notifications and, with
        email=true (the default), is also emailed to their own verified address. It reaches nobody else.
        `title` is one short line (at most 200 characters); `body` is the result itself in plain text (at
        most 10 000 characters); `link` is optional, a path on this node such as /v1/home. Call it ONCE, at
        the end, with the whole result. The answer says whether the email went; if it did not, say so in
        your reply instead of claiming it was sent."""
        return send_notification(agent_name, title, body, link or None, email)

    return notify

"""some-analyst: draft genuine reply suggestions for Social Radar opportunities (a human approves + posts).

Reads the `opportunity` records that `some-listener` (HN) and the Grok scout (Reddit/X) wrote into the
Social Radar workspace, and for each fresh, worth-it opportunity drafts a value-first reply -> a
`reply-draft` record (status=draft). A HUMAN reviews, edits, approves, and posts — this crew NEVER posts,
replies, or contacts anyone.

The loop + dedup are deterministic code; only the reply prose is the LLM's job, and it follows the
playbook: value first, disclose the builder, mention AIMEAT only when it truly fits, never astroturf.
"""

from __future__ import annotations

import sys

from crewai.tools import tool

from crewaimeat.aimeat_crew import _aimeat_call
from crewaimeat.llm import get_llm
from crewaimeat.some_listener import _ORG_ID, _RADAR_WS

_MIN_FIT = 3  # skip low-fit opportunities

_PLAYBOOK = (
    "Rules (strict): (1) Value first — answer the actual question / add real insight; no marketing tone. "
    "(2) Mention AIMEAT ONLY if it truly fits this thread, and at most ~1 in 3 replies should mention it; "
    "if you do, disclose the author is the builder and keep it to ONE natural sentence. "
    "(3) Match the platform's norms; be concise, specific, technical, human. (4) Never astroturf — if a "
    "mention would feel salesy, leave it out entirely."
)
_AIMEAT = (
    "AIMEAT (aimeat.io) is an AI-native operating substrate for AI agents — shared persistent memory, task "
    "queues, identity, app/extension hosting, and multi-agent 'organisms' (shared, versioned, auditable "
    "workspaces). The author runs an autonomous AI newspaper on it and dogfoods project coordination there."
)


def _read(space_key: str):
    """Return the published records of one space, or None if the workspace isn't accessible."""
    data = _aimeat_call("some-analyst", "aimeat_workspace_read", {"organism_id": _ORG_ID, "ws": _RADAR_WS})
    if not data or data.get("manifest") is None:
        return None
    return data.get("objects", {}).get(space_key, []) or []


def _draft_reply(opp: dict, llm) -> tuple[str, str]:
    prompt = (
        "You are helping a builder draft a GENUINELY HELPFUL reply to an online discussion.\n\n"
        f"{_PLAYBOOK}\n\nCONTEXT — {_AIMEAT}\n\n"
        "THREAD:\n"
        f"- platform: {opp.get('source')}\n"
        f"- title: {opp.get('title')}\n"
        f"- what it's about: {opp.get('summary')}\n"
        f"- url: {opp.get('url')}\n"
        f"- triage angle: {opp.get('angle')}\n"
        f"- mention-AIMEAT hint: spam_risk={opp.get('spam_risk')} "
        "(welcome = a mention is likely fine; borderline = lead with value, mention only if it flows; "
        "no = do NOT mention AIMEAT at all)\n\n"
        "Write ONLY the reply (plain text, no preamble, ready to paste). Then a final line:\n"
        "APPROACH: <one line — the angle, and whether you mentioned AIMEAT and why>"
    )
    out = (llm.call([{"role": "user", "content": prompt}]) or "").strip()
    approach = opp.get("angle", "")
    if "APPROACH:" in out:
        body, _, ap = out.rpartition("APPROACH:")
        out, approach = body.strip(), ap.strip()
    return out, approach


def draft_opportunities(limit: int = 5) -> dict:
    """Draft a reply-draft for each fresh, worth-it Social Radar opportunity. Human approves + posts.

    Candidates: status=new, spam_risk != 'no' (skip hostile), fit_score >= 3, not already drafted.
    Deterministic loop + dedup; the LLM only writes the reply prose. Never posts anything.
    """
    opps = _read("opportunity")
    if opps is None:
        print("[some-analyst] Social Radar not accessible to this agent — skipping.", file=sys.stderr)
        return {"drafted": 0, "failed": 0, "candidates": 0, "no_access": True}
    existing = {d.get("id") for d in (_read("reply-draft") or [])}
    candidates = [
        o for o in opps
        if o.get("status") == "new"
        and o.get("spam_risk") != "no"
        and int(o.get("fit_score") or 0) >= _MIN_FIT
        and f"draft-{o.get('id')}" not in existing
    ]
    candidates.sort(key=lambda o: int(o.get("fit_score") or 0), reverse=True)
    candidates = candidates[:limit]
    if not candidates:
        return {"drafted": 0, "failed": 0, "candidates": 0}

    llm = get_llm(for_tool_use=False, temperature=0.6)
    drafted = failed = 0
    for opp in candidates:
        oid = opp["id"]
        did = f"draft-{oid}"
        try:
            text, approach = _draft_reply(opp, llm)
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"[some-analyst] draft FAILED for {oid}: {exc!r}", file=sys.stderr)
            continue
        rec = {"id": did, "opportunity_ref": oid, "platform": opp.get("source", ""),
               "draft": text, "angle": approach, "status": "draft"}
        wrote = _aimeat_call("some-analyst", "aimeat_workspace_write",
                             {"organism_id": _ORG_ID, "ws": _RADAR_WS, "space": "reply-draft",
                              "id": did, "value": rec})
        pub = _aimeat_call("some-analyst", "aimeat_workspace_publish",
                           {"organism_id": _ORG_ID, "ws": _RADAR_WS,
                            "namespace": "shared.drafts", "id": did}) if wrote else None
        if wrote and pub:
            drafted += 1
        else:
            failed += 1
            print(f"[some-analyst] reply-draft write FAILED for {did} "
                  f"(write={bool(wrote)}, publish={bool(pub)})", file=sys.stderr)
    return {"drafted": drafted, "failed": failed, "candidates": len(candidates)}


def make_analyst_tools(agent_name: str) -> list:
    """The single drafting tool. It writes DRAFTS only — a human approves + posts; nothing auto-posts."""

    @tool("draft_opportunities")
    def _draft(limit: int = 5) -> str:
        """Read fresh Social Radar opportunities (status=new, fit>=3, not hostile, not already drafted) and
        draft a value-first reply-draft (status=draft) for each. A HUMAN reviews + approves + posts — this
        never posts or contacts anyone. Returns the counts."""
        res = draft_opportunities(limit=limit)
        if res.get("no_access"):
            return "Social Radar not accessible to this agent yet — drafted nothing."
        return (f"some-analyst: drafted {res.get('drafted', 0)} reply-drafts "
                f"({res.get('failed', 0)} failed, {res.get('candidates', 0)} candidates). "
                "Review + approve + post in the Social Radar workspace.")

    return [_draft]

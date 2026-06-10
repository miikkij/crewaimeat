"""activity-reporter: a DETERMINISTIC workspace-contract that turns the activity feed into reports.

Contract:
  inputs : `activity-tracking` (records) — config for what/when to report.
             { id, ws (a workspace id, or "*" for ALL the agent's member workspaces = the organism delta),
               period_hours(int, default 168=weekly), narrator?, since?, last_run?,
               status: requested | active | in-progress | done | failed }
           A record is DUE when status=='requested' (one-shot) OR status=='active' and now-last_run>=period.
  outputs: `activity-report` (DOCUMENT) — a per-period report that renders as a page:
             ## Digest · ## Shipped / changes · ## Who did what · ## The story (narrated by a character).
           Reusable downstream: standup/digest · changelog/release notes · build-in-public (-> SOME pipeline)
           · attribution/credit · a running project log.
  lifecycle: in-progress -> write report doc -> a 'requested' record becomes done; an 'active' one stays
             active with last_run bumped (so it fires again next period).

It reads the workspace activity feed (GET /v1/organisms/:id/workspace/activity?ws=, member-gated — who/what/
when, derived from version history) via REST with the agent's OWN token (same pattern as the scheduler), and
distils it (owl-alpha) into the report. The loop + the delta math are plain code; only the prose is the LLM's
job. The agent posts nothing external — it only reads + writes the workspace.
"""

from __future__ import annotations

import datetime
import sys

import requests
from crewai.tools import tool

from crewaimeat.aimeat_crew import _aimeat_call, member_workspaces
from crewaimeat.generator_tool import _discover_owner, _token
from crewaimeat.llm import get_llm

AGENT = "activity-reporter"
IN_SPACE, IN_NS = "activity-tracking", "shared.activity_tracking"
OUT_SPACE, OUT_NS = "activity-report", "shared.activity_reports"  # a DOCUMENT space
_DEFAULT_NARRATOR = "the organism's resident chronicler — wry, vivid, but strictly factual"

# Runaway guard: config ids already reported in THIS daemon run. Prevents re-generating the same report
# every poll if a stale read keeps the config looking due (read-after-write lag). Resets on daemon restart.
_REPORTED: set[str] = set()


def _call(tool_name: str, payload: dict):
    return _aimeat_call(AGENT, tool_name, payload)


def _member_workspaces(org_id: str | None = None) -> list[tuple[str, str]]:
    """Workspaces this agent serves (organism_list + AIMEAT_CONTRACT_ORGS home organisms)."""
    pairs = member_workspaces(AGENT)
    return [p for p in pairs if p[0] == org_id] if org_id else pairs


def _activity_events(org_id: str, ws: str) -> list[dict]:
    """Fetch the workspace activity feed (who/what/when).

    Primary path: the loopback serve daemon's /v1/* REST proxy (keep-alive Session; the daemon
    holds the token and rides its persistent WS tunnel). Fallback: direct REST with the agent's
    own token, for environments without the daemon."""
    try:
        from crewaimeat.aimeat_crew import _serve_api
        api = _serve_api()
        if api is not None:
            base, session = api
            r = session.get(
                f"{base}/v1/organisms/{org_id}/workspace/activity",
                params={"ws": ws}, headers={"X-Aimeat-Agent": AGENT}, timeout=30,
            )
        else:
            owner = _discover_owner(AGENT)
            tok, url = _token(AGENT, owner)
            if not tok or not url:
                return []
            r = requests.get(
                f"{url.rstrip('/')}/v1/organisms/{org_id}/workspace/activity",
                params={"ws": ws}, headers={"Authorization": f"Bearer {tok}"}, timeout=30,
            )
        evs = ((r.json() or {}).get("data") or {}).get("events") or []
        for e in evs:
            e["ws"] = ws
        return evs
    except Exception:  # noqa: BLE001
        return []


def _gather(org_id: str, ws_spec: str, since: str) -> list[dict]:
    """Events since `since` for one ws, or for EVERY workspace in this organism if ws_spec=='*'.

    For "*" we list the host organism's workspaces DIRECTLY (workspace_list(org_id)) rather than filtering a
    pre-built member list — organism_list may not surface every org the agent can actually read, but a direct
    workspace_list(org_id) does, so the organism-delta covers all of its workspaces.
    """
    if ws_spec == "*":
        wl = _call("aimeat_workspace_list", {"organism_id": org_id}) or {}
        wss = [w["id"] for w in (wl.get("workspaces") or []) if w.get("id")]
    else:
        wss = [ws_spec]
    out: list[dict] = []
    for w in wss:
        out.extend(e for e in _activity_events(org_id, w) if (e.get("at") or "") >= since)
    out.sort(key=lambda e: e.get("at") or "", reverse=True)
    return out


def _distill(events: list[dict], scope: str, since: str, narrator: str) -> str:
    lines = [
        f"- {e.get('at','')} · {(e.get('agent') or e.get('actor') or '?')} {e.get('action')} "
        f"{e.get('type')}/{e.get('instance')}" + (f" [{e.get('ws')}]" if e.get('ws') else "")
        for e in events
    ]
    feed = "\n".join(lines[:250]) if lines else "(no activity in this window)"
    prompt = (
        f"You are {narrator}. Below is the raw activity feed for {scope} since {since} "
        f"({len(events)} events — who did what, when; publish vs draft, per record).\n\n{feed}\n\n"
        "Write a markdown ACTIVITY REPORT with EXACTLY these sections:\n"
        "## Digest\n(3-5 sentences: what changed, the themes, the momentum)\n\n"
        "## Shipped / changes\n(bullets of concrete items created/published, grouped by type)\n\n"
        "## Who did what\n(brief attribution per agent/actor)\n\n"
        f"## The story\n(a short, vivid narrative IN CHARACTER as {narrator}, telling what happened over this "
        "period like a tale — factual, using only the events above, but with character and color)\n\n"
        "Use ONLY the events above. If nothing happened in the window, say so plainly and keep it short."
    )
    llm = get_llm(for_tool_use=False, temperature=0.5, agent_name=AGENT)
    return (llm.call([{"role": "user", "content": prompt}]) or "").strip()


def _advance(org_id: str, ws_id: str, rec: dict, **changes) -> None:
    out = {k: v for k, v in {**rec, **changes}.items() if not k.startswith("_")}
    if _call("aimeat_workspace_write", {"organism_id": org_id, "ws": ws_id, "space": IN_SPACE, "id": out["id"], "value": out}):
        _call("aimeat_workspace_publish", {"organism_id": org_id, "ws": ws_id, "namespace": IN_NS, "id": out["id"]})


def process_activity_reports(targets: list[tuple[str, str]] | None = None) -> dict:
    """Generate activity reports for DUE `activity-tracking` records across the agent's member workspaces.

    Deterministic: read config records -> for each DUE one, gather the activity delta (since last_run/period)
    -> distil (owl-alpha) -> write an `activity-report` document -> advance the config. `targets` restricts to
    (org, ws) pairs (else auto-discover). The delta math + loop use NO LLM; only the report prose does.
    """
    member_ws = _member_workspaces()
    pairs = targets if targets is not None else member_ws
    now = datetime.datetime.now(datetime.timezone.utc)
    nowiso = now.isoformat()
    made = failed = 0
    for org_id, ws_id in pairs:
        d = _call("aimeat_workspace_read", {"organism_id": org_id, "ws": ws_id})
        if not d or d.get("manifest") is None:
            continue
        cfgs = (d.get("objects", {}) or {}).get(IN_SPACE) or []
        existing_reports = {r.get("id") for r in ((d.get("objects", {}) or {}).get(OUT_SPACE) or [])}
        for rec in cfgs:
            status, rid = rec.get("status"), rec.get("id")
            if not rid:
                continue
            period_h = int(rec.get("period_hours") or 168)
            last = rec.get("last_run")
            due = status == "requested"
            if not due and status == "active":
                if not last:
                    due = True  # active but never run -> due now (the first report)
                else:
                    try:
                        due = (now - datetime.datetime.fromisoformat(last)).total_seconds() >= period_h * 3600
                    except Exception:  # noqa: BLE001
                        due = True
            if not due:
                continue
            if rid in _REPORTED:  # already generated this run — guard against a stale 'due' read
                continue
            if f"report-{rid}-{nowiso[:10]}" in existing_reports:  # already reported today -> output-dedup
                _REPORTED.add(rid)
                continue
            _REPORTED.add(rid)
            since = rec.get("since") or last or (now - datetime.timedelta(hours=period_h)).isoformat()
            ws_spec = rec.get("ws") or "*"
            scope = "the whole organism" if ws_spec == "*" else f"workspace '{ws_spec}'"
            _advance(org_id, ws_id, rec, status="in-progress")
            try:
                events = _gather(org_id, ws_spec, since)
                report = _distill(events, scope, since, rec.get("narrator") or _DEFAULT_NARRATOR)
            except Exception as exc:  # noqa: BLE001
                _advance(org_id, ws_id, rec, status="failed", error=repr(exc)[:300])
                failed += 1
                print(f"[{AGENT}] report FAILED for {rid}: {exc!r}", file=sys.stderr)
                continue
            if not report:
                _advance(org_id, ws_id, rec, status="failed", error="empty report")
                failed += 1
                continue
            out_id = f"report-{rid}-{nowiso[:10]}"
            title = f"Activity report · {scope} · {nowiso[:10]}"
            wrote = _call("aimeat_workspace_write",
                          {"organism_id": org_id, "ws": ws_id, "space": OUT_SPACE, "id": out_id,
                           "value": {"title": title, "markdown": report}})
            pub = _call("aimeat_workspace_publish",
                        {"organism_id": org_id, "ws": ws_id, "namespace": OUT_NS, "id": out_id}) if wrote else None
            if wrote and pub:
                _advance(org_id, ws_id, rec,
                         status=("done" if status == "requested" else "active"),
                         last_run=nowiso, last_report=out_id)
                made += 1
            else:
                _advance(org_id, ws_id, rec, status="failed", error="report write failed")
                failed += 1
    return {"reports": made, "failed": failed}


def make_activity_tools(agent_name: str) -> list:
    """The single report-generation tool. Reads the activity feed + writes report documents; posts nothing."""

    @tool("process_activity_reports")
    def _process() -> str:
        """Generate activity reports for any DUE activity-tracking config records in the agent's member
        workspaces: gather the activity delta (who did what since the last run / over the period), distil it
        into a digest + changelog + attribution + an in-character story, and write an activity-report
        document. Deterministic loop; never posts externally. Returns the counts."""
        res = process_activity_reports()
        return f"activity-reporter: wrote {res['reports']} report(s), {res['failed']} failed."

    return [_process]

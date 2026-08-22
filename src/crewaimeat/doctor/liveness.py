"""Lens 3 — LIVENESS. What the NODE believes, versus what this repo declares.

Lenses 1 and 2 read the repo. They cannot see that a schedule has been disabled since June, that three
enabled schedules belong to agents nobody reads, or that an agent's last_seen is 50 days old. Only the
node knows that, so this lens asks it.

A HARD RULE lives here: **this lens never reports "fine" when it could not look.** The connector's tool
surface returns empty lists off-fleet rather than failing, which is precisely the shape of a false
green — a check that cannot reach the node and prints nothing looks identical to a check that reached
it and found nothing wrong. So an unreachable node produces a `live.unreachable` finding and the lens
is recorded as SKIPPED in the report, never silently passed.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .inventory import Inventory
from .model import ERROR, WARN, Finding, Report

STALE_AGENT_DAYS = 30
STALE_SCHEDULE_DAYS = 30

# Schedules whose target is an interactive tool session (a Claude Desktop / goose / VS Code identity)
# are the owner's own calendar reminders, not fleet automation. They are reported, never judged.
TOOL_SESSION_HINT = ("claude", "goose", "openhands", "cursor", "vscode", "desktop", "chat")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age_days(stamp: str | None) -> int | None:
    if not stamp:
        return None
    try:
        dt = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (_now() - dt).days


def _probe_agent(inv: Inventory) -> str | None:
    """An agent identity to ask the node with — any registered one; its token is what authorises the
    read. Returns None when nothing is registered (then there is nothing to reconcile anyway)."""
    return next(iter(sorted(inv.served)), None)


def check(inv: Inventory, report: Report) -> None:
    agent = _probe_agent(inv)
    if not agent:
        report.lenses_skipped["live"] = "nothing registered in serve.json — no identity to ask the node with"
        return
    try:
        from crewaimeat.aimeat_crew import _aimeat_call
    except Exception as exc:  # noqa: BLE001
        report.lenses_skipped["live"] = f"cannot import the dispatcher ({exc})"
        return

    agents = _aimeat_call(agent, "aimeat_agents_list", {}) or {}
    roster = agents.get("agents") if isinstance(agents, dict) else None
    if not roster:
        report.lenses_skipped["live"] = (
            "the node returned no agent roster. Connector tools answer EMPTY (not an error) when the "
            "fleet is not attached, so this is reported rather than passed"
        )
        report.add(
            Finding(
                "live.unreachable",
                WARN,
                "aimeat_agents_list",
                "could not read the node's agent roster — the live lens did NOT run, so a clean report "
                "below says nothing about the node's actual state",
                "run `crewaimeat doctor --live` with the fleet attached (scripts/start_fleet.ps1)",
            )
        )
        return
    report.lenses_run.append("live")
    _agents(inv, roster, report)
    _schedules(inv, agent, report)
    _workflow_triggers(report)


def _agents(inv: Inventory, roster: list, report: Report) -> None:
    by_name = {a.get("name"): a for a in roster if isinstance(a, dict) and a.get("name")}
    report.note(f"node: {len(by_name)} agents, {len(inv.live_agents)} of them backed by a crew file here")

    for name in sorted(inv.live_agents - set(by_name)):
        report.add(
            Finding(
                "live.agent.absent",
                ERROR,
                name,
                "a live crew that the node has never seen — it is running locally against an identity "
                "that does not exist, so nothing it produces can arrive",
                "register it, or park the crew file",
            )
        )
    for name, rec in sorted(by_name.items()):
        if name not in inv.live_agents:
            continue
        age = _age_days(rec.get("last_seen"))
        if age is not None and age > STALE_AGENT_DAYS:
            report.add(
                Finding(
                    "live.agent.stale",
                    WARN,
                    name,
                    f"backed by a live crew but last seen {age} days ago — it is not attaching",
                    "check the fleet host log for this agent; its token may be stale",
                )
            )
        if not (rec.get("tags") or []):
            report.add(
                Finding(
                    "live.agent.untagged",
                    WARN,
                    name,
                    "has no tags on the node, so tag-based discovery cannot find it",
                    "add tags in fleet_identity, then restart the fleet (tags are set on start)",
                )
            )
    # Agents on the node that this repo knows nothing about. Not automatically wrong — the owner runs
    # other things — but this is where 20 near-duplicate experiments accumulated unnoticed.
    unknown = sorted(set(by_name) - inv.live_agents - inv.parked_agents - set(inv.served))
    if unknown:
        report.note(f"node holds {len(unknown)} agents unknown to this repo: {', '.join(unknown[:8])}…")


def _declared_triggers(inv: Inventory) -> dict[str, dict]:
    """agent -> the schedule the REPO says fires it (a crew's `SCHEDULE`, or a workflow's).

    A workflow declares ONE trigger for a whole chain, which is why the six per-agent Sanomat crons
    were replaced by one. Both shapes are collected here so the comparison below is against everything
    the repo actually claims, not just the per-crew half.
    """
    return {m.agent: dict(m.schedule) for m in inv.crews if m.agent and m.schedule}


def _workflow_triggers(report: Report) -> None:
    """Report what the repo says drives each WORKFLOW.

    A workflow's trigger lives in the node's workflow engine, NOT in `/v1/schedules`, so it is reported
    rather than reconciled here — demanding an agent_task row for it would manufacture a failure. What
    it buys is the thing that was missing: a reader can see, in the repo, that the evening paper is
    driven by one workflow trigger and not by the six per-agent crons that sat beside it for months.
    """
    try:
        from crewaimeat.workflow_spec import WORKFLOWS
    except Exception:  # noqa: BLE001
        return
    for wf in WORKFLOWS.values():
        sched = wf.get("schedule") or {}
        if sched.get("cron"):
            steps = wf.get("steps") or []
            report.note(
                f"workflow '{wf.get('id')}' declares its trigger {sched['cron']} ({sched.get('timezone')}) "
                f"for {len(steps)} steps starting at {(steps[0] or {}).get('agent', '?')}"
            )


def _schedules(inv: Inventory, agent: str, report: Report) -> None:
    """Reconcile the node's real schedule list against what the repo DECLARES fires each product.

    Without this, "which trigger drives the evening paper?" had no answer anyone trusted: six disabled
    crons sat beside a workflow, and the crons survived for months because deleting one might have
    stopped the paper. A declared trigger turns that into a diff.
    """
    # REST, not `aimeat_schedule_list`: the MCP tool's rows carry no target agent, so a comparison
    # built on them silently matched nothing and printed a clean report — the exact false green this
    # lens exists to prevent. `GET /v1/schedules` returns `agentName` on every row.
    from crewaimeat.aimeat_crew import _aimeat_rest

    data = _aimeat_rest(agent, "GET", "/v1/schedules")
    # An EMPTY list is a real answer ("this owner has no schedules"); a FAILED read is None, because
    # `_aimeat_rest` returns None and logs loudly on an HTTP error. Conflating the two would either
    # hide an unreachable node or invent an outage on a node that genuinely has nothing scheduled —
    # and the second is worse, since `live.schedule.missing` is an ERROR.
    if not isinstance(data, dict):
        report.add(
            Finding(
                "live.unreachable",
                WARN,
                "GET /v1/schedules",
                "could not read the node's schedule list, so nothing below says anything about which "
                "triggers are actually live",
                "run with the fleet attached",
            )
        )
        return
    schedules = data.get("managed") or []
    known = inv.live_agents | inv.parked_agents | set(inv.served)
    declared = _declared_triggers(inv)
    seen: set[str] = set()

    for s in schedules:
        if not isinstance(s, dict):
            continue
        name = s.get("displayName") or s.get("display_name") or s.get("id") or "?"
        enabled = bool(s.get("enabled"))
        age = _age_days(s.get("lastRunAt") or s.get("last_run_at"))
        target = str(s.get("agentName") or s.get("agent") or s.get("target_agent") or "")

        if not enabled and (age is None or age > STALE_SCHEDULE_DAYS):
            report.add(
                Finding(
                    "live.schedule.abandoned",
                    WARN,
                    name,
                    f"disabled and last run {age if age is not None else 'never'} days ago — a leftover "
                    "that makes it impossible to tell which trigger actually drives the product",
                    "delete it on the node, or re-enable it if it is still the real trigger",
                )
            )
            continue
        if not enabled or (s.get("type") or s.get("kind")) != "agent_task" or not target:
            continue

        seen.add(target)
        if target not in known:
            if any(h in target.lower() for h in TOOL_SESSION_HINT):
                report.note(
                    f"schedule '{name}' fires at the tool session '{target}' — an owner reminder, not fleet work"
                )
                continue
            report.add(
                Finding(
                    "live.schedule.orphan",
                    WARN,
                    name,
                    f"enabled and fires at '{target}', which has no crew file here — it burns model "
                    "calls on output nobody in this repo produces or reads",
                    f"crewaimeat retire {target}, or restore the agent",
                )
            )
            continue
        want = declared.get(target)
        if want is None:
            report.add(
                Finding(
                    "live.schedule.undeclared",
                    WARN,
                    f"{name} -> {target}",
                    "the node fires this agent on a schedule the repo says nothing about, so nobody "
                    "reading the code can tell what drives it",
                    f"declare SCHEDULE = {{'cron': '{s.get('cron')}', 'timezone': '{s.get('timezone')}'}} in the crew",
                )
            )
        elif want.get("cron") and s.get("cron") and want["cron"] != s.get("cron"):
            report.add(
                Finding(
                    "live.schedule.drifted",
                    WARN,
                    f"{name} -> {target}",
                    f"the crew declares cron '{want['cron']}' but the node runs '{s.get('cron')}'",
                    "change one of them deliberately — the declaration is what a reader will believe",
                )
            )

    for target, want in sorted(declared.items()):
        if target in seen or target not in inv.live_agents:
            continue
        report.add(
            Finding(
                "live.schedule.missing",
                ERROR,
                target,
                f"the crew declares it is fired by '{want.get('cron')}' ({want.get('purpose') or 'no purpose given'}), "
                "but the node has no enabled schedule for it — whatever it produces is not being produced",
                "create the schedule on the node, or remove the declaration if it is no longer true",
            )
        )

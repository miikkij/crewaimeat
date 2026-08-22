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


def _schedules(inv: Inventory, agent: str, report: Report) -> None:
    from crewaimeat.aimeat_crew import _aimeat_call

    data = _aimeat_call(agent, "aimeat_schedule_list", {}) or {}
    schedules = data.get("schedules") if isinstance(data, dict) else None
    if not schedules:
        return
    known = inv.live_agents | inv.parked_agents | set(inv.served)
    for s in schedules:
        if not isinstance(s, dict):
            continue
        name = s.get("display_name") or s.get("id") or "?"
        enabled = bool(s.get("enabled"))
        age = _age_days(s.get("last_run_at"))
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
        if enabled and s.get("kind") == "agent_task":
            target = str(s.get("agent") or s.get("target_agent") or "")
            if target and target not in known:
                report.add(
                    Finding(
                        "live.schedule.orphan",
                        WARN,
                        name,
                        f"enabled and fires at '{target}', which has no crew file here — it burns model "
                        "calls on output nobody in this repo produces or reads",
                        "disable the schedule, or restore the agent",
                    )
                )

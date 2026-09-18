"""When an agent works on its own: the node's `agent_task` schedules, in words a person picks.

The NODE owns the clock (`POST /v1/schedules`, kind `agent_task`): on every fire it puts a task in
the agent's queue, so a schedule fires even while this machine is off and the agent picks the task up
when it is back. agency 2.0 only writes and reads those records, as the agent itself.

The cron expression is BUILT HERE, from a preset and a time — never written by a model, and never
shown to the person. `describe()` turns the cron back into words for the presets it can make; any
other cron (made on the node) is shown as it is rather than guessed at.

Scopes: creating an agent_task schedule needs `task:write`, listing needs `workflow:read`
(services/schedule-gate.ts, routes/schedules.ts). Both are in `connect.REQUIRED_SCOPES`.
"""

from __future__ import annotations

import re

from crewaimeat.agency2 import node

PRESETS = ("daily", "weekdays", "weekly", "hourly")
_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_DAYS = {
    "fi": [
        "maanantaisin",
        "tiistaisin",
        "keskiviikkoisin",
        "torstaisin",
        "perjantaisin",
        "lauantaisin",
        "sunnuntaisin",
    ],
    "en": ["on Mondays", "on Tuesdays", "on Wednesdays", "on Thursdays", "on Fridays", "on Saturdays", "on Sundays"],
}


class ScheduleError(ValueError):
    """A refusal the person can act on."""


def build_cron(preset: str, time: str = "07:00", weekday: int = 1) -> str:
    """preset + 'HH:MM' (+ ISO weekday 1=Mon..7=Sun for weekly) -> a 5-field cron."""
    if preset == "hourly":
        return "0 * * * *"
    m = _TIME_RE.match((time or "").strip())
    if not m:
        raise ScheduleError("the time must look like 07:30")
    h, mi = int(m.group(1)), int(m.group(2))
    if preset == "daily":
        return f"{mi} {h} * * *"
    if preset == "weekdays":
        return f"{mi} {h} * * 1-5"
    if preset == "weekly":
        if not 1 <= int(weekday) <= 7:
            raise ScheduleError("the weekday must be 1 (Monday) … 7 (Sunday)")
        return f"{mi} {h} * * {int(weekday) % 7}"  # cron: 0 = Sunday
    raise ScheduleError(f"unknown choice: {preset!r}")


def describe(cron: str, lang: str = "fi") -> str:
    """Words for the crons `build_cron` makes; anything else is returned as it is."""
    parts = (cron or "").split()
    if parts == ["0", "*", "*", "*", "*"]:
        return "joka tasatunti" if lang == "fi" else "every hour, on the hour"
    if len(parts) == 5 and parts[0].isdigit() and parts[1].isdigit() and parts[2:4] == ["*", "*"]:
        hhmm = (
            f"{int(parts[1]):02d}.{int(parts[0]):02d}" if lang == "fi" else f"{int(parts[1]):02d}:{int(parts[0]):02d}"
        )
        at = f"klo {hhmm}" if lang == "fi" else f"at {hhmm}"
        dow = parts[4]
        if dow == "*":
            return f"joka päivä {at}" if lang == "fi" else f"every day {at}"
        if dow == "1-5":
            return f"arkisin {at}" if lang == "fi" else f"on weekdays {at}"
        if dow.isdigit() and 0 <= int(dow) <= 6:
            name = _DAYS.get(lang, _DAYS["en"])[(int(dow) - 1) % 7]
            return f"{name} {at}"
    return cron


def _records(data) -> list[dict]:
    if isinstance(data, dict):
        for k in ("schedules", "jobs", "items", "managed"):
            if isinstance(data.get(k), list):
                return data[k]
        return []
    return list(data or [])


def _agent_of(rec: dict) -> str | None:
    a = rec.get("agentName") or rec.get("agent_name")
    if a:
        return str(a)
    gaii = rec.get("agentGaii") or rec.get("agent_gaii") or ""
    return str(gaii).split("#")[0] or None


def list_for(agent: str, lang: str = "fi") -> list[dict]:
    """This agent's agent_task schedules, with the words and the task they create."""
    out = []
    for r in _records(node.call(agent, "aimeat_schedule_list")):
        if (r.get("type") or r.get("kind")) != "agent_task" or _agent_of(r) != agent:
            continue
        tmpl = r.get("taskTemplate") or r.get("task_template") or (r.get("input") or {}).get("taskTemplate") or {}
        if not isinstance(tmpl, dict) or not (tmpl.get("description") or tmpl.get("title")):
            tmpl = {"title": r.get("purpose") or r.get("displayName") or ""}
        out.append(
            {
                "id": r.get("id"),
                "cron": r.get("cron"),
                "when": describe(r.get("cron") or "", lang),
                "timezone": r.get("timezone") or r.get("effectiveTimezone"),
                "enabled": r.get("enabled", True),
                "what": (tmpl.get("description") or tmpl.get("title") or "") if isinstance(tmpl, dict) else "",
                "last_run": r.get("lastRunAt") or r.get("last_run_at"),
                "next_run": r.get("nextRunAt") or r.get("next_run_at"),
            }
        )
    return out


def create(agent: str, *, preset: str, time: str, weekday: int, what: str, timezone: str) -> dict:
    """The WHOLE instruction goes in `task_title`: the connector's door for this tool takes no task
    description (its schema: kind, cron, display_name, timezone, purpose, task_title, agent_name…;
    `task_template` is refused with UNKNOWN_PARAMETER — measured 2026-09-18 on 3.17.0), and the crew
    reads the task's title as its request. `purpose` carries it too, for the owner's scheduler view."""
    what = (what or "").strip()
    if not what:
        raise ScheduleError("write what the agent should do at that time")
    cron = build_cron(preset, time, weekday)
    return node.call(
        agent,
        "aimeat_schedule_create",
        {
            "kind": "agent_task",
            "agent_name": agent,
            "cron": cron,
            "timezone": timezone or "Europe/Helsinki",
            "display_name": f"{agent}: {describe(cron, 'fi')}",
            "purpose": what,
            "task_title": what,
        },
    )


def set_enabled(agent: str, schedule_id: str, enabled: bool) -> dict:
    return node.call(agent, "aimeat_schedule_update", {"schedule_id": schedule_id, "enabled": bool(enabled)})


def delete(agent: str, schedule_id: str) -> dict:
    return node.call(agent, "aimeat_schedule_delete", {"schedule_id": schedule_id})

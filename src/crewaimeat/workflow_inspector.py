"""workflow-inspector — the crew-side handler invoked when a workflow step's signal goes RED.

Three tiers (the answer to "can the crew side do more than analysis?" — yes):
  1. AUTO-REPAIR (deterministic, safe): the FIRST step that is output-RED while its input is GREEN
     is re-run directly (the stage functions are idempotent — output-existence dedup + local_marks).
     An input-RED step is NOT re-run — its upstream is the real problem; fix upstream first.
  2. DIAGNOSE + RECOMMEND: whatever a re-run can't turn GREEN is classified (input-RED vs
     output-RED, the failing agent's task status, the daemon evidence) into a step-by-step report.
  3. ESCALATE: anything needing a workflow-rule change or human decision is surfaced, not acted on.

Output: a `workflow-run` record dict + a markdown report (for a memory key and the morning section),
so a broken edition is NEVER silent. Runs deterministically; an LLM is used only for an `llm` signal
leaf (and the report prose if desired), never for the orchestration.
"""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

from crewaimeat.aimeat_crew import _aimeat_call
from crewaimeat.workflow_spec import WORKFLOWS, _default_reader, check_workflow, loc

AGENT = "workflow-inspector"
_TZ = ZoneInfo("Europe/Helsinki")
_MAX_RERUNS = 4  # bounded: one workflow has at most a handful of steps


def _rerun_step(step_id: str, date: str, edition: str) -> str:
    """Deterministically re-run a step's stage function. Idempotent (output-existence dedup).
    Returns a short report line. The node engine would re-dispatch the agent_task instead; here
    the inspector calls the stage directly as the safe repair."""
    if step_id == "fetch":
        from crewaimeat.fetch_pipeline import build_edition_raw

        return build_edition_raw("news-fetcher", date, edition)
    if step_id in ("write-a", "write"):  # "write" kept for older single-desk definitions
        from crewaimeat.write_pipeline import DESK_A, DESK_B, write_edition_articles

        a = write_edition_articles("news-writer", date, edition, DESK_A)
        if step_id == "write":
            b = write_edition_articles("news-writer-b", date, edition, DESK_B)
            return f"A: {a} | B: {b}"
        return a
    if step_id == "write-b":
        from crewaimeat.write_pipeline import DESK_B, write_edition_articles

        return write_edition_articles("news-writer-b", date, edition, DESK_B)
    if step_id == "space-weather":
        from crewaimeat.space_weather_pipeline import write_space_weather

        return write_space_weather("space-weather-writer", date, edition)
    if step_id == "features":
        from crewaimeat.features_pipeline import build_quiz

        return build_quiz("daily-features-writer", date, edition)
    if step_id == "editorial":
        from crewaimeat.editorial_pipeline import build_editorial_and_index

        return build_editorial_and_index("editorial-writer", date, edition)
    return f"no re-run adapter for step {step_id}"


def _agent_task_state(agent: str) -> str:
    """Best-effort: the failing agent's most recent task status (stalled/failed = a real signal)."""
    for st in ("stalled", "failed", "active"):
        r = _aimeat_call(agent, "aimeat_task_list", {"status": st, "per_page": 1}) or {}
        ts = r.get("tasks") or (r.get("data") or {}).get("tasks") or []
        if ts:
            return f"{agent}: latest task {st}"
    return f"{agent}: no stalled/failed task"


def inspect(wf_id: str, params: dict, *, repair: bool = True, lister=None) -> dict:
    """Run the three-tier inspection for one workflow run. Returns a result dict with a markdown
    report, the per-step states, what was auto-repaired, and what remains RED."""
    wf = WORKFLOWS[wf_id]
    vars = {v["name"]: params.get(v["name"], v.get("default")) for v in wf["vars"]}
    vars.update(params)
    date, edition = vars.get("date"), vars.get("edition")
    lister = lister or _default_reader("news-fetcher")
    steps_by_id = {s["id"]: s for s in wf["steps"]}

    actions: list[str] = []
    # Tier 1 — walk in order; re-run the FIRST output-RED-but-input-GREEN step, then re-check all.
    reruns = 0
    while reruns < _MAX_RERUNS:
        run = check_workflow(wf_id, vars, lister=lister)
        # Re-run the first OUTPUT-RED step (its input is GREEN by definition of the state); an
        # input-RED step is left for its upstream to fix first.
        target = next((s for s in run["steps"] if s["state"] == "output-RED"), None)
        if not target or not repair:
            break
        sid = target["id"]
        actions.append(f"Tier-1 re-run `{sid}` (output-RED, input-GREEN) …")
        try:
            rep = _rerun_step(sid, date, edition)
            actions.append(f"  → {str(rep)[:160]}")
        except Exception as exc:  # noqa: BLE001
            actions.append(f"  → re-run FAILED: {exc!r}")
            break
        reruns += 1

    final = check_workflow(wf_id, vars, lister=lister)
    fixed = [s["id"] for s in final["steps"] if s["state"] == "GREEN"]
    still_red = [s for s in final["steps"] if s["state"] != "GREEN"]

    # Tier 2 — classify what remains RED.
    diagnoses: list[str] = []
    for s in still_red:
        step = steps_by_id[s["id"]]
        agents = step.get("agent")
        agents = agents if isinstance(agents, list) else [agents]
        if s["state"] == "input-RED":
            ups = ", ".join(step.get("after") or []) or "—"
            diagnoses.append(
                f"**{s['id']}** input-RED → blocked on upstream ({ups}); fix that first. "
                f"Observed: {s['input']['observed']}"
            )
        else:  # output-RED after re-runs exhausted
            states = "; ".join(_agent_task_state(a) for a in agents)
            diagnoses.append(
                f"**{s['id']}** output-RED after {reruns} re-run(s) → not a transient miss. "
                f"Evidence: {states}. Observed: {s['output']['observed']}. "
                f"Likely: model unreachable / daemon crash-looping / stage bug — needs attention."
            )

    overall = "GREEN" if not still_red else "RED"
    report = _report_md(wf, vars, final, actions, diagnoses, overall)
    return {
        "workflow": wf_id,
        "date": date,
        "edition": edition,
        "overall": overall,
        "fixed": fixed,
        "still_red": [s["id"] for s in still_red],
        "steps": final["steps"],
        "actions": actions,
        "report_md": report,
    }


def _report_md(wf, vars, final, actions, diagnoses, overall) -> str:
    icon = {"GREEN": "✅", "input-RED": "⛔(input)", "output-RED": "⛔(output)"}
    lines = [
        f"## Workflow: {loc(wf['title'])} — {vars.get('date')} {vars.get('edition')}  [{overall}]",
        "",
        "**Steps:**",
    ]
    sbyid = {s["id"]: s for s in wf["steps"]}
    for s in final["steps"]:
        desc = loc(sbyid[s["id"]].get("description", ""))
        obs = s["output"]["observed"] if s["state"] != "input-RED" else s["input"]["observed"]
        lines.append(f"- {icon.get(s['state'], s['state'])} **{s['id']}** — {desc}  \n  _{obs}_")
    if actions:
        lines += ["", "**Auto-repair (Tier 1):**"] + [f"- {a}" for a in actions]
    if diagnoses:
        lines += ["", "**Diagnosis + recommendation (Tier 2/3):**"] + [f"- {d}" for d in diagnoses]
    if overall == "GREEN":
        lines += ["", "_Edition healthy after inspection._"]
    return "\n".join(lines)


def publish_inspection(result: dict, *, org: str | None = None, ws: str | None = None) -> dict:
    """Persist the inspection: a `workflow-run` record (memory) + a morning-report section so the
    outcome is never silent. Memory-key fallback when no workspace is wired."""
    date, wf = result["date"], result["workflow"]
    run_key = f"workflows.{wf}.runs.{date}"
    _aimeat_call(
        AGENT,
        "aimeat_memory_write",
        {
            "key": run_key,
            "visibility": "owner",
            "value": {
                "workflow": wf,
                "date": date,
                "overall": result["overall"],
                "steps": result["steps"],
                "fixed": result["fixed"],
                "still_red": result["still_red"],
            },
        },
    )
    now = datetime.datetime.now(_TZ).isoformat()
    title = f"Workflow watch · {wf} · {result['overall']}"
    _aimeat_call(
        AGENT,
        "aimeat_memory_write",
        {
            "key": "mail.morning.sections.workflow-inspector",
            "visibility": "owner",
            "value": {"title": title, "markdown": result["report_md"], "updated_at": now},
        },
    )
    return {"run_key": run_key, "overall": result["overall"]}

"""cadence-followup: a workspace-contract crew that watches a CADENCE CRM workspace and drafts
follow-up tasks.

It wakes on a workspace-record push (a contact/deal/activity/task change), reads the five CRM spaces,
runs the CADENCE follow-up watch-logic (mirrored from the Tier-1 cortex engine), and DRAFTS or
auto-creates `crm-task` follow-ups governed by an autonomy band (CADENCE_FOLLOWUP_BAND: propose=draft
only, auto=write+publish). The loop is deterministic (crewaimeat.cadence_contract) — no LLM. It never
contacts anyone, sends anything, or moves a deal; it only creates CRM task records for the owner.

Run as a crew:
  npx aimeat@latest connect add --agent cadence-followup --mode task-runner --url https://aimeat.io --owner <you>
  uv run python crews/cadence_followup_crew.py

Quick test (after registering + adopting the contract on a CRM workspace):
  uv run python -c "from crewaimeat.cadence_contract import process_cadence_followups; print(process_cadence_followups())"
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, contract_record_spaces, record_event_targets, run_crew
from crewaimeat.cadence_contract import CONTRACT, make_cadence_tools, process_cadence_followups
from crewaimeat.contract_adopt import build_adopt_domain, is_adopt_task

AGENT_NAME = "cadence-followup"

README = """[[FIGLET:slant]["Cadence Followup"]]

Watches a **CADENCE CRM** workspace and drafts **follow-up tasks** — the Tier-2 runtime of the same
watch-logic the app's Follow-up tab runs, so both agree on what needs attention. On a record change it
reads contacts/deals/activities/tasks and proposes a `crm-task` for each stale/cold/overdue
relationship or closing deal:

- **overdue task** · **cold contact** · **new lead with no task** · **negative-call follow-up** ·
  **closing-soon / stale / no-task deal**

Governed by an **autonomy band** (`CADENCE_FOLLOWUP_BAND`): `propose` (draft only — the owner reviews +
publishes) or `auto` (write + publish). Deterministic; **it never contacts anyone, sends anything, or
moves a deal — it only creates CRM task records for the owner to action.**

**How to task me:** "run a follow-up pass" — I scan the CRM once and draft any pending follow-ups.
"""


def build_domain(ctx: BuildContext):
    if is_adopt_task(ctx.task):  # UI "Adopt contract" chip -> provision/verify our spaces there
        return build_adopt_domain(ctx, AGENT_NAME, CONTRACT)
    assistant = Agent(
        role="CADENCE Follow-up Assistant",
        goal="Draft follow-up tasks for stale, cold and overdue CRM relationships — never contacting anyone.",
        backstory="You watch a CADENCE CRM workspace and turn its state into follow-up tasks: cold "
        "contacts, new leads with no task, poor-call follow-ups, and closing/stale deals. You call "
        "process_cadence_followups ONCE and report the counts. The scan is deterministic and mirrors "
        "the app's own follow-up logic. You never send anything or move a deal — you only create tasks.",
        llm=ctx.llm,
        tools=[*make_cadence_tools(AGENT_NAME)],
    )
    assistant_task = Task(
        description=(
            f"Today is {ctx.today}. Request: '{ctx.prompt}'\n\n"
            "Call process_cadence_followups() EXACTLY ONCE. It deterministically reads the CADENCE CRM "
            "records, runs the follow-up watch-logic, and drafts (or, under the auto band, creates) a "
            "crm-task for each surfaced proposal — deduplicated, so re-runs never duplicate. Report the "
            "counts. Contact NO ONE; send NOTHING."
        ),
        agent=assistant,
        expected_output="The process_cadence_followups report: how many follow-up tasks were drafted/created.",
    )
    return ([assistant], [assistant_task])


def run() -> None:
    # Event-driven: a pushed CRM record change (or the catch-up on connect) wakes us; the DETERMINISTIC
    # scan drafts any pending follow-ups (NO LLM in the check). targets scopes the scan to the event's
    # OWN workspace — no member rediscovery per event, and loop-safe (our own crm-task publish wakes a
    # single bounded read where the contact now has an open task -> suppressed -> no re-write).
    def _on_record(event) -> None:
        res = process_cadence_followups(targets=record_event_targets(event))
        if res.get("created") or res.get("failed"):
            print(f"[{AGENT_NAME}] cadence event: {res}")

    run_crew(
        CrewSpec(
            agent_name=AGENT_NAME,
            build_domain=build_domain,
            readme_md=README,
            listen_for=("tasks", "records"),
            record_spaces=lambda: contract_record_spaces(AGENT_NAME, CONTRACT),
            on_record=_on_record,
        )
    )


if __name__ == "__main__":
    run()

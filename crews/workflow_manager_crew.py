"""workflow-manager — give it a goal; it fans work out to other crews, gathers, and synthesizes.

This is the agent-driven coordinator (its OWN AIMEAT identity + memory, not a borrowed one). It:
  1. discovers which crews are available,
  2. delegates self-contained subtasks to the right ones (fan-out, via aimeat_task_create),
  3. waits for all their deliverables (fan-in, polling their memory),
  4. an Editor agent assembles everything into one finished deliverable, published to its memory.

Uses only existing primitives — no platform changes. The subtasks run on the OTHER crews, so this
agent never re-runs its own work. Register first, then approve in the dashboard:
  npx aimeat@latest connect add --agent workflow-manager --mode task-runner --url https://aimeat.io --owner <your-aimeat-account>

Run: uv run python crews/workflow_manager_crew.py
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, CrewSpec, run_crew
from crewaimeat.scheduler import make_schedule_tools
from crewaimeat.workflow import make_workflow_tools

AGENT_NAME = "workflow-manager"

# Infrastructure crews that are not content producers — hidden from delegation.
_NOT_DELEGABLE = ["crew-forge"]

README = '''[[FIGLET:slant]["WORKFLOW"]]

# workflow-manager — fan out, gather, synthesize

Give me a goal that several crews can contribute to. I look at which crews are available, delegate
the pieces to them, wait for their results, and assemble one finished deliverable.

## How to task me
Queue a goal, for example:
- `A one-page brief on idea X: rate its feasibility and list 5 ways it could play out`
- `A short fun bulletin about Tapiola, Espoo next week`
'''


def build_domain(ctx: BuildContext) -> tuple[list[Agent], list[Task]]:
    # Workers publish into the shared tag area agents.tag.workflow.<run>.* — assign the "workflow"
    # tag (Data Access -> Shared tags) to this agent AND every worker it delegates to.
    run_id = (ctx.task.get("id") or "manual").split("-", 1)[0]
    tools = make_workflow_tools(
        AGENT_NAME, run_id=run_id, task_id=ctx.task.get("id"), tag="workflow", exclude=_NOT_DELEGABLE,
        timeout=3600,  # 60 min: the coordinator waits for slow workers (commissioned crews, deep
        #              multi-search research) — its budget must exceed any single worker's runtime.
        directives=ctx.directives,  # so leaked owner-directive markers get stripped from delegated work
        llm=ctx.llm,         # the grounded judge that rates each worker's deliverable
        rate_workers=True,   # coordinator -> worker reputation rating (AIMEAT POST /tasks/:id/rate)
    )
    # Scheduler: for RECURRING goals (daily pipelines), set up AIMEAT server-run schedules (the node
    # owns the cron clock; fires offline; owner controls them in Profile -> Scheduler).
    sched_tools = make_schedule_tools(AGENT_NAME)

    dispatcher = Agent(
        role="Workflow Dispatcher",
        goal="Break the goal into self-contained subtasks, delegate each to the right crew, and collect every result",
        backstory=(
            "You orchestrate a fleet of specialist crews. You never do the domain work yourself — you "
            "decide who should do what, hand each crew a complete instruction, and gather their outputs. "
            "You delegate everything first, then collect once, so the crews work in parallel."
        ),
        tools=[*tools, *sched_tools],
        llm=ctx.llm,
        verbose=True,
    )
    editor = Agent(
        role="Editor",
        goal="Assemble the crews' contributions into one polished, well-structured deliverable for the goal",
        backstory=(
            "You are a sharp editor. You weave separate contributions into a single coherent result, "
            "keep what matters, and note any gap where a crew did not deliver."
        ),
        llm=ctx.llm,
        verbose=True,
    )

    dispatch = Task(
        description=(
            f"{ctx.today}\n\nGoal:\n{ctx.prompt}\n\n"
            "Work ONE tool call at a time. If the goal is genuinely ambiguous in a way that changes "
            "the work, call ask_owner(question, options) FIRST (do not guess); otherwise proceed:\n"
            "1. Call discover_crews to see which crews are available and what they do.\n"
            "2. Decide which crews can contribute. For each INDEPENDENT piece, call delegate_subtask("
            "target_agent, title, instruction) with a complete, self-contained prompt (the crew does NOT "
            "see this goal). These run in parallel. Delegate ONLY the work content — never copy your own "
            "standing directives into a crew's instruction; each crew applies its own directives.\n"
            "3. If a piece DEPENDS on another crew's output, call delegate_and_wait(target_agent, title, "
            "instruction) for the prerequisite first — it returns that crew's result — then paste that "
            "result into the dependent crew's instruction. Chain these for an A -> B pipeline. Only "
            "serialize real dependencies; keep independent work parallel via delegate_subtask.\n"
            "4. ADAPT to what you learn. When the goal asks you to act conditionally (e.g. 'if the rating "
            "is below 6, do X, otherwise do Y'), use delegate_and_wait to get the deciding result, read "
            "it, decide which branch the result calls for, and then delegate ONLY that branch (make the "
            "branch you chose clear in that subtask's title). You are not locked into a fixed plan — your "
            "next steps may depend on what earlier crews returned.\n"
            "5. If a needed capability has NO matching crew, call commission_crew(agent_name, capability) "
            "to have crew-forge build one, then wait_for_crew(agent_name), then delegate to it. "
            "Only do this for a genuine gap — prefer existing crews.\n"
            "5b. RECURRING / SCHEDULED goals (e.g. 'every morning fetch news and write an editorial'): do "
            "NOT run the pipeline yourself now — the AIMEAT scheduler runs it on the node's cron clock "
            "(fires even when crews are offline; the owner controls it in Profile -> Scheduler). Steps: "
            "(a) ENSURE the crews exist — for content production prefer commissioning DEDICATED crews from "
            "crew-forge (e.g. a multi-category WRITER that reads raw material from owner memory and writes "
            "own-words articles to PUBLIC keys, and a separate EDITORIAL writer that combines the articles "
            "into a styled editorial); commission_crew + wait_for_crew for any that are missing (forged "
            "crews come up in task-runner mode, so the scheduler can run them unattended). (b) For each "
            "stage call schedule_create(kind='agent_task', target_agent=<crew>, cron=<5-field>, "
            "timezone='Europe/Helsinki', display_name, purpose, task_title, task_description). The "
            "task_description is the per-fire instruction: name the EXACT memory keys to READ and the EXACT "
            "keys to WRITE (with the visibility — articles/editorials PUBLIC), the category, and the "
            "persona/style for that run (e.g. a Spider Jerusalem gonzo voice for the editorial). Schedules "
            "have NO run-after-dependency: STAGE by cron times (fetch earliest, writers later, editorial "
            "last) with buffer, and tell each downstream stage to READ its inputs first and STOP without "
            "fabricating if they are missing. (c) Report the schedules you created (schedule_list) and any "
            "crews you commissioned (the owner approves new ones in the dashboard; once approved the node "
            "runs them). Use schedule_update (enabled=false to pause) / schedule_delete to adjust.\n"
            "6. After delegating every parallel subtask, call collect_results ONCE to gather their outputs "
            "(results you already got from delegate_and_wait are included automatically).\n"
            "Then report all the collected materials verbatim as your result (or, for a scheduling goal, "
            "the schedules + commissioned crews you set up)."
        ),
        expected_output="The collected materials from every delegated crew.",
        agent=dispatcher,
    )
    compose = Task(
        description=(
            "Using the collected materials above, produce the FINAL polished deliverable that fulfils "
            f"the original goal:\n{ctx.prompt}\n\n"
            "Integrate the crews' contributions into one clean, well-structured result. If a crew "
            "returned no result, work with what arrived and briefly note the gap.\n\n"
            "FIDELITY: preserve every factual claim, name, number, date and source from the contributions "
            "EXACTLY as given. Do NOT add, infer, 'improve' or invent any fact, and never attach a source to "
            "anything not in the contributions. If only ONE crew contributed, return its output essentially "
            "verbatim (do not re-write it). Write creative connective prose only if the goal is itself creative."
        ),
        expected_output="The final deliverable for the goal, assembled from the crews' contributions.",
        agent=editor,
        context=[dispatch],
    )

    return [dispatcher, editor], [dispatch, compose]


def run() -> None:
    # adapt_to_task: classify each goal (fact/creative/mixed) -> cool+grounded+faithfulness-verified for
    # fact work, warm+free for creative. verify="on" is the fallback when the gate is inactive.
    run_crew(CrewSpec(
        agent_name=AGENT_NAME, build_domain=build_domain, readme_md=README,
        adapt_to_task=True, verify="on", score_to_stats=True,
    ))


if __name__ == "__main__":
    run()

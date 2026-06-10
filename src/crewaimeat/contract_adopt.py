"""adopt-contract: one-click workspace adoption for contract agents.

The AIMEAT UI's "Adopt contract" chip creates a task for the agent with scope fields
(convention agreed with the AIMEAT dev, docs §7c):

    POST /v1/agents/<agent>/tasks
    { title, description, scope: [ {name:"kind", value:"adopt-contract"},
                                   {name:"organism_id", value:...}, {name:"ws", value:...},
                                   {name:"contract", value:"<contract id>"} ] }

The agent recognizes the task by scope[kind] == "adopt-contract" (never by title) and then:
  1. joins the organism if needed (ALREADY_MEMBER is fine),
  2. provisions the contract's input+output spaces from its OWN embedded contract declaration
     (workspace_update add_spaces — additive, skip-if-exists, so adoption is idempotent),
  3. completes the task with a summary.

Each contract module embeds its declaration as a CONTRACT dict:
  { "id": "<capability name>", "spaces": [ {space, namespace, mode, schema?}, ... ] }
"""

from __future__ import annotations

from crewai import Agent, Task

from crewaimeat.aimeat_crew import BuildContext, _aimeat_call


def task_scope(task: dict) -> dict:
    """The task's scope fields as a {name: value} dict (empty when none)."""
    return {s.get("name"): s.get("value")
            for s in (task.get("scope") or []) if isinstance(s, dict) and s.get("name")}


def is_adopt_task(task: dict) -> bool:
    return task_scope(task).get("kind") == "adopt-contract"


def adopt_contract(agent: str, contract: dict, organism_id: str, ws: str) -> str:
    """Deterministically adopt this agent's contract into one workspace. Idempotent."""
    join = _aimeat_call(agent, "aimeat_organism_join", {"id": organism_id})  # ALREADY_MEMBER -> None, fine
    add = [{"name": s["space"], "namespace": s["namespace"], "mode": s["mode"]}
           for s in contract["spaces"]]
    schemas = {s["namespace"]: s["schema"] for s in contract["spaces"] if s.get("schema")}
    payload: dict = {"organism_id": organism_id, "ws": ws, "add_spaces": add}
    if schemas:
        payload["schemas"] = schemas
    r = _aimeat_call(agent, "aimeat_workspace_update", payload)
    if not r:
        return (f"FAILED: workspace_update returned nothing for {ws} — the agent may lack access to "
                f"organism {organism_id} (grant access first), or the workspace id is wrong.")
    return (f"contract '{contract['id']}' adopted into {ws}: "
            f"added={r.get('added')}, skipped={r.get('skipped')} (joined={bool(join)})")


def build_adopt_domain(ctx: BuildContext, agent_name: str, contract: dict):
    """build_domain branch for an adopt-contract task: one agent, one tool, one call."""
    scope = task_scope(ctx.task)
    organism_id, ws = scope.get("organism_id") or "", scope.get("ws") or ""

    from crewai.tools import tool

    @tool("adopt_contract")
    def _adopt() -> str:
        """Adopt this agent's workspace contract into the requested workspace: join the organism
        if needed and provision the contract's input+output spaces (idempotent). Returns a summary."""
        return adopt_contract(agent_name, contract, organism_id, ws)

    adopter = Agent(
        role="Contract Adopter",
        goal="Adopt this agent's workspace contract into the requested workspace.",
        backstory="You handle adopt-contract tasks: you call the adopt_contract tool EXACTLY ONCE "
                  "and report its result verbatim. The tool is deterministic and idempotent.",
        llm=ctx.llm,
        tools=[_adopt],
    )
    adopt_task = Task(
        description=(f"Adopt the '{contract['id']}' contract into workspace {ws} of organism "
                     f"{organism_id}. Call adopt_contract EXACTLY ONCE and report its result."),
        agent=adopter,
        expected_output="The adopt_contract result line (added/skipped spaces).",
    )
    return ([adopter], [adopt_task])

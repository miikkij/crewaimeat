"""The concierge proposes a NEW AGENT on the node, instead of recommending an outside product.

What went wrong. On 2026-10-02 the owner of a freshly bought place asked their concierge for "an agent
that every morning gathers the CRM's open deals and tells me what to act on. Propose one." After 196 s it
answered with an essay recommending CrewAI Studio and naming HubSpot, Salesforce and Pipedrive. The owner's
own CRM (CADENCE, a workspace in their organism) went unmentioned, and nothing was proposed: the node's
proposal list stayed empty. Web search was the only tool it had, so it searched the web.

What this is. The node already makes, runs and credentials an agent in one owner press: an agent PROPOSES
(aimeat_agent_propose), the owner approves on the Agents page, and the node creates it, seeds its
definition and hands it to the connector. This module is the concierge's side of that road, split along
the line this repo draws everywhere: THE MODEL WRITES AND JUDGES, EVERYTHING ELSE IS CODE.

  The model decides: the name, a purpose that names the person's data, the agent's instructions in prose,
  which workspace it reads, which runtime tools it needs, and whether it runs on a clock.

  Code does the rest, and does it so the node cannot refuse it for a reason the model could get wrong:
    - the crew_def is ASSEMBLED, not written by the model: one agent, one task that takes {{ctx.prompt}},
      the task naming the agent that exists, tool ids only from the runtime's own registry -- the node's
      three INVALID_CREW_DEF rules hold by construction, and crewaimeat's own validator runs before sending;
    - the scopes are COMPUTED: what the tools need, plus what this runtime writes with on every run
      (agent_scopes.runtime_scopes: memory:write, and ai:use when the owner's default road is the node), CAPPED at what the concierge itself holds -- the node refuses a
      proposal wider than the proposer (SCOPE_ESCALATION), and what was left out is said, not dropped;
    - the node's answer is relayed in the node's words (`next_step`, `approval_url`), and a refusal in its
      own code and sentence. No second yes/no question: the press on the Agents page IS the approval.

THE CLOCK COMES AFTER THE APPROVAL. "Every morning" is an `agent_task` schedule for the new agent, and a
schedule for an agent that does not exist yet fails on its first run. So the intended schedule is kept in
the concierge's own memory, and `start_proposed` creates it only once the agent is on the node.

Contract: aimeat-protocol 0b24e1a63 -- routes/agents-v2/agent-proposals.ts, services/agent-proposals.ts,
tool-dispatch/tool-call-defs-agent.ts (aimeat_agent_propose on /local/call).
"""

from __future__ import annotations

import re
import sys
import time

from crewaimeat.agent_scopes import runtime_scopes
from crewaimeat.aimeat_crew import _aimeat_call, _aimeat_rest
from crewaimeat.workspace_tools import READ_SCOPES, WRITE_SCOPES

# Every proposed agent gets `decline_request`: asked for something outside its purpose, it says so and the
# task ends as declined rather than done (crewaimeat.decline). It needs no scope, so it is not a choice.
ALWAYS_TOOLS = ("decline",)

# The node's own name rule (services/agent-proposals.ts NAME_SHAPE): 3-40 characters, lowercase letters,
# digits and hyphens, starting with a letter. Checked here so a bad name costs a sentence, not a call.
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{2,39}$")

# The runtime tools a proposed agent may be given, and the node scopes each one needs on top of what the
# runtime writes with. ONLY tools whose needs are known are offered: a tool here with the wrong scope list
# is an agent the owner approves and that then fails its first run (exactly the memory:read-only proposal
# the brief measured). Every id is also in crew_def.TOOL_REGISTRY, which a test holds.
#   memory      read/write exact memory keys             memory:read, memory:write
#   workspace   read organisms and workspaces by name     organism:read -- READING one needs it
#               (routes/organisms/workspace-read.ts); only the LIST is membership-gated. Assumed otherwise
#               from the list route alone, and the live test caught it: the approved agent could list CADENCE
#               and read nothing in it.
#   workspace_write  the above, plus create/update records    + memory:write, organism:write (the draft is a
#               and append rows                            memory write; publishing and rows take
#               organism:write). workspace_tools.READ_SCOPES / WRITE_SCOPES are the one source.
#   web         search the live web                       (no node call)
#   article_fetch  read the article behind a link         (no node call)
#   schedule    manage its own node schedules              workflow:read, task:write (schedule-gate.ts)
#   dm          read and reply in its inbox                messages:read, messages:send
PROPOSABLE_TOOLS: dict[str, tuple[str, ...]] = {
    "memory": ("memory:read", "memory:write"),
    "workspace": READ_SCOPES,
    "workspace_write": WRITE_SCOPES,
    "web": (),
    "article_fetch": (),
    "schedule": ("workflow:read", "task:write"),
    "dm": ("messages:read", "messages:send"),
}

# Where the concierge keeps a schedule it promised, until the agent it belongs to exists.
PENDING_PREFIX = "proposals.pending."

_CRON_FIELD = re.compile(r"^[0-9*/,\-]+$")
_BEYOND = re.compile(r"Beyond yours:\s*([^.]+)")


def _plain(text: str) -> str:
    """Model text going into a crew_def, with double braces flattened.

    `{{ctx.x}}` is the definition's own template syntax, and the runtime refuses a placeholder it does not
    know. A sentence the model wrote is prose, never a template, so it must not be able to make one.
    """
    return str(text or "").replace("{{", "{").replace("}}", "}").strip()


def _covered(held: list[str], scope: str) -> bool:
    if "*" in held or scope in held:
        return True
    return f"{scope.split(':', 1)[0]}:*" in held


def own_scopes(agent_name: str) -> list[str] | None:
    """What the concierge itself holds, read from the node (GET /v1/agents/{name}/refusals answers the
    agent itself with `granted_scopes`). None when the node could not say -- an older node without the
    route, or no answer -- and then nothing is capped here and the node's own ceiling decides."""
    from crewaimeat.agent_manifest import agent_local_name

    res = _aimeat_rest(
        agent_name, "GET", f"/v1/agents/{agent_local_name(agent_name)}/refusals", retries=2, return_error=True
    )
    if not isinstance(res, dict) or res.get("ok") is False:
        return None
    held = res.get("granted_scopes")
    return [str(s) for s in held] if isinstance(held, list) else None


def compute_scopes(tools: list[str], held: list[str] | None, *, node_road: bool = False) -> tuple[list[str], list[str]]:
    """(the scopes to ask for, the ones left out because the concierge does not hold them).

    Always memory:read (an agent reads its own work) and the runtime's write scopes -- with ai:use when
    the new agent will think through the node (`node_road`: the owner's default) -- then each tool's.
    """
    wanted: list[str] = []
    runtime = runtime_scopes(node_road=node_road)
    for s in ("memory:read", *runtime, *(s for t in tools for s in PROPOSABLE_TOOLS.get(t, ()))):
        if s not in wanted:
            wanted.append(s)
    if held is None:
        return wanted, []
    return [s for s in wanted if _covered(held, s)], [s for s in wanted if not _covered(held, s)]


def build_crew_def(
    *,
    name: str,
    display_name: str,
    purpose: str,
    instructions: str,
    tools: list[str],
    workspace: dict | None,
    delivers: str,
) -> dict:
    """One agent, one task -- valid for the node and the runtime by construction.

    The agent is keyed by its ROLE (no `name`), and the task names that role: both validators accept the
    role as the key when there is no name. The task's description ends with {{ctx.prompt}}, so every run
    answers what it was asked rather than the same thing every time.
    """
    role = _plain(display_name) or name
    data = ""
    if workspace:
        org = workspace.get("organism") or workspace["organism_id"]
        data = (
            f'Your data is the workspace "{workspace["name"]}" in the organism "{org}". Read its index first '
            f'with read_workspace(workspace="{workspace["name"]}", organism="{workspace["organism_id"]}"), then '
            "the records you need, before you answer. Name the records your answer is based on, and say so "
            "plainly when they hold nothing that answers the request.\n\n"
        )
    elif {"workspace", "workspace_write"} & set(tools):
        # No workspace was named when it was proposed: it finds them, it never guesses one (a proposed agent
        # tried "default", "owner" and "aimeat" as organism names on a hosted place, 2026-10-02).
        data = (
            "Your owner's data is in their workspaces. Call list_workspaces first and use the names it "
            "shows; never guess a workspace or organism name.\n\n"
        )
    if "workspace_write" in tools:
        data += (
            "When the request is to add or change something, look the record up first so you update it "
            "rather than create a duplicate, write it with write_workspace_record, and say which records "
            "you created or updated. Never claim a write the tool did not confirm.\n\n"
        )
    task = {
        "id": "run",
        "agent": role,
        "description": f"{_plain(instructions)}\n\n{data}The request for this run:\n{{{{ctx.prompt}}}}",
        "expected_output": _plain(delivers)
        or "A short answer in the language of the request, naming the records it is based on.",
    }
    agent = {
        "role": role,
        "goal": _plain(purpose),
        "backstory": (
            f"You are {role}, an agent on your owner's AIMEAT node. {_plain(instructions)} You work only from "
            "the data your owner keeps on this node and what your tools return; you never invent a record. "
            "When a request is outside what you do, or needs access you do not have, call decline_request with "
            "the reason and say who or what can do it instead."
        ),
        "tools": [*tools, *(t for t in ALWAYS_TOOLS if t not in tools)],
        "allow_delegation": False,
    }
    return {
        "agent_name": name,
        "process": "sequential",
        "readme_md": f"# {role}\n\n{_plain(purpose)}\n",
        "agents": [agent],
        "tasks": [task],
    }


def _parse_tools(tools: str | list[str]) -> list[str]:
    items = tools if isinstance(tools, list) else re.split(r"[,\s]+", tools or "")
    out: list[str] = []
    for t in items:
        t = str(t).strip().lower()
        if t and t not in out:
            out.append(t)
    return out


def _valid_cron(cron: str) -> bool:
    parts = (cron or "").split()
    return len(parts) == 5 and all(_CRON_FIELD.match(p) for p in parts)


def _relay(answer: dict, left_out: list[str], name: str, scheduled: str, assumption: str = "") -> str:
    """The node's own words first, then what only this side knows."""
    next_step = str(answer.get("next_step") or "").strip()
    url = str(answer.get("approval_url") or "").strip()
    lines = [next_step or f"I have proposed the agent {name}."]
    if url and url not in next_step:
        lines.append(f"Approve it here: {url}")
    if assumption:
        lines.append(assumption)
    if left_out:
        lines.append(
            f"I left out {', '.join(left_out)}: I do not hold {'it' if len(left_out) == 1 else 'them'} myself, "
            "so I cannot ask for it on the agent's behalf. You can give it on the agent's page after you approve."
        )
    if scheduled:
        lines.append(f'Once you have approved it, tell me "start {name}" and I will set it to run {scheduled}.')
    return "\n\n".join(lines)


def propose(
    agent_name: str,
    *,
    name: str,
    display_name: str,
    purpose: str,
    instructions: str,
    tools: str | list[str] = "",
    workspace: str = "",
    delivers: str = "",
    schedule_cron: str = "",
    timezone: str = "Europe/Helsinki",
    schedule_task: str = "",
) -> str:
    """Propose a new agent on the node and return what to tell the person. Never raises: a refusal is an
    answer the person needs to hear, in the node's words."""
    name = (name or "").strip().lower()
    if not NAME_RE.match(name):
        return (
            f"'{name}' cannot be an agent name: 3 to 40 characters, lowercase letters, digits and hyphens, "
            "starting with a letter. Pick another and call propose_agent again."
        )
    if len((purpose or "").strip()) < 10:
        return "Say what the agent is for in a full sentence the owner can decide from, then call propose_agent again."
    if schedule_cron and not _valid_cron(schedule_cron):
        return f"'{schedule_cron}' is not a 5-field cron (minute hour day month weekday), e.g. '0 7 * * *' for 07:00 daily."

    chosen = _parse_tools(tools)
    from crewaimeat.crew_def import TOOL_REGISTRY

    offered = sorted(t for t in PROPOSABLE_TOOLS if t in TOOL_REGISTRY)
    unknown = [t for t in chosen if t not in offered]
    if unknown:
        return f"I cannot give an agent {', '.join(unknown)}. The tools I can give: {', '.join(offered)}."
    if schedule_cron and "schedule" in chosen:
        # ASK ONLY FOR WHAT ITS RUNS CALL. "Every morning" is a clock the CONCIERGE sets once the agent
        # exists (start_proposed, POST /v1/agents/<name>/schedules with the concierge's own credential);
        # the agent's runs never call a schedule route. The `schedule` tool brought workflow:read and
        # task:write into a morning-brief proposal on a sold place (2026-10-03) for nothing.
        chosen = [t for t in chosen if t != "schedule"]
        print(
            f"[{agent_name}] propose {name}: the clock is set after approval, so the agent is not given the "
            "schedule tool (no workflow:read, no task:write)",
            file=sys.stderr,
        )

    found_ws = None
    no_workspace_yet = ""
    if workspace:
        from crewaimeat.workspace_tools import find_workspace, list_workspaces, render_workspaces

        seen = list_workspaces(agent_name)
        found_ws = find_workspace(seen, workspace)
        if found_ws is None and seen:
            return f"I cannot find a workspace called '{workspace}'. {render_workspaces(seen)}"
        if found_ws is None:
            # AN EMPTY NODE STILL GETS ITS PROPOSAL (ruling 2026-10-02): the person asked for one. The
            # agent reads memory until the workspace exists, and the reply says so as an assumption to
            # correct, instead of a question in place of the proposal.
            no_workspace_yet = (
                f"Your node has no workspace called '{workspace}' yet, so the agent reads memory until one "
                "exists. When you have created it, tell me and I attach the agent to it."
            )
            chosen = [t for t in chosen if t not in ("workspace", "workspace_write")]
        elif "workspace" not in chosen and "workspace_write" not in chosen:
            chosen.append("workspace")
    if not chosen:
        chosen = ["memory"]

    crew_def = build_crew_def(
        name=name,
        display_name=display_name or name,
        purpose=purpose,
        instructions=instructions,
        tools=chosen,
        workspace=found_ws,
        delivers=delivers,
    )
    from crewaimeat.crew_def import validate_crew_doc

    problems = validate_crew_doc(crew_def)
    if problems:  # assembled here, so this is a bug in us -- say it rather than send it
        return "I could not put together a definition the runtime accepts: " + "; ".join(problems)

    from crewaimeat.llm_choice import default_is_node_road

    scopes, left_out = compute_scopes(chosen, own_scopes(agent_name), node_road=default_is_node_road(agent_name))
    payload = {
        "name": name,
        "display_name": _plain(display_name) or name,
        "purpose": _plain(purpose),
        "scopes": scopes,
        "mode": "task-runner",  # its tasks start by themselves; the owner approves this with the agent
        "run_mode": "spawn",  # a worker per piece of work, nothing resident while it waits
        "crew_def": crew_def,
    }
    answer = _aimeat_call(agent_name, "aimeat_agent_propose", payload, return_error=True)
    if isinstance(answer, dict) and answer.get("ok") is False:
        err = answer.get("error") or {}
        beyond = _BEYOND.search(str(err.get("message") or ""))
        if err.get("code") == "SCOPE_ESCALATION" and beyond:
            # The brief's "correct the call once": the node named what is beyond the proposer, in its own
            # words. Ask again without exactly those, and say what was left out.
            drop = [s.strip() for s in beyond.group(1).split(",") if s.strip()]
            payload["scopes"] = [s for s in scopes if s not in drop]
            left_out = left_out + [s for s in drop if s not in left_out]
            answer = _aimeat_call(agent_name, "aimeat_agent_propose", payload, return_error=True)
    if answer is None:
        return "The node did not answer, so nothing was proposed. Try again in a moment."
    if isinstance(answer, dict) and answer.get("ok") is False:
        err = answer.get("error") or {}
        return f"The node did not accept the proposal ({err.get('code', 'refused')}): {err.get('message', '')}"

    scheduled = ""
    if schedule_cron:
        record = {
            "name": name,
            "display_name": payload["display_name"],
            "purpose": payload["purpose"],
            "cron": schedule_cron,
            "timezone": timezone or "Europe/Helsinki",
            "task_title": f"{payload['display_name']} — scheduled run",
            "task_description": _plain(schedule_task) or _plain(instructions),
            "approval_url": answer.get("approval_url"),
            "proposed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        saved = _aimeat_call(
            agent_name,
            "aimeat_memory_write",
            {"key": f"{PENDING_PREFIX}{name}", "value": record, "visibility": "owner"},
        )
        scheduled = f"on the schedule '{schedule_cron}' ({record['timezone']})" if saved is not None else ""
        if saved is None:
            return _relay(answer, left_out, name, "", no_workspace_yet) + (
                "\n\nI could not note the schedule you asked for. After approving, tell me again when it "
                "should run and I will set it."
            )
    return _relay(answer, left_out, name, scheduled, no_workspace_yet)


def _pending(agent_name: str, name: str) -> dict | None:
    r = _aimeat_call(agent_name, "aimeat_memory_read", {"key": f"{PENDING_PREFIX}{name}"}, quiet=True)
    val = r.get("value") if isinstance(r, dict) else None
    return val if isinstance(val, dict) else None


def start_proposed(agent_name: str, name: str) -> str:
    """Create the schedule promised for `name`, once the agent exists. Never before: a schedule for an agent
    that is not on the node yet fails on its first run."""
    name = (name or "").strip().lower()
    record = _pending(agent_name, name) if name else None
    if record is None:
        return f"I have no schedule waiting for an agent called '{name}'."

    from crewaimeat.orchestrator import list_node_agents

    names = {str(a.get("name")) for a in list_node_agents(agent_name) if isinstance(a, dict)}
    if name not in names:
        where = f" at {record['approval_url']}" if record.get("approval_url") else " on your Agents page"
        return f"{name} is not on your node yet. Approve it first{where}, then tell me to start it."

    body = {
        "kind": "agent_task",
        "cron": record["cron"],
        "timezone": record.get("timezone") or "Europe/Helsinki",
        "display_name": record.get("task_title") or f"{name} — scheduled run",
        "purpose": record.get("purpose") or f"Runs {name} on its schedule.",
        "task_template": {
            "title": record.get("task_title") or name,
            "description": record.get("task_description") or "",
        },
    }
    # The concierge's OWN token, the new agent's path: the node resolves the target under the caller's
    # owner, so a same-owner sibling can be scheduled without borrowing its credential (scheduler.py).
    res = _aimeat_rest(agent_name, "POST", f"/v1/agents/{name}/schedules", body, retries=2, return_error=True)
    if res is None:
        return "The node did not answer, so the schedule was not set. Try again in a moment."
    if isinstance(res, dict) and res.get("ok") is False:
        err = res.get("error") or {}
        return f"The node did not accept the schedule ({err.get('code', 'refused')}): {err.get('message', '')}"
    _aimeat_call(agent_name, "aimeat_memory_delete", {"key": f"{PENDING_PREFIX}{name}"}, quiet=True)
    return (
        f"Done: {name} now runs on the schedule '{body['cron']}' ({body['timezone']}). You can pause or "
        "cancel it in Profile > Scheduler."
    )

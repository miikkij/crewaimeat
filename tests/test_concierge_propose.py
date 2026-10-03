"""The concierge proposes a new agent on the node, instead of recommending an outside product.

On 2026-10-02 a freshly bought place's concierge, asked for "an agent that every morning gathers the
CRM's open deals", recommended CrewAI Studio and named HubSpot, Salesforce and Pipedrive. The owner's own
CRM (CADENCE, a workspace in their organism) went unmentioned and nothing was proposed. These tests hold
what replaces that, against a recording fake of the node at the seams the code really calls through
(`_aimeat_call`, `_aimeat_rest`):

  - it looks at the person's workspaces BEFORE it proposes, and the proposal names that workspace;
  - the crew_def is assembled to pass the node's three INVALID_CREW_DEF rules and the runtime's validator;
  - the scopes are what the runtime writes with plus what the tools need, CAPPED at what the concierge
    holds, and what was left out is said; a SCOPE_ESCALATION is corrected once from the node's own words;
  - the reply relays the node's `next_step` and `approval_url` as they are, and a refusal in its own code;
  - the schedule is created only after the agent exists, never before.
"""

from __future__ import annotations

import pytest

from crewaimeat import concierge_propose, workspace_tools
from crewaimeat.agent_scopes import RUNTIME_WRITE_SCOPES

NEXT_STEP = (
    "Morning deals is waiting for your approval. Open http://node/v1/profile?tab=agents and approve it "
    "there; it is created, credentialed and started only then."
)
APPROVAL_URL = "http://node/v1/profile?tab=agents"


@pytest.fixture(autouse=True)
def _owner_default_is_not_the_node(monkeypatch):
    """The owner's default road is read from the node; offline it is the machine road unless a test says."""
    monkeypatch.setattr("crewaimeat.llm_choice.default_is_node_road", lambda agent_name: False)


class FakeNode:
    """Answers the tools and routes the concierge uses, and records every call in order."""

    def __init__(self, *, held=("*",), agents=(), propose_answers=None):
        self.calls: list[tuple[str, dict]] = []
        self.memory: dict[str, object] = {}
        self.held = list(held) if held is not None else None
        self.agents = list(agents)
        self.propose_answers = list(propose_answers or [])
        self.schedules: list[tuple[str, dict]] = []

    def call(self, agent_name, tool, payload, **kw):
        self.calls.append((tool, payload))
        if tool == "aimeat_organism_list":
            return {"organisms": [{"id": "org-1", "name": "Acme Oy"}]}
        if tool == "aimeat_workspace_list":
            return {"workspaces": [{"id": "ws-crm", "name": "CADENCE"}, {"id": "ws-doc", "name": "Documents"}]}
        if tool == "aimeat_workspace_read":
            return {"spaces": [{"name": "deals", "instances": [{"id": "d1", "title": "Rantanen Ky"}]}]}
        if tool == "aimeat_agent_propose":
            if self.propose_answers:
                return self.propose_answers.pop(0)
            return {"id": "p1", "already_waiting": False, "approval_url": APPROVAL_URL, "next_step": NEXT_STEP}
        if tool == "aimeat_memory_write":
            self.memory[payload["key"]] = payload["value"]
            return {"ok": True}
        if tool == "aimeat_memory_read":
            v = self.memory.get(payload["key"])
            return {"value": v} if v is not None else None
        if tool == "aimeat_memory_delete":
            self.memory.pop(payload["key"], None)
            return {"ok": True}
        if tool == "aimeat_agents_list":
            return {"agents": [{"name": n} for n in self.agents]}
        return None

    def rest(self, agent_name, method, path, body=None, **kw):
        self.calls.append((f"{method} {path}", body or {}))
        if path.endswith("/refusals"):
            if self.held is None:
                return {"ok": False, "error": {"code": "NOT_FOUND"}, "http_status": 404}
            return {"granted_scopes": self.held, "refusals": []}
        if method == "POST" and path.endswith("/schedules"):
            self.schedules.append((path, body))
            return {"id": "sched-1"}
        return None

    def tools(self) -> list[str]:
        return [t for t, _ in self.calls]

    def proposal(self) -> dict:
        return next(p for t, p in self.calls if t == "aimeat_agent_propose")


@pytest.fixture
def node(monkeypatch):
    def install(**kw) -> FakeNode:
        n = FakeNode(**kw)
        for mod in (concierge_propose, workspace_tools):
            monkeypatch.setattr(mod, "_aimeat_call", n.call)
        monkeypatch.setattr(concierge_propose, "_aimeat_rest", n.rest)
        from crewaimeat import orchestrator

        monkeypatch.setattr(orchestrator, "_aimeat_call", n.call)
        return n

    return install


def _propose(**over) -> str:
    args = dict(
        name="morning-deals",
        display_name="Morning deals",
        purpose="Reads the open deals in CADENCE every morning and names the ones to act on today.",
        instructions="Read the open deals and list the ones whose next step is due today or overdue.",
        workspace="CADENCE",
        schedule_cron="",
    )
    args.update(over)
    return concierge_propose.propose("concierge", **args)


# ── it looks before it proposes, and proposes on the person's own data ───────────────────────


def test_the_workspaces_are_read_before_anything_is_proposed(node):
    n = node()
    _propose()
    tools = n.tools()
    assert "aimeat_agent_propose" in tools, "a request for an agent must end in a proposal"
    assert tools.index("aimeat_organism_list") < tools.index("aimeat_agent_propose")
    assert tools.index("aimeat_workspace_list") < tools.index("aimeat_agent_propose")


def test_the_proposal_works_on_the_named_workspace(node):
    n = node()
    _propose()
    p = n.proposal()
    task = p["crew_def"]["tasks"][0]["description"]
    assert 'read_workspace(workspace="CADENCE", organism="org-1")' in task, "it is told where its data is, by name"
    assert "workspace" in p["crew_def"]["agents"][0]["tools"], "and is given the tool to read it"
    assert "CADENCE" in p["purpose"]


def test_a_workspace_that_does_not_exist_is_said_and_nothing_is_proposed(node):
    n = node()
    reply = _propose(workspace="HubSpot")
    assert "aimeat_agent_propose" not in n.tools()
    assert "CADENCE" in reply, "it says what the person DOES keep"


# ── the definition passes the node and the runtime by construction ──────────────────────────


def test_the_definition_passes_the_nodes_three_rules_and_the_runtimes_validator(node):
    from crewaimeat.crew_def import validate_crew_doc

    n = node()
    _propose()
    d = n.proposal()["crew_def"]
    assert d["agents"] and d["tasks"], "at least one agent and one task"
    assert any("{{ctx.prompt}}" in t["description"] for t in d["tasks"]), "a task takes the request"
    keys = {a.get("name") or a.get("role") for a in d["agents"]}
    assert all(t["agent"] in keys for t in d["tasks"]), "every task names an agent that exists"
    assert validate_crew_doc(d) == [], "the runtime that will run it accepts it"


def test_model_written_braces_cannot_become_a_template(node):
    from crewaimeat.crew_def import validate_crew_doc

    n = node()
    _propose(instructions="Summarise {{ctx.secrets}} and the deals.")
    d = n.proposal()["crew_def"]
    assert "{{ctx.secrets}}" not in d["tasks"][0]["description"]
    assert validate_crew_doc(d) == []


def test_mode_and_run_mode_are_the_ones_whose_tasks_start_by_themselves(node):
    n = node()
    _propose()
    p = n.proposal()
    assert p["mode"] == "task-runner" and p["run_mode"] == "spawn"


# ── scopes: what it writes with, what its tools need, capped at what the concierge holds ─────


def test_the_runtimes_own_write_scopes_are_always_asked_for(node):
    n = node()
    _propose()
    scopes = n.proposal()["scopes"]
    assert set(RUNTIME_WRITE_SCOPES) <= set(scopes), "memory:read alone failed its first run (measured)"
    assert "memory:read" in scopes


def test_an_agent_that_reads_a_workspace_is_given_the_scope_to_read_it(node):
    # Reading a workspace needs organism:read (routes/organisms/workspace-read.ts); only LISTING is
    # membership-gated. Measured live: without it the approved agent listed CADENCE and read nothing.
    n = node()
    _propose(workspace="CADENCE")
    assert "organism:read" in n.proposal()["scopes"]


def test_a_tools_needs_are_added(node):
    n = node()
    _propose(tools="schedule")
    assert {"workflow:read", "task:write"} <= set(n.proposal()["scopes"])


def test_scopes_are_capped_at_what_the_concierge_holds_and_the_gap_is_said(node):
    n = node(held=["memory:read", "memory:write", "agent:write"])
    reply = _propose(tools="dm")
    scopes = n.proposal()["scopes"]
    assert "messages:send" not in scopes and "messages:read" not in scopes
    assert "messages:send" in reply and "I do not hold" in reply, "what was left out is said, not dropped"


def test_an_older_node_that_cannot_say_what_is_held_caps_nothing(node):
    n = node(held=None)
    _propose(tools="dm")
    assert "messages:send" in n.proposal()["scopes"], "the node's own ceiling decides"


def test_a_scope_escalation_is_corrected_once_from_the_nodes_own_words(node):
    refused = {
        "ok": False,
        "error": {
            "code": "SCOPE_ESCALATION",
            "message": "You cannot propose an agent that would hold more than you do. Beyond yours: agent:write.",
        },
        "http_status": 403,
    }
    n = node(held=None, propose_answers=[refused])
    reply = _propose()
    proposals = [p for t, p in n.calls if t == "aimeat_agent_propose"]
    assert len(proposals) == 2, "one correction, not a loop"
    assert "agent:write" not in proposals[1]["scopes"]
    assert NEXT_STEP in reply and "agent:write" in reply


# ── the node's words, relayed ────────────────────────────────────────────────────────────────


def test_the_reply_relays_next_step_and_the_approval_address(node):
    node()
    reply = _propose()
    assert NEXT_STEP in reply, "the node wrote it to be relayed as it is"
    assert APPROVAL_URL in reply


def test_the_reply_asks_no_second_yes_no(node):
    node()
    reply = _propose().lower()
    assert "yes/no" not in reply and "shall i" not in reply and "do you want me to" not in reply


def test_a_refusal_comes_back_in_the_nodes_own_code_and_words(node):
    taken = {
        "ok": False,
        "error": {"code": "NAME_TAKEN", "message": 'You already have an agent called "morning-deals".'},
    }
    node(propose_answers=[taken])
    reply = _propose()
    assert "NAME_TAKEN" in reply and 'already have an agent called "morning-deals"' in reply


@pytest.mark.parametrize(
    ("over", "said"),
    [
        ({"name": "Morning Deals"}, "cannot be an agent name"),
        ({"name": "md"}, "cannot be an agent name"),
        ({"purpose": "deals"}, "full sentence"),
        ({"schedule_cron": "every morning"}, "5-field cron"),
        ({"tools": "hubspot"}, "cannot give an agent hubspot"),
    ],
)
def test_what_the_node_would_refuse_is_said_before_it_is_asked(node, over, said):
    n = node()
    assert said in _propose(**over)
    assert "aimeat_agent_propose" not in n.tools()


# ── the clock comes after the approval ──────────────────────────────────────────────────────


def test_a_scheduled_proposal_promises_the_clock_and_keeps_it_for_later(node):
    n = node()
    reply = _propose(schedule_cron="0 7 * * *")
    assert "start morning-deals" in reply
    assert f"{concierge_propose.PENDING_PREFIX}morning-deals" in n.memory
    assert n.schedules == [], "no schedule before the agent exists"


def test_start_refuses_while_the_agent_does_not_exist_yet(node):
    n = node()
    _propose(schedule_cron="0 7 * * *")
    reply = concierge_propose.start_proposed("concierge", "morning-deals")
    assert n.schedules == [], "a schedule for an agent that does not exist fails on its first run"
    assert "Approve it first" in reply and APPROVAL_URL in reply


def test_start_sets_the_schedule_once_the_agent_exists(node):
    n = node()
    _propose(schedule_cron="0 7 * * *", timezone="Europe/Helsinki")
    n.agents.append("morning-deals")  # the owner approved
    reply = concierge_propose.start_proposed("concierge", "morning-deals")
    assert len(n.schedules) == 1
    path, body = n.schedules[0]
    assert path == "/v1/agents/morning-deals/schedules", "the new agent's path, the concierge's own token"
    assert body["kind"] == "agent_task" and body["cron"] == "0 7 * * *" and body["timezone"] == "Europe/Helsinki"
    assert body["task_template"]["title"] and body["task_template"]["description"]
    assert f"{concierge_propose.PENDING_PREFIX}morning-deals" not in n.memory, "the promise is kept once"
    assert "Done" in reply


def test_start_without_a_promise_says_so(node):
    n = node(agents=["morning-deals"])
    assert "no schedule waiting" in concierge_propose.start_proposed("concierge", "morning-deals")
    assert n.schedules == []


# ── the workspace reader the proposed agent gets ────────────────────────────────────────────


def test_an_exact_workspace_name_beats_a_partial_one():
    found = [
        {"organism_id": "o", "organism": "O", "ws": "a", "name": "CRM archive"},
        {"organism_id": "o", "organism": "O", "ws": "b", "name": "CRM"},
    ]
    assert workspace_tools.find_workspace(found, "crm")["ws"] == "b"


def test_an_ambiguous_partial_name_finds_nothing_rather_than_guessing():
    found = [
        {"organism_id": "o", "organism": "O", "ws": "a", "name": "Sales CRM"},
        {"organism_id": "o", "organism": "O", "ws": "b", "name": "Support CRM"},
    ]
    assert workspace_tools.find_workspace(found, "crm") is None


def test_every_proposable_tool_is_one_the_runtime_resolves():
    from crewaimeat.crew_def import TOOL_PURPOSES, TOOL_REGISTRY

    assert set(concierge_propose.PROPOSABLE_TOOLS) <= set(TOOL_REGISTRY)
    assert "workspace" in TOOL_REGISTRY and "workspace" in TOOL_PURPOSES


# ── the runtime states its own needs ────────────────────────────────────────────────────────


def test_crew_menu_states_the_scopes_every_agent_writes_with():
    import crewaimeat.crew_invoke as ci

    ok, menu = ci.handle("crew.menu", {}, agent_name="concierge")
    assert ok
    assert menu["required_scopes"] == list(RUNTIME_WRITE_SCOPES)
    assert "workspace" in {t["id"] for t in menu["tools"]}


# ── the concierge itself ────────────────────────────────────────────────────────────────────


@pytest.fixture
def concierge():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "crews" / "concierge_crew.py"
    spec = importlib.util.spec_from_file_location("concierge_crew_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_proposal_tools_are_on_the_task_path_too(concierge):
    # A hosted place reaches the concierge by ASSIGNED TASK as well as by direct message; the delegation
    # tools are DM-only, and these must not be.
    names = [t.name for t in concierge._concierge_tools({"attachments": []})]
    assert {"look_at_my_workspaces", "propose_agent", "start_proposed_agent"} <= set(names)


def test_the_proposal_tools_are_on_the_dm_path(concierge):
    names = [t.name for t in concierge._concierge_tools({"attachments": []}, ask_to="u", ask_conv="c")]
    assert {"look_at_my_workspaces", "propose_agent", "start_proposed_agent"} <= set(names)


def test_the_prompt_sends_a_new_agent_request_to_propose_agent_and_away_from_outside_products(concierge):
    task = concierge._task("I want an agent that every morning gathers the CRM's open deals.", "", None, "2026-10-02")
    text = task.description
    assert "propose_agent" in text and "look_at_my_workspaces" in text
    assert "Never recommend an outside agent builder" in text


def test_what_it_says_about_itself_mentions_proposals_and_keeps_its_negative_scope(concierge):
    line = "I can propose a new agent for you; you approve it on your Agents page and it runs here."
    assert line in concierge.README
    ask = concierge.OFFERS[0]["ask"]
    assert line in ask
    # Still true: the agent it proposes runs the job, not the concierge.
    assert "I do not run scheduled jobs" in ask
    assert "Propose a new agent" in concierge.CAPABILITIES_TEXT


def test_a_proposal_on_the_owners_node_road_asks_for_ai_use(node, monkeypatch):
    """The new agent takes the owner's default road; on the node road it calls /v1/llm, which needs ai:use."""
    monkeypatch.setattr("crewaimeat.llm_choice.default_is_node_road", lambda agent_name: True)
    n = node()
    _propose()
    assert "ai:use" in n.proposal()["scopes"]
    assert "agent:write" not in n.proposal()["scopes"], "no longer needed by the runtime (aimeat-protocol bcd4027ed)"


# ── an empty node still gets its proposal (ruling 2026-10-02) ───────────────────────────────


def test_on_a_node_with_no_workspace_the_proposal_is_filed_and_the_assumption_is_said(node, monkeypatch):
    """Asked on an empty place, the concierge asked for the workspace name, the language and the time
    instead of proposing. The customer asked for a proposal and must get one: the agent reads memory
    until the workspace exists, and the reply says so as an assumption to correct."""
    n = node()
    monkeypatch.setattr(workspace_tools, "list_workspaces", lambda agent: [])
    reply = _propose(workspace="CADENCE")
    p = n.proposal()
    assert p["name"] == "morning-deals", "proposed, not asked"
    assert "workspace" not in p["crew_def"]["agents"][0]["tools"] and "memory" in p["crew_def"]["agents"][0]["tools"]
    assert "no workspace called 'CADENCE' yet" in reply and "reads memory until one exists" in reply
    assert NEXT_STEP in reply


def test_when_workspaces_exist_but_none_matches_nothing_is_guessed(node):
    n = node()
    reply = _propose(workspace="Pipeline")
    assert "aimeat_agent_propose" not in n.tools()
    assert "cannot find a workspace called 'Pipeline'" in reply and "CADENCE" in reply


def test_the_prompt_tells_it_to_propose_on_an_empty_node_with_stated_assumptions(concierge):
    task = concierge._task("Haluan agentin, joka kerää joka aamu CRM:n avoimet kaupat.", "", None, "2026-10-02")
    text = task.description
    assert "ALWAYS file the proposal in THIS run" in text
    assert "07:00 Europe/Helsinki" in text and "reads memory until one exists" in text
    assert "Never ask the workspace name, the language or the time INSTEAD of proposing" in text


def test_a_clock_job_is_not_given_the_schedule_tool(node):
    """A sold place's morning-brief proposal asked for workflow:read and task:write (2026-10-03). Those came
    with the `schedule` tool; the clock is set by the concierge after the approval, so the agent's runs
    never call a schedule route."""
    n = node()
    _propose(tools="workspace,schedule", schedule_cron="0 7 * * *")
    p = n.proposal()
    assert "schedule" not in p["crew_def"]["agents"][0]["tools"]
    assert "workflow:read" not in p["scopes"] and "task:write" not in p["scopes"]
    assert set(p["scopes"]) == {"memory:read", "memory:write", "organism:read"}


def test_an_agent_that_manages_schedules_itself_keeps_the_tool(node):
    n = node()
    _propose(tools="schedule", schedule_cron="")
    assert {"workflow:read", "task:write"} <= set(n.proposal()["scopes"])

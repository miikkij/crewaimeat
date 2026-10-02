"""Workspace tools — READ the owner's organisms and workspaces, by name, from inside a crew.

Why this exists. Asked for "an agent that gathers the CRM's open deals every morning", the concierge of a
freshly bought place recommended CrewAI Studio and named HubSpot, Salesforce and Pipedrive (2026-10-02).
The person's own CRM, CADENCE, is a workspace in their organism, and nothing the concierge held could see
it. These tools are how a crew sees what the person keeps: the organisms they belong to, each one's
workspaces by name, and one workspace's index (its spaces and record titles) or the records themselves.

Two consumers, one implementation:
  - the concierge's `look_at_my_workspaces`, so a proposal names the person's own data;
  - the `workspace` tool id in crew_def's TOOL_REGISTRY, so the agent it proposes reads that data the same
    way. Without it a JSON-defined agent could read a workspace record only if handed its exact memory key,
    which the proposer would have to reconstruct -- a guess written into somebody else's agent.

READ-ONLY on purpose. Writing rows is a different permission (organism:write) and a different trust
decision; an agent that has to write the CRM is proposed with a tool that says so, not given it here.

Access is the node's, and it is two different gates. LISTING an organism's workspaces is gated by
membership (routes/organisms/workspace-access.ts), and the connector merges the owner's own organisms in,
so a same-owner agent sees them. READING one -- its index or its records -- also needs the scope
`organism:read` (routes/organisms/workspace-read.ts). An agent without it lists CADENCE and reads nothing
in it, which is what the live test measured before the proposal learned to ask for the scope.
Everything goes through `_aimeat_call`, the connector's /local/call surface every crew uses.
"""

from __future__ import annotations

import json

from crewaimeat.aimeat_crew import _aimeat_call


def _items(data, key: str) -> list[dict]:
    """The list under `key` in a tool's answer, or the answer itself when it already is the list."""
    got = data.get(key) if isinstance(data, dict) else data
    return [x for x in got if isinstance(x, dict)] if isinstance(got, list) else []


def list_workspaces(agent_name: str) -> list[dict]:
    """Every workspace this agent can see, as {organism_id, organism, ws, name}.

    One organism the agent cannot read does not hide the others: a probe of an organism it is not an
    active member of is an expected answer while scanning, so it is asked quietly and skipped.
    """
    orgs = _items(_aimeat_call(agent_name, "aimeat_organism_list", {}) or {}, "organisms")
    out: list[dict] = []
    for o in orgs:
        oid = o.get("id")
        if not oid:
            continue
        wl = _aimeat_call(agent_name, "aimeat_workspace_list", {"organism_id": oid}, quiet=True) or {}
        for w in _items(wl, "workspaces"):
            if w.get("id"):
                out.append(
                    {
                        "organism_id": oid,
                        "organism": o.get("name") or oid,
                        "ws": w["id"],
                        "name": w.get("name") or w["id"],
                    }
                )
    return out


def find_workspace(found: list[dict], name: str) -> dict | None:
    """The workspace whose name (or id) matches `name`, ignoring case; an exact match beats a partial one."""
    want = (name or "").strip().lower()
    if not want:
        return None
    exact = [w for w in found if want in (w["name"].lower(), w["ws"].lower())]
    if exact:
        return exact[0]
    partial = [w for w in found if want in w["name"].lower()]
    return partial[0] if len(partial) == 1 else None


def workspace_index(agent_name: str, organism_id: str, ws: str):
    """The workspace's index: its spaces and, per space, each record's id and title. No bodies.

    None when the node answered nothing. The index is the node's own small form of a workspace however
    large it is (aimeat_workspace_read without ids), so it is safe to hand to a model whole.
    """
    return _aimeat_call(agent_name, "aimeat_workspace_read", {"organism_id": organism_id, "ws": ws}, quiet=True)


def workspace_records(agent_name: str, organism_id: str, ws: str, ids: list[str]):
    """The full values of the records `ids` in one workspace."""
    return _aimeat_call(
        agent_name, "aimeat_workspace_read", {"organism_id": organism_id, "ws": ws, "ids": ids}, quiet=True
    )


def render_workspaces(found: list[dict]) -> str:
    if not found:
        return "No organisms or workspaces are visible to me."
    lines = ["Organisms and workspaces I can see:"]
    for w in found:
        lines.append(f"- {w['name']} (workspace {w['ws']}) in organism {w['organism']} ({w['organism_id']})")
    return "\n".join(lines)


def make_workspace_tools(agent_name: str) -> list:
    """The crew_def `workspace` tool: list the owner's workspaces, read one's index or its records."""
    from crewai.tools import tool

    @tool("list_workspaces")
    def list_workspaces_tool() -> str:
        """List the organisms and workspaces you can read, by name, with the ids read_workspace needs."""
        return render_workspaces(list_workspaces(agent_name))

    @tool("read_workspace")
    def read_workspace(organism_id: str, ws: str, ids: str = "") -> str:
        """Read a workspace. Without `ids`: its index (the spaces and every record's id and title). With
        `ids` (comma-separated record ids from the index): those records in full. Read the index first,
        then the records you need."""
        wanted = [i.strip() for i in (ids or "").split(",") if i.strip()]
        data = (
            workspace_records(agent_name, organism_id, ws, wanted)
            if wanted
            else workspace_index(agent_name, organism_id, ws)
        )
        if data is None:
            return f"Could not read workspace {ws} in organism {organism_id} (not visible to me, or it is empty)."
        return json.dumps(data, ensure_ascii=False)

    return [list_workspaces_tool, read_workspace]
